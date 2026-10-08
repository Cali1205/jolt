"""Recalculate the charging plan during the trip - stage 3.

The optimizer from stage 2 plans from a start with a state of charge. That is
exactly what is available on the road, too: the current position is the
start, the reported state of charge is the state of charge. So it is not the
whole trip that is replanned, but the **remaining route** - and with what was
measured on the way rather than what was assumed before departure.

Two measured values go into it:

- The **consumption factor** scales the energy demand of the remaining route.
  Whoever has used 20 % more so far will hardly start driving economically
  for the next hundred kilometres: speed, load and weather remain what they
  are.
- The **time factor** scales the driving times. It is not the same thing: in
  a traffic jam consumption per kilometre rises by a few percent, but the
  driving time by a multiple. Without it, all arrival times of the new plan
  would be wrong.

Both are the most honest extrapolation available - not a forecast, just the
assumption that things stay as they were. That is precisely why the plan is
adjusted continuously instead of being calculated perfectly once.
"""
import logging

from .. import models
from ..energy import model, weather
from ..energy.model import VehicleValues, Environment
from ..geo import haversine_m
from ..charging import curves, optimizer, prices, availability
from ..routing import corridor

log = logging.getLogger("uvicorn.error")

# Defaults for the candidate search when the session does not bring any.
# They correspond to the defaults of the UI.
DEFAULTS = {"radius_km": 10.0, "min_kw": 50.0, "connector_type": "",
            "detour_limit_min": optimizer.DETOUR_LIMIT_MIN,
            "stop_fixed_cost_min": optimizer.STOP_FIXED_COST_MIN,
            "charge_park_bonus_min": optimizer.CHARGE_PARK_BONUS_MIN,
            "time_value_eur_h": optimizer.TIME_VALUE_EUR_H}


def read_parameter(plan: dict | None) -> dict:
    """The search parameters from a stored plan, otherwise the defaults.

    That way the search on the road uses the same radius and the same minimum
    power as at the start - a plan that also changes its selection criteria
    in the middle of the trip would no longer be traceable.
    """
    params = dict(DEFAULTS)
    for key in params:
        if plan and plan.get(key) is not None:
            params[key] = plan[key]
    return params


def rest_from(geometry: list, from_km: float) -> tuple[list, float]:
    """The route from a kilometre mark on, plus its actual start value.

    What is returned is the *support point* before `from_km` and its
    kilometre mark - not `from_km` itself. Only that way do geometry and
    profile afterwards refer to the same zero point; a shift between the two
    would be an offset in every leg calculation.
    """
    if len(geometry) < 2:
        return list(geometry), 0.0
    km = 0.0
    for i in range(1, len(geometry)):
        earlier = km
        km += haversine_m(geometry[i - 1][1], geometry[i - 1][0],
                          geometry[i][1], geometry[i][0]) / 1000.0
        if km >= from_km:
            return geometry[i - 1:], earlier
    # Already at the destination - the last edge remains, so that there is a
    # route at all.
    return geometry[-2:], km


def remaining_profile(energy_profile: list, from_km: float, consumption_factor: float = 1.0,
               time_factor: float = 1.0) -> optimizer.RouteProfile:
    """The route profile of the remaining route, zeroed and scaled."""
    rest = [e for e in energy_profile if (e.get("km") or 0.0) >= from_km]
    if len(rest) < 2:
        rest = energy_profile[-2:] if len(energy_profile) >= 2 else energy_profile
    if len(rest) < 2:
        return optimizer.RouteProfile(km=[], kwh=[], mins=[])

    km0 = rest[0].get("km") or 0.0
    kwh0 = rest[0].get("kwh") or 0.0
    min0 = rest[0].get("mins") or 0.0
    return optimizer.RouteProfile(
        km=[(e.get("km") or 0.0) - km0 for e in rest],
        kwh=[((e.get("kwh") or 0.0) - kwh0) * consumption_factor for e in rest],
        mins=[((e.get("mins") or 0.0) - min0) * time_factor for e in rest])


def environment_on_the_road(trip: models.Trip, points: list):
    """The weather for the remaining route - now, not at departure.

    Over eight hundred kilometres, winter regularly puts ten degrees and a
    different wind between start and destination, and this morning's forecast
    is no longer this morning's forecast by the afternoon. The query costs one
    call per replan, and replanning happens at most every ten kilometres.

    If it fails, the temperature of **this trip** applies and not the default
    of 15 °C: resetting a trip calculated at -5 °C to 15 °C would lose the
    heating load and make the remaining route cheaper on paper than it is -
    the error would point in exactly the direction in which it leaves
    someone stranded.
    """
    fallback = Environment()
    if trip.outside_temp_c is not None:
        fallback = Environment(temp_c=trip.outside_temp_c)
    try:
        return weather.along_route(points, preset=fallback)
    except Exception as failure:      # noqa: BLE001
        log.warning("Weather on the road not retrievable: %s", failure)
        return lambda lat, lon: fallback


