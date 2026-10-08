"""Turn a route that was driven into a trip after the fact.

The reverse of planning: there the route is fixed beforehand and the trip is
held against it; here the car is driven, logged, and the route is created
afterwards from what the phone recorded.

**What for.** A vehicle's correction factor learns from the comparison of
forecast and reality. Having to plan a route for that is too cumbersome for
the most obvious case - a known short route, always the same, driven a few
times, is the cleanest measurement there is: no charging stop, identical
conditions, repeatable.

**Why a reconstruction is needed at all.** A consumption measurement without
an elevation profile cannot be interpreted. Whether 22 kWh/100 km was due to
driving style or to four hundred metres of climb cannot be separated from
the consumption alone - and whoever writes it into the correction factor
anyway teaches the vehicle the hill it happened to drive over.

That is why the elevation comes from map data (openrouteservice), not from
the GPS: its elevation reading scatters by ten to twenty metres, and summing
up such differences yields several hundred metres of climb for a trip across
flat land. For the *position*, GPS is accurate enough, for the elevation it
is not. The GPS elevation is recorded anyway - as a fallback if no key is
available, and because it costs nothing.

**And the speed?** That is the gain of this mode: it is not assumed but
calculated from the timestamps of the samples. A recorded trip knows its
speed for each segment exactly - a planned one has to estimate it.
"""
import logging

from .. import models, routing
from ..energy import model, weather
from ..energy.model import VehicleValues, Environment
from ..geo import haversine_m
from ..routing.corridor import point_on_route

log = logging.getLogger("uvicorn.error")

# Maximum number of support points for the elevation query. openrouteservice
# does not accept arbitrarily many, and finer than about every hundred metres
# does nothing for the elevation profile anyway.
AT_MOST_SUPPORT_POINTS = 1800

# Points that lie closer together are merged. A stationary car otherwise
# delivers hundreds of points on the same spot, and they distort every speed
# calculated from them.
MIN_DISTANCE_M = 25.0

# Window width over which GPS elevations are averaged. Five hundred metres is
# a compromise: narrow enough that a real motorway gradient remains (it
# stretches over kilometres), and wide enough that little of the
# high-frequency noise is left.
SMOOTHING_M = 500.0


def distance_build(points: list) -> list:
    """From the samples, a geometry [[lon, lat], ...].

    Everything closer together than `MIN_DISTANCE_M` is merged: at a traffic
    light or at the charger, dozens of points would otherwise stack up,
    yielding a speed of zero and a segment of length zero - both throw the
    calculation behind them off.
    """
    built: list = []
    last = None
    for point in points:
        if point.lat is None or point.lon is None:
            continue
        if last is not None:
            spacing = haversine_m(last.lat, last.lon, point.lat, point.lon)
            if spacing < MIN_DISTANCE_M:
                continue
        built.append(point)
        last = point
    return built


def _thin_out(points: list, at_most: int) -> list:
    if len(points) <= at_most:
        return points
    step = len(points) / at_most
    chosen = [points[int(i * step)] for i in range(at_most)]
    # The last point has to be included - otherwise the reconstructed route
    # ends before the destination and the balance is off.
    if chosen[-1] is not points[-1]:
        chosen.append(points[-1])
    return chosen


