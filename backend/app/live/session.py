"""Live tracking: actual versus plan, while driving.

The reason for the whole project. A plan calculated at departure is wrong
after eighty kilometres - speed, temperature, wind and traffic jams add up in
the same direction. Whoever notices needs no buffer of twenty percent;
whoever does not notice ends up at an occupied charger with four percent.

What happens here: for every sample it is determined where on the route it
lies, what the plan had predicted at that spot, and how far reality deviates
from that. From this come two running factors - one for consumption, one for
time - and from them the question whether the charging plan is still valid.
If it is not, it is recalculated (`live/replanning.py`).

Replanning deliberately does not happen at every measurement, but only when
one of the triggers from section 2.3 of the concept applies. A plan that
changes every thirty seconds is no plan.
"""
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .. import models
from ..energy import charge_phases
from ..energy.profile import minutes_at as plan_minutes_at
from ..energy.profile import soc_at as plan_soc_at
from ..charging import availability
from ..routing.corridor import point_on_route
from . import replanning

log = logging.getLogger("uvicorn.error")

# Thresholds for replanning, taken one to one from section 2.3 of the
# concept. Deliberately thresholds and not a recalculation at every
# measurement: whoever has just decided to take a break in 40 km should not
# have to overturn that three times. A change must mean something.
THRESHOLD_SOC_PP = 5.0            # percentage points of deviation
THRESHOLD_DETOUR_M = 500.0         # distance from the route
THRESHOLD_DETOUR_S = 60.0          # ... and how long it has to last
THRESHOLD_ARRIVAL_MIN = 10.0      # shift of the arrival time

# How far one must have driven before the same non-urgent trigger sets off
# another replan. Without this lock every measurement would recalculate for
# as long as the deviation persists - and that is the normal case, not the
# exception.
REPLANNING_SPACING_KM = 10.0

# Over how many kilometres the factors are averaged. Too short, and a single
# traffic-light phase bends them; too long, and the change of weather behind
# the pass arrives too late.
TIMEFRAME_KM = 25.0
# Before that, the SoC display (usually 1 % resolution) is too coarse for a
# statement.
MIN_DISTANCE_KM = 5.0


@dataclass
class State:
    km_on_route: float
    spacing_to_route_m: float
    # Where the sample was located.
    #
    # Without these two, the UI had to back-calculate the position from the
    # *planned* profile ("which support point is at km X"). For a
    # **recording** that profile does not exist - it only comes into being on
    # completion -, and the back-calculation silently returned (0, 0). The
    # map showed the Gulf of Guinea, the driven track consisted of a single
    # point, and the progress curve, which measures its x-axis along that
    # track, collapsed to a vertical line - in precisely the mode in which the
    # curve is the only thing there is to see.
    #
    # The server knows the coordinate; it has just received it.
    lat: float
    lon: float
    # The state of charge used for calculations - reported or extrapolated.
    # `soc_reported` says which of the two: whoever sees a number at the wheel
    # should know whether it was measured or calculated from the profile.
    actual_soc: float | None
    soc_reported: bool
    plan_soc: float | None
    deviation_pp: float | None
    consumption_factor: float
    time_factor: float
    remaining_km: float
    forecast_soc_at_target: float | None
    reserve_at_km: float | None
    arrival_shift_min: float | None
    next_stop: dict | None
    replanning_required: bool
    reason: str
    # Only set if a replan actually took place.
    plan: dict | None = None
    plan_changed: bool = False
    change: str = ""
    urgent: bool = field(default=False, repr=False)
    # Where `actual_soc` comes from: "gemessen" (measured; this point),
    # "gerechnet" (calculated; from the profile) or "zuletzt" (last; the last
    # measurement of this trip, because this point has none and there is no
    # profile - the case of a recording while the car does not respond).
    soc_source: str = "gemessen"


# ---------------------------------------------------------------------------
# The two running factors
# ---------------------------------------------------------------------------