def remaining_profile_physics(trip: models.Trip, rest: list, speed_factor: float,
                      environment_for) -> optimizer.RouteProfile | None:
    """Recalculate the remaining route with the **measured** speed.

    The difference to `remaining_profile` is that between scaling and
    calculating. Scaling takes the result of the planning and multiplies it;
    that is right as long as one has a measured consumption that one wants to
    extrapolate. But whoever only knows that they drive faster than assumed
    cannot turn that into an energy factor: air resistance goes with v²,
    rolling resistance almost linearly, the auxiliary loads not with speed at
    all but with time - and that *falls* when you drive faster. A flat markup
    would hit none of these three.

    That is why the model is run over the remaining route again here, with
    the same elevation profile and the same segment speeds as in the
    planning, just shifted by the measured factor. This works because the
    stored energy profile carries position, elevation and speed at each
    support point - it can be evaluated on its own and no longer needs the
    routing response.

    Returns None if the profile does not offer enough for that (trips from
    the time before these fields). The caller then falls back to scaling -
    a worse calculation is better than none.
    """
    if len(rest) < 2:
        return None
    points, speed_ms = [], []
    for i, entry in enumerate(rest):
        lat, lon = entry.get("lat"), entry.get("lon")
        if lat is None or lon is None:
            return None
        points.append([lon, lat, entry.get("elevation") or 0.0])
        # `speed_kmh` at a support point is the speed of the segment that
        # *ends there* - see model.compute_profile. For segment i (from i to
        # i+1) it is therefore found at point i+1.
        if i > 0:
            velocity = entry.get("speed_kmh")
            if not velocity:
                return None
            speed_ms.append(velocity / 3.6)

    flat_profile = model.compute_profile(
        VehicleValues.from_trip(trip), points, speed_ms,
        # The state of charge is irrelevant for the route profile: only the
        # cumulative kWh and minutes are needed, and the energy demand of a
        # leg does not depend on how full the battery is.
        start_soc=100.0, environment_for=environment_for, speed_factor=speed_factor)
    if len(flat_profile.points) < 2:
        return None
    return optimizer.RouteProfile(
        km=[p.km for p in flat_profile.points],
        kwh=[p.kwh_cumulative for p in flat_profile.points],
        mins=[p.minutes_cumulative for p in flat_profile.points])


def search_options(db, geometry: list, vehicle, parameter: dict
                    ) -> list[optimizer.ChargeOption]:
    """The charging options in the corridor of the (remaining) route."""
    kind = parameter.get("connector_type") or vehicle.connector_type
    candidates = corridor.seek(db, geometry,
                                 radius_km=parameter["radius_km"],
                                 min_kw=parameter["min_kw"], connector_type=kind)
    options = []
    for candidate in candidates:
        lp = candidate.charge_point
        state = availability.REPORTS.state(lp)
        options.append(optimizer.ChargeOption(
            id=lp.id, km_on_route=candidate.km_on_route,
            detour_minutes=candidate.detour_minutes, max_kw=lp.max_kw or 0.0,
            point_count=lp.point_count or 1, name=lp.name or "",
            operator=lp.operator or "", city=lp.city or "",
            lat=lp.lat, lon=lp.lon,
            locked=state.source == "meldung"))
    return options