def gps_elevations_smooth(geometry: list, elevations: list) -> list:
    """Average GPS elevations over a stretch window.

    **Why this is necessary.** The consumption model is not interested in
    elevations but in elevation *differences* between consecutive points - and
    of those it sums up the positive ones. This very rectification is the
    catch: zero-mean noise turns into a systematic surcharge (the expected
    value of the positive part is about 0.4·σ), and that adds up linearly over
    the points, not with the square root. A 40 km trip has about 1600 support
    points at a minimum spacing of 25 m; even at σ = 5 m per difference, that
    adds up to kilometres of invented climb. At 2.5 t, 1000 m of climb is
    about 7 kWh - flat land would look like an Alpine stage, and that would go
    unchecked into the correction factor.

    **Why averaging helps.** The error of the GPS elevation has two parts. The
    slowly varying one (same satellite geometry over minutes) is the more
    harmless: it looks like a long hill, is wrong, but bounded. The
    high-frequency part, independent from measurement to measurement, is the
    one that feeds the rectification - and a moving average takes out exactly
    that. So the smoothing acts where the damage arises.

    Averaging is done over the **distance**, not over a number of points: the
    samples are dense in a traffic jam and far apart on the motorway, a window
    of twenty points would be 300 m wide at one time and 3 km at another.
    """
    if len(geometry) != len(elevations) or len(geometry) < 3:
        return list(elevations)

    # Running distance along the route - calculated once, after that the
    # window is a sliding of two edges.
    odometer_km = [0.0]
    for (lon1, lat1), (lon2, lat2) in zip(geometry, geometry[1:]):
        odometer_km.append(odometer_km[-1] + haversine_m(lat1, lon1, lat2, lon2))

    half = SMOOTHING_M / 2.0
    smoothed = []
    left_side = right = 0
    for i, middle in enumerate(odometer_km):
        while odometer_km[left_side] < middle - half:
            left_side += 1
        while right + 1 < len(odometer_km) and odometer_km[right + 1] <= middle + half:
            right += 1
        timeframe = [elevations[j] for j in range(left_side, right + 1)
                   if elevations[j] is not None]
        smoothed.append(sum(timeframe) / len(timeframe) if timeframe
                          else (elevations[i] or 0.0))
    return smoothed


def complete_elevations(geometry: list, gps_elevations: list | None = None
                     ) -> tuple[list, str]:
    """Turn [[lon, lat], ...] into [[lon, lat, elevation], ...].

    Returns the geometry **and the source**. The source is not a side issue:
    previously only the geometry was returned, and the caller guessed `karte`
    (map) from "some elevation is not zero". If the ORS query failed -
    exhausted quota, network hiccup - it silently slid to GPS and still
    reported `karte`. Afterwards you could not tell from a trip whether its
    elevations were any good.

    First choice is map data. Its error is similarly large in absolute terms
    as with GPS, but spatially correlated and **always the same**: the same
    road gets the same profile on every trip. For the purpose of the trip
    view - January against June, empty against loaded - an error that is the
    same on both trips simply cancels out.

    If the query fails, the smoothed GPS elevation applies (raw it is
    unusable, see `gps_elevations_smooth`), and if that is missing too, the
    calculation is done flat. A route calculated flat is explicitly **not** a
    disaster for the calibration, as long as start and destination are at the
    same height - over a closed loop the elevation cancels out anyway. For a
    trip into the mountains it is no good, and the log then says so as well.
    """
    try:
        with_elevation = routing.provider().elevations(geometry)
        if with_elevation:
            return with_elevation, "karte"
    except Exception as failure:      # noqa: BLE001
        log.warning("Elevation query failed: %s", failure)

    if gps_elevations and len(gps_elevations) == len(geometry) \
            and any(h is not None for h in gps_elevations):
        smoothed = gps_elevations_smooth(geometry, gps_elevations)
        log.info("Elevations from GPS, smoothed over %.0f m - less accurate "
                 "than map data.", SMOOTHING_M)
        return ([[lon, lat, elevation] for (lon, lat), elevation
                 in zip(geometry, smoothed)], "gps")

    log.warning("No elevation data - the route is calculated flat.")
    return [[lon, lat, 0.0] for lon, lat in geometry], "flach"


# From which driven distance on the vehicle's odometer may determine the
# distance. It resolves in whole kilometres: on a trip of four kilometres
# that is a quarter of uncertainty, on a hundred one percent.
ODOMETER_MIN_DISTANCE_KM = 5.0

# How far odometer and GPS distance may differ before the odometer is deemed
# not credible. Below 1.0 the GPS track would be longer than the distance
# driven - that can only be noise. Above 3.0 something else is wrong (a
# reading format, a vehicle change), and tripling a distance is too
# consequential to guess.
ODOMETER_LIMITS = (1.0, 3.0)