def _recent_points(points: list) -> list:
    """The samples of the last `TIMEFRAME_KM`, but at least two."""
    usable = [p for p in points
                 if p.km_on_route is not None and p.plan_soc is not None]
    if len(usable) < 2:
        return []
    last = usable[-1]
    recent = [p for p in usable
               if (last.km_on_route - p.km_on_route) <= TIMEFRAME_KM]
    return recent if len(recent) >= 2 else usable[-2:]


def _charge_pauses_minutes(points: list, energy_profile: list) -> float:
    """How much of the elapsed time was spent on charging pauses.

    Necessary because the energy profile carries exclusively **driving time**:
    charging time is in the plan, never in the profile. Whoever holds the wall
    clock unfiltered against the profile therefore sees, after the first
    charging stop, a delay equal to the charging duration - and permanently,
    because it is never made up. The trigger "arrival shifts" would then stand
    above its threshold for the rest of the trip and report the same delay
    every ten kilometres. A message that always comes gets switched off.

    The pause is recognised by the rising state of charge: while driving it
    falls, while charging it rises. But not the whole time span is counted,
    only the part that exceeds the driving time for the distance covered in
    the process. That handles two cases at once - regeneration on a long
    descent raises the state of charge too, but costs no additional time; and
    it makes no difference whether the logger kept transmitting during
    charging or only woke up again afterwards.

    A pause without charging is not deducted - lunch, traffic jam, a jam in
    front of the roadworks. That really does shift the arrival, and that is
    exactly what the trigger should see.
    """
    def driven(from_km: float, until_km: float) -> float:
        begin = plan_minutes_at(energy_profile, from_km)
        end = plan_minutes_at(energy_profile, until_km)
        return 0.0 if begin is None or end is None else max(0.0, end - begin)

    return charge_phases.charge_pauses_minutes(points, driven)


def _consumption_factor(points: list) -> float | None:
    """Actual consumption divided by planned consumption over the sliding window.

    This is calculated from SoC differences and not from absolute values: a
    speedometer that consistently reads two percent too high does not distort
    the difference - but it does distort the absolute comparison.

    Only reported states of charge count. An estimated value here would mean
    measuring the model against itself: the factor would always come out at
    1.0 and thereby claim the forecast is right - and the more confidently,
    the longer nobody has checked.
    """
    # Filter first, then window - not the other way round. Whoever only types
    # in the state of charge at charging stops has two reports two hundred
    # kilometres apart; a window of 25 km over *all* points would contain
    # neither two of them and the factor would never come about. Over the
    # reported states of charge, the fallback rule in `_timeframe` takes over
    # instead and takes the last two - a long measurement basis is even the
    # better one here.
    recent = _recent_points([p for p in points if p.soc is not None])
    if not recent:
        return None

    first, last = recent[0], recent[-1]
    if last.km_on_route - first.km_on_route < MIN_DISTANCE_KM:
        return None

    # Charging sections drop out instead of making the factor unusable.
    # Previously this said `first.soc - last.soc`, and a charging stop in the
    # window yielded a negative "consumption" - caught only by the outlier
    # limit below, which then discarded the factor. The consequence: after
    # every charging stop the old value kept applying for the duration of the
    # window, although fresh measurements had been taken.
    actual_consumption = 0.0
    plan_consumption = 0.0
    for section in charge_phases.sections(recent):
        if section.charges:
            continue
        actual_consumption += section.soc_pp
        plan_consumption += ((section.begin.plan_soc or 0.0)
                           - (section.past.plan_soc or 0.0))
    if plan_consumption <= 0.5:
        return None

    factor = actual_consumption / plan_consumption
    # The limit stays as a safety net: it now only catches real outliers - a
    # re-plugged logger, an SoC jump after a restart -, no longer the normal
    # case of a charging stop.
    if not 0.4 <= factor <= 2.5:
        return None
    return round(factor, 3)