def schedule(db, trip: models.Trip, from_km: float, start_soc: float,
           parameter: dict, consumption_factor: float = 1.0,
           time_factor: float = 1.0, speed_factor: float | None = None) -> dict:
    """A charging plan for the remaining route from `from_km` with `start_soc`.

    The kilometre marks in the result refer to the **whole** trip again, not
    to the remaining route: on the road you want to know that the stop is at
    km 412, and not at km 87 of the remaining route.

    `speed_factor` switches from scaling to recalculating: instead of
    multiplying the planned profile by an energy factor, the model is run
    over the remaining route again with the measured speed and the current
    weather. This is intended for the case that nobody has reported a state
    of charge yet - then there is no consumption factor, and the alternative
    would be to keep calculating with the slider value from before departure.

    Both together would be wrong: a measured consumption already contains the
    effect of speed. Whoever factors in the speed on top counts it twice.
    That is why it is either-or, and the caller decides.
    """
    vehicle = trip.vehicle
    geometry, km0 = rest_from(trip.geometry or [], from_km)

    profile = None
    basis = "verbrauch gemessen" if consumption_factor != 1.0 else "planung"
    if speed_factor is not None:
        rest = [e for e in (trip.energy_profile or [])
                if (e.get("km") or 0.0) >= km0]
        try:
            profile = remaining_profile_physics(trip, rest, speed_factor,
                                       environment_on_the_road(trip, geometry))
        except Exception as failure:      # noqa: BLE001
            # A failed recalculation must not cost us the replan - scaling
            # underneath is worse, but it stands.
            log.warning("Recalculation with measured speed failed: %s",
                        failure)
        if profile is not None:
            basis = "tempo gemessen"

    if profile is None:
        profile = remaining_profile(trip.energy_profile or [], km0, consumption_factor,
                            time_factor)

    if not profile.km or len(profile.km) < 2:
        return {**parameter, "feasible": False, "reading_km": round(from_km, 1),
                "reason": "Zur Reststrecke gibt es kein Profil mehr.",
                "stop_count": 0, "stops": []}

    options = search_options(db, geometry, vehicle, parameter)
    plan = optimizer.schedule(
        profile, options, VehicleValues.from_trip(trip),
        curves.as_pairs(vehicle.charge_curve), start_soc=start_soc,
        target_soc=vehicle.target_soc,
        max_vehicle_kw=vehicle.max_charge_power_kw,
        temperature_factor=curves.temperature_factor(
            trip.outside_temp_c if trip.outside_temp_c is not None else 15.0),
        detour_limit_min=parameter["detour_limit_min"],
        stop_fixed_cost_min=parameter["stop_fixed_cost_min"],
        charge_park_bonus_min=parameter["charge_park_bonus_min"],
        price_for=prices.price_function(vehicle),
        time_value_eur_h=parameter["time_value_eur_h"],
        preferred_operators=vehicle.preferred_operators or None,
        # So that the rationale names the same kilometres as the stops
        # below it - the optimizer calculates on the remaining route from
        # zero.
        km_offset=km0)

    result = plan.as_dict()
    for stop in result["stops"]:
        stop["km_on_route"] = round(stop["km_on_route"] + km0, 1)
        if stop.get("detour_alt"):
            stop["detour_alt"]["km_on_route"] = round(
                stop["detour_alt"]["km_on_route"] + km0, 1)
    return {**parameter, **result, "reading_km": round(km0, 1),
            # What the plan is based on - so that at the wheel and in the log
            # it is traceable whether a measurement is at work here or still
            # the slider from before departure.
            "basis": basis,
            "speed_factor": speed_factor}


def stops_same(old: dict | None, new_plan: dict | None) -> bool:
    """Do two plans describe the same stops?

    What is compared is location and departure state of charge, not the
    charging time to the decimal place: a standing time shifting by forty
    seconds is not a change anyone at the wheel wants to be informed about. A
    different location or a noticeably different charge swing is.
    """
    if old is None or new_plan is None:
        return old is new_plan
    if bool(old.get("feasible")) != bool(new_plan.get("feasible")):
        return False

    a = old.get("stops") or []
    b = new_plan.get("stops") or []
    if len(a) != len(b):
        return False
    for one, two in zip(a, b):
        if one.get("id") != two.get("id"):
            return False
        if abs((one.get("departure_soc") or 0) - (two.get("departure_soc") or 0)) > 3.0:
            return False
    return True


def describe_change(old: dict | None, new_plan: dict) -> str:
    """What has changed - in a sentence that works at the wheel.

    No diff and no list: whoever is driving can hear a sentence or read it in
    passing. Everything else is in the view.
    """
    if not new_plan.get("feasible"):
        return new_plan.get("reason") or "Kein Ladeplan mehr möglich."

    new_stops = new_plan.get("stops") or []
    old_stops = (old or {}).get("stops") or []
    if old is not None and not old.get("feasible"):
        return f"Wieder ein Plan möglich: {len(new_stops)} Ladestopp(s)."

    if not new_stops:
        return "Kein Ladestopp mehr nötig."

    first = new_stops[0]
    name = first.get("name") or first.get("operator") or "Ladepunkt"
    if len(new_stops) != len(old_stops):
        direction = "mehr" if len(new_stops) > len(old_stops) else "weniger"
        return (f"{len(new_stops)} Ladestopps statt {len(old_stops)} "
                f"({direction}) - nächster: {name} bei km "
                f"{first.get('km_on_route')}.")
    if old_stops and first.get("id") != old_stops[0].get("id"):
        return f"Nächster Ladestopp jetzt {name} bei km {first.get('km_on_route')}."
    return (f"{name}: laden bis {first.get('departure_soc')} % "
            f"({first.get('charge_time_minutes')} min).")