def odometer_factor(points: list, gps_km: float) -> tuple[float, dict]:
    """By how much the GPS track is too short - according to the car's odometer.

    **Why this is needed.** The distance of a recording is built from the
    samples, and they arrive every thirty seconds. At country-road speed
    there are four hundred metres in between, and the straight line cuts off
    every bend. With a dead-zone gap, a whole stretch is missing. Both make
    the distance too short - and because the measured consumption is
    calculated in kilowatt hours **per hundred kilometres**, the error goes
    straight into the vehicle's correction factor.

    The car knows better. Its odometer counts wheel revolutions and knows
    neither bends nor dead zones.

    What is returned is a factor on the **whole** distance, not per segment:
    the counter resolves in whole kilometres, and between two samples four
    hundred metres apart it jumps by zero or one. For the single segment it
    is therefore useless, for the sum over a trip exactly right.
    """
    as_of = [(p.raw_values or {}).get("odometer_km") for p in points]
    as_of = [k for k in as_of if isinstance(k, (int, float))]
    if len(as_of) < 2:
        return 1.0, {"reason": "weniger als zwei Ablesungen"}

    driven = as_of[-1] - as_of[0]
    if driven < ODOMETER_MIN_DISTANCE_KM:
        return 1.0, {"reason": f"nur {driven:g} km laut Zaehler - zu kurz "
                              f"fuer eine Aufloesung von einem Kilometer",
                     "odometer_km": driven}
    if gps_km <= 0:
        return 1.0, {"reason": "keine GPS-Strecke zum Vergleichen"}

    factor = driven / gps_km
    if not ODOMETER_LIMITS[0] <= factor <= ODOMETER_LIMITS[1]:
        log.warning("Odometer discarded: %.0f km per counter against "
                    "%.1f km from GPS (factor %.2f).", driven, gps_km,
                    factor)
        return 1.0, {"reason": f"Faktor {factor:.2f} ausserhalb der Grenzen",
                     "odometer_km": driven, "gps_km": round(gps_km, 1)}
    return factor, {"odometer_km": driven, "gps_km": round(gps_km, 1),
                    "factor": round(factor, 3)}


def speed_per_segment(points: list, distance_factor: float = 1.0) -> list:
    """Speed driven in m/s per segment, from the timestamps.

    `distance_factor` belongs here just as much as in the energy profile:
    whoever stretches the distance without stretching the speed along makes
    the model drive too slowly - and via v² it then predicts distinctly too
    little consumption.

    The real advantage of a recording: the planned trip has to assume the
    speed, the driven one knows it. Time jumps and standing times yield
    absurd values, hence the limits - below 2 m/s the model uses its own
    minimum value anyway, above 70 m/s (252 km/h) it was not a car but a
    broken clock.
    """
    speeds = []
    for earlier, after in zip(points, points[1:]):
        distance = haversine_m(earlier.lat, earlier.lon,
                              after.lat, after.lon) * distance_factor
        duration = 0.0
        if earlier.timestamp and after.timestamp:
            duration = (after.timestamp - earlier.timestamp).total_seconds()
        speeds.append(min(70.0, max(2.0, distance / duration)) if duration > 0 else 25.0)
    return speeds


def determine_environment(points: list, geometry: list):
    """The weather of the trip - measured, if it was measured.

    A logger on the OBD2 port delivers the vehicle's outside temperature. It
    is superior to any forecast: it comes from the route, at the right time,
    and it is the largest single item of the cold. Only if none came along is
    a query made - and then the weather service delivers the weather of
    *now*, not that of the trip.
    """
    measured = [p.outside_temp_c for p in points if p.outside_temp_c is not None]
    if measured:
        avg = sum(measured) / len(measured)
        log.info("Recording: measured outside temperature %.1f °C", avg)
        return lambda lat, lon: Environment(temp_c=avg), avg

    fetch = weather.along_route(geometry)
    avg = weather.mean(geometry).temp_c
    return fetch, avg