def soc_estimate(points: list, point, consumption_factor: float) -> float | None:
    """The state of charge at this spot, if none was reported.

    This is the core of operation without vehicle data: the phone delivers
    position constantly, someone types in the state of charge now and then,
    and in between the energy profile carries. It knows gradient, speed and
    weather of the route - it is exactly the model on which the charging plan
    is built -, and the measured consumption factor says how far the car
    deviates from it.

    So not a forecast, but the same extrapolation that replanning calculates
    with anyway. And it is pulled back to reality at every state of charge
    that is typed in.

    Calculation starts from the **last report** and not from the start:
    whoever has charged on the way has a jump in the state of charge that no
    profile knows. The last report lies behind that jump.
    """
    if point.plan_soc is None:
        return None
    previous = [p for p in points
                 if p.soc is not None and p.plan_soc is not None
                 and p is not point]
    if not previous:
        # No state of charge ever reported - then the planned one applies. The
        # profile begins at the start state of charge of the trip, so that is
        # the only statement available at all. Without this fallback the whole
        # display would stay empty until someone types something in of their
        # own accord, and the tracking would be off for the unsuspecting case.
        return point.plan_soc
    tail = previous[-1]
    consumed_plan = (tail.plan_soc or 0.0) - point.plan_soc
    return round(tail.soc - consumed_plan * consumption_factor, 2)


def _time_factor(points: list, energy_profile: list) -> float | None:
    """Actual driving time divided by planned driving time over the same window.

    A factor of its own is needed because consumption does not see a traffic
    jam: whoever stands still even uses slightly more per kilometre, but the
    arrival time shifts by a multiple of that. Without this number the trigger
    "arrival time shifts" would not be available - and every arrival time in
    the replanned charging plan would be the one from the old plan.
    """
    recent = _recent_points(points)
    if not recent:
        return None

    first, last = recent[0], recent[-1]
    if last.km_on_route - first.km_on_route < MIN_DISTANCE_KM:
        return None
    if not first.timestamp or not last.timestamp:
        return None

    # Charging time does not belong in the time factor: whoever stood half an
    # hour at the charger is not in a traffic jam. Without the deduction the
    # factor would be so large after every charging stop that the limit below
    # discards it - and it would be frozen for the next TIMEFRAME_KM, i.e.
    # blind precisely on the stretch where it is needed again.
    actual_minutes = ((last.timestamp - first.timestamp).total_seconds() / 60.0
                   - _charge_pauses_minutes(recent, energy_profile))
    plan_end = plan_minutes_at(energy_profile, last.km_on_route)
    plan_start = plan_minutes_at(energy_profile, first.km_on_route)
    if plan_end is None or plan_start is None:
        return None
    plan_minutes = plan_end - plan_start
    if plan_minutes <= 0.5 or actual_minutes <= 0:
        return None

    factor = actual_minutes / plan_minutes
    # The same outlier limit as for consumption - now only against what the
    # charging-pause deduction does not explain.
    if not 0.4 <= factor <= 3.0:
        return None
    return round(factor, 3)


# ---------------------------------------------------------------------------
# Sample comes in
# ---------------------------------------------------------------------------