def complete(db, trip: models.Trip, session: models.LiveSession) -> dict:
    """Build geometry and energy profile from the samples of a session.

    After that the recording is a trip like any other: it has a route, an
    elevation profile and a forecast against which the measured consumption
    can be held. Only then can `energy/calibration.py` learn anything at all -
    it compares `plan_soc` with `soc`, and both are only settled now.
    """
    raw = distance_build(list(session.points))
    if len(raw) < 2:
        return {"ok": False, "reason": "Zu wenige Messpunkte für eine Strecke."}

    chosen = _thin_out(raw, AT_MOST_SUPPORT_POINTS)
    flat = [[p.lon, p.lat] for p in chosen]
    gps_elevations = [(p.raw_values or {}).get("elevation_m") for p in chosen]
    geometry, elevations_source = complete_elevations(flat, gps_elevations)

    fetch_environment, avg_temp = determine_environment(chosen, flat)

    # What the GPS yields - and what the car says about it.
    gps_km = sum(haversine_m(a.lat, a.lon, b.lat, b.lon)
                 for a, b in zip(chosen, chosen[1:])) / 1000.0
    factor, odo = odometer_factor(list(session.points), gps_km)
    if factor != 1.0:
        log.info("Route stretched per odometer: %.1f km from GPS "
                 "-> %g km per counter (factor %.3f).",
                 gps_km, odo.get("odometer_km"), factor)

    profile = model.compute_profile(
        VehicleValues.from_trip(trip), geometry,
        speed_per_segment(chosen, factor),
        start_soc=chosen[0].soc if chosen[0].soc is not None else 100.0,
        environment_for=fetch_environment, distance_factor=factor,
        speed_cap=False)
    if len(profile.points) < 2:
        return {"ok": False, "reason": "Aus der Strecke entstand kein Profil."}

    trip.geometry = geometry
    trip.energy_profile = [p.as_dict() for p in profile.points]
    trip.distance_m = profile.distance_km * 1000.0
    trip.drive_time_s = profile.mins * 60.0
    trip.outside_temp_c = round(avg_temp, 1)
    trip.start_lat, trip.start_lon = chosen[0].lat, chosen[0].lon
    trip.target_lat, trip.target_lon = chosen[-1].lat, chosen[-1].lon
    # When it was created, this said "unterwegs" (on the way) - the
    # destination was still unknown then. Now it is known, but it has no name:
    # jolt can search for places, but cannot turn a coordinate into a place
    # name the other way round. Leaving the placeholder word was the worst of
    # the options - the trip list would permanently have said "Aufzeichnung →
    # unterwegs", i.e. a claim about a trip that is long over. Empty honestly
    # means "no place name" here; the list then shows the name of the
    # recording alone.
    if (trip.target_text or "") == "unterwegs":
        trip.target_text = ""
    if chosen[0].soc is not None:
        trip.start_soc = chosen[0].soc

    # So far the samples carry neither odometer reading nor target value - on
    # arrival there was no route to put them on. Without this the calibration
    # finds nothing usable.
    for point in session.points:
        km, _ = point_on_route(geometry, point.lat, point.lon)
        point.km_on_route = km
        point.plan_soc = _plan_at(trip.energy_profile, km)

    db.flush()
    return {"ok": True, "distance_km": round(profile.distance_km, 1),
            "drive_time_minutes": round(profile.mins),
            "consumption_kwh": round(profile.kwh_total, 2),
            "outside_temp_c": trip.outside_temp_c,
            "elevations": elevations_source,
            # Where the route comes from - one corrected from the odometer is
            # something different from a pure GPS track, and the trip should
            # show it.
            "distance_source": "kilometerstand" if factor != 1.0 else "gps",
            "odometer": odo}


def _plan_at(energy_profile: list, km: float):
    # Deliberately here and not via energy/profile.py: the profile has only
    # just been created and exists as a list of dicts, not on the trip object.
    from ..energy.profile import soc_at
    return soc_at(energy_profile, km)