def record_sample(db, session: models.LiveSession, lat: float, lon: float,
                        soc: float | None = None, speed_kmh: float | None = None,
                        outside_temp_c: float | None = None,
                        timestamp: datetime | None = None,
                        raw_values: dict | None = None,
                        new_plan: bool = True) -> State:
    """File a sample and return the new state.

    `soc` may be missing. Then it is a pure position report, as the phone can
    deliver it every second - the state of charge for this point is
    extrapolated from the energy profile (`soc_estimate`). Only that way do
    time factor and arrival forecast come about at all, as long as the car
    does not report its state of charge itself.

    `timestamp` overrides the timestamp. The simulator needs this: it plays
    hours back in seconds, and with real times the time factor would be
    meaningless there - that is, precisely the quantity that represents the
    traffic jam.

    `new_plan=False` records the point without recalculating the remaining
    route. Needed for points delivered after the fact from a dead zone: a
    plan calculated from a position ten minutes ago is outdated as soon as it
    appears - and would send a change to the phone on top of that. Only the
    last point of the batch, which is the present, may replan.
    """
    trip = session.trip
    geometry = trip.geometry or []
    profile = trip.energy_profile or []

    km, spacing = point_on_route(geometry, lat, lon)
    plan_value = plan_soc_at(profile, km)

    point = models.LivePoint(lat=lat, lon=lon, soc=soc, speed_kmh=speed_kmh,
                             outside_temp_c=outside_temp_c, km_on_route=km,
                             plan_soc=plan_value, raw_values=raw_values,
                             timestamp=timestamp or datetime.utcnow())
    # Append via the relationship and not via db.add(): otherwise the point
    # appears twice in the loaded collection - once through the append, once
    # through the cascade - and the factors calculate with a duplicate.
    session.points.append(point)
    capacity_remember(trip.vehicle, raw_values, point.timestamp)
    db.flush()

    factor = _consumption_factor(session.points)
    if factor is not None:
        session.consumption_factor = factor
    zfaktor = _time_factor(session.points, profile)
    if zfaktor is not None:
        session.time_factor = zfaktor

    # A detour needs duration, not just distance: an inaccurate measurement
    # under a bridge is not leaving the route.
    if spacing > THRESHOLD_DETOUR_M:
        if session.detour_since is None:
            session.detour_since = point.timestamp
    else:
        session.detour_since = None

    state = _build_state(session, point, spacing)

    # Replanning uses the state of charge that applies - reported or
    # extrapolated. Otherwise a pure position point would reach the optimizer
    # with `None`.
    if (new_plan and state.replanning_required
            and _may_new_plan(session, km, state)):
        _replan(db, session, state, km, state.actual_soc)

    session.hint = state.reason
    db.commit()
    return state


# From what deviation a new capacity value is written at all. The counter
# jumps by a few watt hours between two measurements; writing every time
# would mean changing a row at every sample without anything changing.
CAPACITY_STEP_KWH = 0.2


def capacity_remember(vehicle, raw_values: dict | None, timestamp) -> None:
    """Record the battery capacity reported by the vehicle on the vehicle.

    It comes along as `battery_kwh` in the raw values - the dongle reads it
    every forty rounds. It is kept because **every** conversion between state
    of charge and kilowatt hours depends on it: the measured consumption, the
    correction factor learned from it, the charge swings in the charging plan,
    the remaining range. The profile holds a brochure figure for a new
    vehicle; here is what this battery can do today.

    It is checked against the profile value: more than the brochure or less
    than half is not ageing but a reading error. The plausibility limit is
    deliberately here and not only in the dongle - measured values can also
    come from a logger that nobody has checked.
    """
    if not vehicle or not raw_values:
        return
    capacity = raw_values.get("battery_kwh")
    if not isinstance(capacity, (int, float)):
        return
    if not (0.5 * vehicle.battery_net_kwh <= capacity
            <= vehicle.battery_net_kwh * 1.05):
        log.info("Capacity %s kWh discarded - does not match %s kWh in the "
                 "profile.", capacity, vehicle.battery_net_kwh)
        return
    so_far = vehicle.measured_capacity_kwh
    if so_far is not None and abs(so_far - capacity) < CAPACITY_STEP_KWH:
        return
    if so_far is None:
        log.info("Capacity of %s measured for the first time: %.1f kWh (profile %.1f).",
                 vehicle.name, capacity, vehicle.battery_net_kwh)
    vehicle.measured_capacity_kwh = round(capacity, 2)
    vehicle.capacity_measured_at = timestamp or datetime.utcnow()


def speed_factor_measured(points: list, energy_profile: list) -> float | None:
    """How much faster than planned the trip is actually being driven.

    This is the reciprocal of the time factor: whoever covers a stretch in
    90 % of the allotted time is driving eleven percent faster. No separate
    measurement is needed for it - the time factor is already available, is
    adjusted for charging pauses and protected against outliers.

    The number is needed because the speed has so far been **guessed**: the
    slider in the planning view is at 120 %, and nobody knows whether that is
    right. Via v², that is the largest single item of the forecast.
    """
    factor = _time_factor(points, energy_profile)
    if factor is None or factor <= 0.1:
        return None
    return round(1.0 / factor, 3)


def _replan(db, session: models.LiveSession, state: State, km: float,
              soc: float) -> None:
    profile = session.trip.energy_profile or []

    # Either-or, not both-and: a consumption measured from real states of
    # charge already contains the effect of speed - and load, weather error
    # and battery age as well. It is the better source as soon as it exists.
    # As long as it does not, the measured speed is still far better than the
    # slider value from before departure.
    anchor = sum(1 for p in session.points if p.soc is not None)
    velocity = None if anchor >= 2 else speed_factor_measured(session.points, profile)

    try:
        updated_plan = replanning.schedule(
            db, session.trip, km, soc,
            replanning.read_parameter(session.plan),
            session.consumption_factor, session.time_factor, speed_factor=velocity)
    except Exception as failure:      # noqa: BLE001
        # A failed replan must not end the trip: the measurement keeps
        # running, and the old plan is still better than none at all.
        log.warning("Replanning failed: %s", failure)
        return

    if not replanning.stops_same(session.plan, updated_plan):
        state.plan_changed = True
        state.change = replanning.describe_change(session.plan, updated_plan)
    session.plan = updated_plan
    state.plan = updated_plan


def _may_new_plan(session: models.LiveSession, km: float,
                     state: State) -> bool:
    """Lock against a plan that changes every minute.

    Urgent reasons - the charger is occupied, the reserve is not enough -
    always go through. Everything else only again after
    `REPLANNING_SPACING_KM`: the deviation persists, after all, otherwise the
    trigger would not have applied. Without the lock every single measurement
    would recalculate.
    """
    if session.plan is None or state.urgent:
        return True
    last_as_of = (session.plan or {}).get("reading_km")
    if last_as_of is None:
        return True
    return (km - last_as_of) >= REPLANNING_SPACING_KM


# ---------------------------------------------------------------------------
# State and triggers
# ---------------------------------------------------------------------------

def _build_state(session: models.LiveSession, point: models.LivePoint,
                    spacing_m: float) -> State:
    trip = session.trip
    vehicle = trip.vehicle
    profile = trip.energy_profile or []
    total_km = profile[-1]["km"] if profile else 0.0
    km = point.km_on_route or 0.0
    remaining_km = max(0.0, total_km - km)

    # The state of charge used for calculations here: the reported one,
    # otherwise the extrapolated one. Which of the two it was is a field of
    # its own in the state - whoever sees a number at the wheel should know
    # whether it was measured or calculated.
    reported = point.soc is not None
    actual_soc = point.soc if reported else soc_estimate(
        session.points, point, session.consumption_factor)
    soc_source = "gemessen" if reported else "gerechnet"
    if actual_soc is None:
        # A recording has no profile from which a state of charge could be
        # estimated. The display then shows the last measurement of this trip
        # - marked as such - instead of staying empty. Nothing is calculated
        # with it: without a profile there is neither deviation nor forecast.
        tail = next((p.soc for p in reversed(session.points)
                       if p.soc is not None), None)
        if tail is not None:
            actual_soc, soc_source = tail, "zuletzt"

    deviation = None
    if point.plan_soc is not None and actual_soc is not None:
        deviation = round(actual_soc - (point.plan_soc + _charged_pp(
            session.points, point)), 2)

    forecast = _forecast_at_target(profile, point, actual_soc,
                                 session.consumption_factor)
    reserve_at = _reserve_at(profile, point, actual_soc,
                               session.consumption_factor, vehicle.reserve_soc)
    shift = _arrival_shift(session, profile, point, total_km)
    next_stop, arrival_soc = _next_stop(session, profile, point, actual_soc)

    required, reason, urgent = _examine_replanning(
        vehicle=vehicle, deviation=deviation, spacing_m=spacing_m,
        detour_since=session.detour_since, now_ts=point.timestamp, forecast=forecast,
        reserve_at=reserve_at, total_km=total_km,
        shift=shift, upcoming=next_stop,
        arrival_soc=arrival_soc)

    return State(
        km_on_route=round(km, 2), spacing_to_route_m=round(spacing_m),
        lat=point.lat, lon=point.lon,
        actual_soc=None if actual_soc is None else round(actual_soc, 2),
        soc_reported=reported, soc_source=soc_source, plan_soc=point.plan_soc,
        deviation_pp=deviation,
        consumption_factor=round(session.consumption_factor, 3),
        time_factor=round(session.time_factor, 3),
        remaining_km=round(remaining_km, 1), forecast_soc_at_target=forecast,
        reserve_at_km=reserve_at, arrival_shift_min=shift,
        next_stop=next_stop, replanning_required=required, reason=reason,
        urgent=urgent)


def _charged_pp(points: list, until_point) -> float:
    """How many percentage points have been recharged up to here.

    Needed for the **deviation**, and only for that. The energy profile knows
    no charging stops: it counts the state of charge down from the start
    without interruption and on a long-distance trip goes deep into the
    negative - on the 774 km Hamburg-Munich down to -257 %. The deviation
    compared the measured state of charge directly with that and, after the
    first charging stop, reported triple-digit percentage points. In a test
    run it showed 262 pp there; the "Abweichung" (deviation) tile is thus
    unusable for every trip with a charging stop - that is, for every long
    one.

    Whoever has recharged 40 points should be 40 points above the profile.
    This function calculates exactly that out, and what remains is the
    question that matters: am I more economical or thirstier on the road than
    planned?

    Forecast and reserve mark do not need this - they calculate with
    differences from the current point anyway and are therefore already
    correct.
    """
    return charge_phases.charged_pp(points, until_point)


def _forecast_at_target(profile: list, point, actual_soc, consumption_factor: float):
    """The rest of the route extrapolated with the measured factor."""
    if not profile or actual_soc is None:
        return None
    rest_plan = (point.plan_soc or actual_soc) - (profile[-1].get("soc") or 0.0)
    return round(actual_soc - rest_plan * consumption_factor, 2)


def _reserve_at(profile: list, point, actual_soc, consumption_factor: float,
                 reserve_soc: float):
    """Where the reserve is reached if things continue as they have so far."""
    if actual_soc is None:
        return None
    for entry in profile:
        if (entry.get("km") or 0.0) < (point.km_on_route or 0.0):
            continue
        consumed = (point.plan_soc or actual_soc) - (entry.get("soc") or 0.0)
        if actual_soc - consumed * consumption_factor <= reserve_soc:
            return round(entry.get("km") or 0.0, 1)
    return None


def _arrival_shift(session, profile: list, point, total_km: float):
    """By how many minutes the arrival shifts - traffic jam included.

    Two parts: what is already lost, and what the time factor will still cost
    on the remaining route. Only together do they give the number that is of
    interest.

    The charging time already spent does not count as delay - it was in the
    plan. See `_charge_pauses_minutes`.
    """
    points = [p for p in session.points if p.km_on_route is not None]
    if len(points) < 2 or not profile:
        return None

    first = points[0]
    if not first.timestamp or not point.timestamp:
        return None
    actual_minutes = (point.timestamp - first.timestamp).total_seconds() / 60.0
    plan_now = plan_minutes_at(profile, point.km_on_route or 0.0)
    plan_start = plan_minutes_at(profile, first.km_on_route or 0.0)
    plan_target = plan_minutes_at(profile, total_km)
    if plan_now is None or plan_start is None or plan_target is None:
        return None

    so_far = (actual_minutes - (plan_now - plan_start)
              - _charge_pauses_minutes(points, profile))
    rest = max(0.0, plan_target - plan_now) * (session.time_factor - 1.0)
    return round(so_far + rest, 1)


def _next_stop(session, profile: list, point, actual_soc):
    """The next planned charging stop and the state of charge expected there.

    The expected value is extrapolated with the measured consumption factor
    and held against the plan. That is exactly the trigger from the concept:
    not the deviation here, but the one at the next stop - that is where it
    becomes a problem.
    """
    stops = ((session.plan or {}).get("stops") or [])
    km = point.km_on_route or 0.0
    stop = next((s for s in stops
                      if (s.get("km_on_route") or 0.0) > km + 0.5), None)
    if stop is None:
        return None, None

    target_km = stop.get("km_on_route") or 0.0
    plan_there = plan_soc_at(profile, target_km)
    if plan_there is None or point.plan_soc is None or actual_soc is None:
        return dict(stop), None

    consumed = point.plan_soc - plan_there
    extrapolated = round(actual_soc - consumed * session.consumption_factor, 2)
    description = {"id": stop.get("id"), "name": stop.get("name"),
                    "km_on_route": target_km,
                    "planned_soc": stop.get("arrival_soc"),
                    "expected_soc": extrapolated}
    return description, extrapolated


def _examine_replanning(*, vehicle, deviation, spacing_m, detour_since, now_ts,
                        forecast, reserve_at, total_km, shift,
                        upcoming, arrival_soc) -> tuple[bool, str, bool]:
    """Does the plan need touching, why - and is it urgent?

    The order is that of urgency: what makes the trip impossible comes before
    what merely makes it inconvenient. `urgent` decides whether the lock
    against too-frequent replanning is bypassed.
    """
    # 1. The next charging point is occupied. The only availability
    #    information that is really true - and it makes the plan worthless at
    #    once.
    if upcoming and upcoming.get("id") is not None:
        if availability.REPORTS.actual_reported(upcoming["id"]):
            name = upcoming.get("name") or "Der nächste Ladepunkt"
            return True, f"{name} ist als belegt gemeldet - Ausweichen.", True

    # 2. It will not last until the destination.
    if reserve_at is not None and reserve_at < total_km:
        return True, (f"Reserve wird bei km {reserve_at:.0f} erreicht - "
                      f"vorher laden."), True
    if forecast is not None and forecast < vehicle.reserve_soc:
        return True, (f"Ankunft mit {forecast:.0f} % prognostiziert, "
                      f"unter der Reserve von {vehicle.reserve_soc:.0f} %."), True

    # 3. Off the route - but only once it persists.
    if detour_since is not None and now_ts is not None:
        duration = (now_ts - detour_since).total_seconds()
        if duration >= THRESHOLD_DETOUR_S:
            return True, (f"Seit {duration / 60:.0f} min mehr als "
                          f"{spacing_m:.0f} m neben der Route."), True

    # 4. Something other than planned arrives at the next stop.
    if upcoming and arrival_soc is not None:
        planned = upcoming.get("planned_soc")
        if planned is not None and abs(arrival_soc - planned) >= THRESHOLD_SOC_PP:
            name = upcoming.get("name") or "nächster Stopp"
            return True, (f"Ankunft an {name} mit {arrival_soc:.0f} % statt "
                          f"{planned:.0f} % - Ladestopps neu rechnen."), False

    # 5. Without a plan, the deviation here remains the best statement available.
    if not upcoming and deviation is not None and abs(deviation) >= THRESHOLD_SOC_PP:
        direction = "unter" if deviation < 0 else "über"
        return True, (f"{abs(deviation):.0f} Prozentpunkte {direction} Plan - "
                      f"Ladestopps neu rechnen."), False

    # 6. Traffic jam: consumption hardly notices it, the arrival time very much.
    if shift is not None and abs(shift) >= THRESHOLD_ARRIVAL_MIN:
        word = "später" if shift > 0 else "früher"
        return True, (f"Ankunft {abs(shift):.0f} min {word} als "
                      f"geplant."), False

    if deviation is not None and abs(deviation) >= THRESHOLD_SOC_PP / 2:
        return False, f"{deviation:+.0f} Prozentpunkte gegenüber Plan.", False
    return False, "im Plan", False


def state_as_dict(state: State) -> dict:
    fields = asdict(state)
    fields.pop("urgent", None)      # only for the internal lock
    return fields
