"""Compute a route: distance, elevation profile, weather, energy demand, charging stops.

The endpoint that brings everything together: the route from the routing
service, the energy demand from the consumption model and - via `/ladeplan`
(charge plan) - the time-optimal sequence of charging stops from the
optimizer.

`/route` does not compute one variant but several: the fastest road, a
toll-free one on request, and up to four detour routes via waypoints next to
the route (`routing/variants.py`).

The waypoints are not an end in themselves. openrouteservice does not know
energy as an edge weight - "consumption-optimal" cannot be ordered there,
only a routing layer of our own could do that (see konzept-routenplaner.md).
And its built-in `alternative_routes` rejects every route over 100 km, i.e.
exactly those where an alternative would change something. What remains:
generate candidates ourselves and evaluate them with jolt's own consumption
model.

Evaluation is based on the **finished charging plan** and not on the travel
time - see `_variants_rate` (rate variants). A route that is longer
*and* slower than the fastest is discarded beforehand: it cannot have a
charging plan that saves it, and computing costs more than weeding out.

The charging plan is deliberately attached to an already computed trip and
not to the route request: radius, minimum power and connector type are
meant to be tried out without burdening the routing quota every time.
"""
import logging
from datetime import datetime, timedelta, timezone

from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import deps, models, routing
from ..database import get_db
from ..energy import model, weather
from ..geo import haversine_m
from ..timestamp import utc_iso
# Only for the default values of the sliders - calculation goes through
# `umplanung.planen` (replanning.schedule), which calls the optimizer itself.
from ..charging import optimizer
from ..live import replanning
from ..routing import own, tomtom, variants
from ..routing.provider import RoutingError

# From the ORS "preference" to the label that the human at the wheel reads.
# "empfohlen" (recommended) instead of "recommended", because nobody uses
# that word who is not an openrouteservice customer themselves.
LABEL = {"fastest": "schnellste", "shortest": "kürzeste",
          "recommended": "empfohlene"}
# How close two variants must be in distance and travel time to count as "the
# same route". A tolerance of one kilometer and one minute absorbs rounding
# differences between the ORS responses without wrongly merging two
# genuinely different routes.
SAME_KM = 1.0
SAME_MIN = 1.0

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api", tags=["route"],
                   dependencies=[Depends(deps.current_session)])


class Point(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    text: str = ""


class Routenanfrage(BaseModel):
    vehicle_id: int
    start: Point
    destination: Point
    start_soc: float = Field(default=80.0, ge=0, le=100)
    # 1.1 means "ten percent faster than the routing assumes". The slider
    # acts disproportionately via v² - exactly the lever with which a
    # charging stop can be saved on the road.
    speed_factor: float = Field(default=1.0, ge=0.6, le=1.5)
    # Surcharge on air drag for a bike rack or roof box.
    # 1.0 = nothing attached. See models.Fahrt.air_drag_factor.
    air_drag_factor: float = Field(default=1.0, ge=1.0, le=2.0)
    consider_weather: bool = True
    # Also compute a second route. Off: it stays with the fastest.
    #
    # "shortest" is deliberately and completely absent here. On the route
    # Le Gurp - Montchanin it delivers 554 km in 11.8 hours versus 654 km in
    # 6.3 - a hundred kilometers less, bought with five and a half hours.
    # Nobody decides that way, and a choice in which one option is never
    # picked only makes the choice confusing. It also costs a third of the
    # ORS daily quota.
    alternative: bool = False
    # Also compute detour routes via waypoints (routing/variants.py).
    #
    # Off by default, and that is a measurement result and not caution: on
    # the test route each of the twelve geometric candidates computed lost
    # against the fastest route. Four additional routing requests per plan
    # for a suggestion that is reliably worse would be a bad deal.
    #
    # On: for experiments with other pick-off points and offset widths. The
    # mechanism behind it is sound - it is only waiting for a candidate
    # supplier that is any good.
    examine_detours: bool = False
    # Driven routes as additional candidates (routing/own.py): whoever has
    # driven a route before knows a way that no edge weight knows. At most
    # two more requests are made, and only if an earlier trip fits the start
    # and destination at all.
    own_trips: bool = True
    # TomTom as an advisor (routing/tomtom.py): suggestions that
    # OpenRouteService does not deliver, and the traffic delay per route.
    # Without TOMTOM_API_KEY nothing happens. None of it is stored.
    tomtom: bool = True
    # When to depart. Empty means now. Traffic (TomTom, forecast
    # time-dependently) and weather (hourly forecast, per vertex for the hour
    # of arrival there) then apply to that time. With time zone; the browser
    # sends UTC.
    departure: datetime | None = None
    # Payload of this one trip. None means "as in the vehicle profile" - the
    # normal case. It is set when the same trip is planned once with two
    # people and once fully loaded: mass enters linearly into rolling and
    # gradient resistance, on a mountain route a 600 kg difference is much
    # more than cosmetics.
    payload_kg: float | None = Field(default=None, ge=0, le=2000)
    # Trailer of this trip: mass and additional drag area (c_w times A, in
    # m²). See models.Fahrt.trailer_kg.
    trailer_kg: float | None = Field(default=None, ge=0, le=3500)
    trailer_cwa_m2: float | None = Field(default=None, ge=0, le=5)
    # Hard top speed of this trip in km/h, e.g. 100 for a car-and-trailer
    # combination. Applies in addition to the vehicle's; the smaller one wins.
    speed_max_kmh: float | None = Field(default=None, ge=30, le=250)


@router.get("/orte")
def search_places(text: str = Query(min_length=2), country: str = ""):
    try:
        hit = routing.provider().seek(text, country)
    except RoutingError as failure:
        raise HTTPException(502, str(failure)) from failure
    return {"demo": routing.is_demo(),
            "hit": [{"name": o.name, "lat": o.lat, "lon": o.lon}
                        for o in hit]}


# A departure that lies back at most this far is "now": typing the time into
# the form takes a while.
DEPARTURE_TOLERANCE = timedelta(minutes=10)
# This far ahead TomTom knows road closures and construction sites; beyond
# that there would only be the usual traffic, and the weather only reaches
# 15 days anyway.
DEPARTURE_MAX = timedelta(days=60)


def _examine_departure(request: Routenanfrage) -> datetime | None:
    """The departure as a point in time with time zone - or None for "now".

    Past and distant dates are a typo and are rejected instead of silently
    calculating with "now": whoever plans for Friday and hits Monday should
    notice before trusting the numbers.
    """
    departure = request.departure
    if departure is None:
        return None
    if departure.tzinfo is None:
        departure = departure.replace(tzinfo=timezone.utc)
    now_ts = datetime.now(timezone.utc)
    if departure < now_ts - DEPARTURE_TOLERANCE:
        raise HTTPException(422, "Die Abfahrt liegt in der Vergangenheit.")
    if departure > now_ts + DEPARTURE_MAX:
        raise HTTPException(422, "Die Abfahrt liegt mehr als 60 Tage in der "
                                 "Zukunft - so weit reicht keine Prognose.")
    if departure <= now_ts + DEPARTURE_TOLERANCE:
        return None
    return departure


@router.post("/route")
def compute_route(request: Routenanfrage, db: Session = Depends(get_db)):
    vehicle = db.get(models.Vehicle, request.vehicle_id)
    if not vehicle:
        raise HTTPException(404, "Fahrzeug nicht gefunden.")

    # Only for this calculation, not stored on the vehicle: payload and air
    # drag surcharge are properties of the trip, not of the car.
    # `from_trip` (from_trip) expects a trip-like object; the trip only comes
    # into being in `_save_trips` (save trips), hence a lightweight
    # placeholder.
    vals = model.VehicleValues.from_trip(SimpleNamespace(
        vehicle=vehicle, payload_kg=request.payload_kg,
        air_drag_factor=request.air_drag_factor,
        trailer_kg=request.trailer_kg,
        trailer_cwa_m2=request.trailer_cwa_m2,
        speed_max_kmh=request.speed_max_kmh))

    departure = _examine_departure(request)
    groups = _distances_collect(request, db, departure)
    candidates = _compute_candidates(request, vals, groups, departure)
    results = _save_trips(db, request, vehicle, candidates)

    # Before the rating: traffic belongs in the ranking.
    _fetch_traffic(db, request, results, departure)
    _variants_rate(db, results, vehicle)
    return {"variants": results,
            "departure": departure.isoformat() if departure else None}


# At most this many driven routes are followed up as candidates. Each costs
# one routing request from the daily quota, and more than the last two rarely
# bring anything new.
MAX_OWN = 2


def _own_routes(db: Session, start: tuple, destination: tuple) -> list[dict]:
    """Routes based on earlier trips that fit the start and destination.

    First a cheap query for the bounding rectangle per session (one row per
    session, not per measurement point); only sessions whose rectangle
    encloses start **and** destination are loaded at all. Newest first: the
    road one drove most recently is the one that still exists.
    """
    straight_line_km = haversine_m(start[0], start[1], destination[0], destination[1]) / 1000.0
    if straight_line_km < own.RADIUS_MIN_KM:
        return []
    edge = own.radius_km(straight_line_km) + 5.0
    # One degree of longitude is less than 55 km north of 60°; calculating
    # with 55 makes the rectangle too large rather than too small - and too
    # large only costs a few more rows.
    edge_degree = edge / 55.0

    box = (db.query(models.LivePoint.session_id,
                       func.min(models.LivePoint.lat), func.max(models.LivePoint.lat),
                       func.min(models.LivePoint.lon), func.max(models.LivePoint.lon),
                       func.count(models.LivePoint.id))
              .group_by(models.LivePoint.session_id)
              .having(func.count(models.LivePoint.id) >= 20)
              .order_by(models.LivePoint.session_id.desc()).all())

    def inside(point, lat0, lat1, lon0, lon1) -> bool:
        return (lat0 - edge_degree <= point[0] <= lat1 + edge_degree
                and lon0 - edge_degree <= point[1] <= lon1 + edge_degree)

    routes: list[dict] = []
    for session_id, lat0, lat1, lon0, lon1, _ in box:
        if len(routes) >= MAX_OWN:
            break
        if not (inside(start, lat0, lat1, lon0, lon1)
                and inside(destination, lat0, lat1, lon0, lon1)):
            continue
        rows = (db.query(models.LivePoint.lat, models.LivePoint.lon,
                           models.LivePoint.speed_kmh)
                  .filter(models.LivePoint.session_id == session_id)
                  .order_by(models.LivePoint.timestamp).all())
        section = own.fitting_section(
            own.path_from_samples(rows), start, destination)
        if section is None:
            continue
        between = own.waypoints(section)
        if not between:
            continue
        session = db.get(models.LiveSession, session_id)
        date = session.started_at.strftime("%d.%m.%Y") if session else "?"
        label = f"meine Strecke vom {date}"
        if section.opposite:
            label += " (Gegenrichtung)"
        log.info("Own route from session %s: %d waypoints, %.0f km "
                 "path (distance start %.1f km, destination %.1f km).", session_id,
                 len(between), section.length_km,
                 section.spacing_start_km, section.spacing_target_km)
        routes.append({"between": between, "toll_free": False,
                     "label": label})
    return routes


def _tomtom_routes(start: tuple, destination: tuple,
                 departure: datetime | None = None) -> list[dict]:
    """Routes based on TomTom's suggestions that are not overtaken.

    TomTom only supplies the template: waypoints are chosen from the
    suggestion, and the OpenRouteService routing drives through them. What is
    stored is its road with elevation and speed, not TomTom's - if only
    because the consumption model needs both, and because TomTom's terms do
    not allow storing their results.

    If TomTom fails (key, quota, network), planning continues without it. An
    advisor that crashes the planning would be worse than none.
    """
    if not tomtom.obtainable():
        return []
    try:
        suggestions = tomtom.alternativen(start, destination, departure=departure)
    except tomtom.TomTomError as failure:
        log.warning("TomTom: %s Planning continues without it.", failure)
        return []

    routes: list[dict] = []
    for nr, v in enumerate(tomtom.not_overtaken(suggestions), 1):
        section = own.Section(
            points=[(lat, lon, None) for lat, lon in v.points],
            opposite=False, spacing_start_km=0.0, spacing_target_km=0.0,
            length_km=v.distance_m / 1000.0)
        between = own.waypoints(section)
        if not between:
            continue
        # No coordinates in the log: they are TomTom results.
        log.info("TomTom suggestion %d: %d waypoints (%.0f km, %.0f min "
                 "at TomTom).", nr, len(between), v.distance_m / 1000,
                 v.time_s / 60)
        routes.append({"between": between, "toll_free": False,
                     "label": f"TomTom-Vorschlag {nr}"})
    return routes


def _fetch_traffic(db: Session, request: Routenanfrage, results: list,
                   departure: datetime | None = None) -> None:
    """The traffic delay per route - as a number in the response, nowhere else.

    TomTom is asked about the route jolt drives, not about its own:
    waypoints are chosen from the stored geometry that force TomTom onto the
    same road. The delay is then attached to the variant and flows into
    `_variants_rate` (rate variants); it does not go into the database.
    """
    if not (request.tomtom and tomtom.obtainable()):
        return
    start = (request.start.lat, request.start.lon)
    destination = (request.destination.lat, request.destination.lon)
    for variant in results:
        trip = db.get(models.Trip, variant["trip_id"])
        geometry = (trip.geometry or []) if trip else []
        if len(geometry) < 3:
            continue
        between = own.waypoints(own.Section(
            points=[(p[1], p[0], None) for p in geometry],
            opposite=False, spacing_start_km=0.0, spacing_target_km=0.0,
            length_km=(trip.distance_m or 0.0) / 1000.0))
        try:
            result = tomtom.traffic(start, destination, between, departure=departure)
        except tomtom.TomTomError as failure:
            # The same error would hit the remaining requests too.
            log.warning("TomTom traffic: %s The ranking applies without it.", failure)
            return
        if result is not None:
            variant["traffic_min"] = round(result.delay_s / 60.0, 1)
            variant["traffic_source"] = "TomTom"
            # Live or forecast time-dependently - the UI says which.
            variant["traffic_basis"] = "prognose" if departure else "live"


def _routes_plan(request: Routenanfrage, start: tuple, destination: tuple,
                 db: Session | None = None,
                 departure: datetime | None = None) -> list[dict]:
    """Which routing requests are made - each costs from the daily quota.

    The first is the fastest road and at the same time the yardstick;
    everything else has to measure up against it.

    `recommended` and `shortest` are deliberately not included: the former
    delivers the same road as `fastest` on motorway stretches, the latter one
    that nobody drives (477 km in 10.7 hours versus 598 km in 5.6). Both
    measured, see routing/variants.py.
    """
    routes = [{"between": [], "toll_free": False, "label": LABEL["fastest"]}]
    if request.alternative:
        routes.append({"between": [], "toll_free": True, "label": "toll_free"})
    if request.examine_detours:
        routes += [{"between": [k["point"]], "toll_free": False,
                  "label": k["label"]}
                 for k in variants.alternative_points(start, destination)]
    if request.own_trips and db is not None:
        routes += _own_routes(db, start, destination)
    if request.tomtom:
        routes += _tomtom_routes(start, destination, departure)
    return routes


def _distances_collect(request: Routenanfrage, db: Session | None = None,
                      departure: datetime | None = None) -> list[dict]:
    """Step 1: query the routes and merge what is the same road.

    Deliberately before weather and consumption model - those are the
    expensive part, and in demo mode as often in reality (shorter routes
    usually have only one sensible way) several presets end up on the same
    route anyway.

    Returns: one entry per genuinely different route, with the labels of all
    paths that led to it, and the route itself.
    """
    vendor = routing.provider()
    start = (request.start.lat, request.start.lon)
    destination = (request.destination.lat, request.destination.lon)

    groups: list[dict] = []
    last_error: RoutingError | None = None
    basis = None
    for path in _routes_plan(request, start, destination, db, departure):
        try:
            distance = vendor.route(start, destination,
                                     intermediate_stops=path["between"] or None,
                                     preference="fastest",
                                     toll_free=path["toll_free"])
        except RoutingError as failure:
            # A route that fails must not drag the others down - only if none
            # is left in the end has the request failed. A waypoint can well
            # end up in water or in a restricted area; that is no reason not
            # to deliver the route.
            last_error = failure
            continue
        if len(distance.points) < 2:
            continue

        if basis is None:
            basis = distance

        # Merge before weeding out: `actual_dominated` (is_dominated) also counts
        # a route of equal length and speed, and that one would otherwise be
        # discarded before its label lands on the route that already exists -
        # the toll-free route would then vanish without a trace although it
        # is exactly the same road.
        fitting = next((g for g in groups
                        if abs(g["distance"].distance_m - distance.distance_m)
                        <= SAME_KM * 1000
                        and abs(g["distance"].drive_time_s - distance.drive_time_s)
                        <= SAME_MIN * 60), None)
        if fitting:
            if path["label"] not in fitting["labels"]:
                fitting["labels"].append(path["label"])
            continue

        if distance is not basis and variants.actual_dominated(
                distance.distance_m, distance.drive_time_s,
                basis.distance_m, basis.drive_time_s):
            # Longer *and* slower than the fastest route: the candidate cannot
            # have a charging plan that saves it. Weeding it out here saves
            # the weather query, consumption profile and charge planning -
            # the expensive part. The routing request has already been paid
            # for by then.
            log.info("Detour route '%s' discarded: %.0f km/%.0f min versus "
                     "%.0f km/%.0f min of the fastest.", path["label"],
                     distance.distance_m / 1000, distance.drive_time_s / 60,
                     basis.distance_m / 1000, basis.drive_time_s / 60)
            continue

        groups.append({"labels": [path["label"]], "distance": distance})

    if not groups:
        if last_error:
            raise HTTPException(502, str(last_error)) from last_error
        raise HTTPException(502, "Route enthält zu wenige Punkte.")
    return groups


def _cap_factor(profile) -> float:
    """By how much the speed cap stretches the travel time, at least 1."""
    if profile.minutes_without_cap > 0 and profile.mins > 0:
        return max(1.0, profile.mins / profile.minutes_without_cap)
    return 1.0


def _compute_candidates(request: Routenanfrage, vals, groups: list[dict],
                        departure: datetime | None = None) -> list[dict]:
    """Step 2: for each genuinely different route - and only for those -
    compute weather and consumption model."""
    candidates: list[dict] = []
    for group in groups:
        distance = group["distance"]
        # Thin out to roughly one point per 250 m. On a long-distance route
        # the routing delivers five-digit numbers of vertices - for map and
        # forecast that is computing time without insight. Elevation jumps
        # are preserved in the process.
        points, velocity = model.thin_out(distance.points, distance.speed_ms)

        if request.consider_weather:
            # For the departure time, not for now: a trip tomorrow morning
            # should not be calculated with this afternoon's weather.
            environment_for = weather.along_route(
                points, departure=departure, duration_s=distance.drive_time_s)
            avg = weather.mean(points, departure=departure,
                                       duration_s=distance.drive_time_s)
        else:
            environment_for = None
            avg = model.Environment()

        profile = model.compute_profile(vals, points, velocity, request.start_soc,
                                       environment_for, request.speed_factor)
        candidates.append({
            "labels": group["labels"],
            "distance_km": distance.distance_m / 1000.0 if distance.distance_m
                else profile.distance_km,
            # Travel time: the openrouteservice figure is the realistic basis
            # - it knows intersections, roundabouts and town crossings that
            # the consumption model does not. Only **it** does not know the
            # speed slider, and the slider shifts it linearly: whoever drives
            # ten percent faster needs one eleventh less time.
            #
            # Without this division the travel time stood unchanged no matter
            # where the slider was - while consumption and charging plan below
            # did change. Two different times for the same trip on the same
            # screen.
            #
            # With a speed cap a third item comes in: what the cap cuts off
            # costs time. `profil` (profile) knows both times, with and
            # without the cap; their ratio stretches the routing's time (1.0
            # as long as no cap applies).
            "drive_time_min": (distance.drive_time_s / 60.0 / request.speed_factor
                             * _cap_factor(profile))
                if distance.drive_time_s else profile.mins,
            "points": points, "profile": profile, "avg": avg})
    return candidates


def _save_trips(db: Session, request: Routenanfrage, vehicle,
                       candidates: list[dict]) -> list[dict]:
    """Step 3: create a trip per candidate and build the response."""
    results = []
    for candidate in candidates:
        trip = models.Trip(
            vehicle_id=vehicle.id,
            start_text=request.start.text, start_lat=request.start.lat,
            start_lon=request.start.lon, target_text=request.destination.text,
            target_lat=request.destination.lat, target_lon=request.destination.lon,
            start_soc=request.start_soc, speed_factor=request.speed_factor,
            outside_temp_c=candidate["avg"].temp_c,
            payload_kg=request.payload_kg,
            air_drag_factor=request.air_drag_factor,
            trailer_kg=request.trailer_kg,
            trailer_cwa_m2=request.trailer_cwa_m2,
            speed_max_kmh=request.speed_max_kmh,
            distance_m=candidate["distance_km"] * 1000,
            drive_time_s=candidate["drive_time_min"] * 60,
            geometry=candidate["points"],
            energy_profile=[p.as_dict() for p in candidate["profile"].points])
        db.add(trip)
        db.flush()      # needs fahrt.id (trip id) without committing for good yet
        results.append({"trip_id": trip.id,
                           "labels": candidate["labels"],
                           **_response(trip, candidate["profile"],
                                      candidate["avg"], vehicle)})
    db.commit()
    return results


def _variants_rate(db, results: list, vehicle) -> None:
    """Measure the variants against the **finished charging plan**, not the
    travel time.

    This is the question that matters for an electric car and that nobody
    else answers: not "which road is shorter", but "where am I sooner when
    charging counts". A route with a hundred kilometers of detour can win if
    the stronger chargers are on it - and a frugal country road loses
    although it needs less energy.

    Previously the lowest-energy variant carried the label "sparsamste"
    (most economical). That was misleading: on Le Gurp - Montchanin it
    singled out the route that, at 47 km/h average, needs 17 instead of 27
    kWh/100 km, but takes five and a half hours longer. Economical it was,
    sensible it was not.

    The calculation uses the default values for radius and minimum power -
    the UI sliders apply to the charging plan below. That is harmless
    because **all** variants get the same treatment: for a comparison the
    yardstick counts, not its zero point.
    """
    if len(results) < 2:
        return
    parameter = dict(replanning.DEFAULTS)
    for variant in results:
        trip = db.get(models.Trip, variant["trip_id"])
        try:
            plan = replanning.schedule(db, trip, 0.0, trip.start_soc, parameter)
        except Exception as failure:      # noqa: BLE001
            # Without a plan the variant remains selectable - it just carries
            # no rating. Discarding a route because its charging plan cannot
            # be computed would be the wrong reaction.
            log.warning("Variant %s cannot be planned: %s", variant["trip_id"],
                        failure)
            continue
        if not plan.get("feasible"):
            variant["plan_feasible"] = False
            continue
        variant.update({
            "plan_feasible": True,
            "plan_stops": plan.get("stop_count"),
            "plan_total_minutes": plan.get("total_minutes"),
            "plan_cost_eur": plan.get("cost_eur")})

    rated = [v for v in results if v.get("plan_feasible")]
    # Traffic is part of time: a route that is two minutes faster on paper
    # but sits twelve in a jam is not the fastest.
    def total(v: dict) -> float:
        return v["plan_total_minutes"] + (v.get("traffic_min") or 0.0)

    for v in rated:
        if v.get("traffic_min") is not None:
            v["plan_total_with_traffic_min"] = round(total(v))
    if rated:
        min(rated, key=total)["labels"].append("insgesamt schnellste")
        cheapest = min(rated, key=lambda v: v["plan_cost_eur"])
        if "insgesamt schnellste" not in cheapest["labels"]:
            cheapest["labels"].append("günstigste")

    # Order for the eye: the overall fastest first - it is the answer to the
    # question jolt is meant to answer.
    results.sort(key=lambda v: "insgesamt schnellste" not in v["labels"])


@router.get("/fahrten/{trip_id}")
def read_trip(trip_id: int, db: Session = Depends(get_db)):
    """A stored trip - in the same shape as a fresh variant.

    The derived values (consumption, reserve point, "is it enough?") are
    recomputed from the stored energy profile instead of being stored along:
    they are functions of the profile, and two sources for the same number
    drift apart. The shape deliberately matches that of `/api/route`, so the
    UI draws an old trip with the same code as a new one.
    """
    trip = db.get(models.Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Fahrt nicht gefunden.")

    profile = trip.energy_profile or []
    reserve_soc = trip.vehicle.reserve_soc
    distance_km = round((trip.distance_m or 0) / 1000.0, 1)
    kwh_total = profile[-1].get("kwh") if profile else None

    reserve_at_km, reserve_point = None, None
    for entry in profile:
        if entry.get("soc") is not None and entry["soc"] <= reserve_soc:
            reserve_at_km = entry.get("km")
            reserve_point = {"km": entry.get("km"), "lat": entry.get("lat"),
                             "lon": entry.get("lon")}
            break

    return {"trip_id": trip.id, "demo": routing.is_demo(),
            "start": {"lat": trip.start_lat, "lon": trip.start_lon,
                      "text": trip.start_text},
            "destination": {"lat": trip.target_lat, "lon": trip.target_lon,
                     "text": trip.target_text},
            "vehicle": {"id": trip.vehicle.id, "name": trip.vehicle.name,
                         "reserve_soc": reserve_soc},
            "start_soc": trip.start_soc, "speed_factor": trip.speed_factor,
            "outside_temp_c": trip.outside_temp_c, "payload_kg": trip.payload_kg,
            "trailer_kg": trip.trailer_kg,
            "trailer_cwa_m2": trip.trailer_cwa_m2,
            "speed_max_kmh": trip.speed_max_kmh,
            "distance_km": distance_km,
            "drive_time_minutes": round((trip.drive_time_s or 0) / 60.0),
            "kwh_total": round(kwh_total, 3) if kwh_total is not None else None,
            "consumption_kwh_100km": (round(kwh_total / distance_km * 100.0, 2)
                                    if kwh_total and distance_km else None),
            "reserve_at_km": reserve_at_km,
            "reserve_point": reserve_point,
            "suffices": reserve_at_km is None,
            # The wind of the trip back then is not stored - only the
            # temperature that was used for the calculation. It is the number
            # shown in the UI ("gerechnet bei 4 °C", i.e. calculated at 4 °C).
            "weather": {"temp_c": trip.outside_temp_c},
            "geometry": trip.geometry or [],
            "profile": _thin_out_profile(profile),
            "soc_at_target": profile[-1]["soc"] if profile else None}


@router.post("/fahrten/{trip_id}/ladeplan")
def compute_charge_plan(trip_id: int, radius_km: float = Query(8.0, gt=0, le=50),
                     min_kw: float = Query(50.0, ge=0),
                     connector_type: str = "",
                     detour_limit_min: float = Query(
                         optimizer.DETOUR_LIMIT_MIN, gt=0, le=60),
                     # Zero is allowed, but the consequence is stated in the
                     # UI text: without fixed costs per stop the plan
                     # fragments into many short stops. Whoever wants to see
                     # that should be able to see it.
                     stop_fixed_cost_min: float = Query(
                         optimizer.STOP_FIXED_COST_MIN, ge=0, le=30),
                     # What a large charging park is worth, in minutes.
                     # Zero means "only time counts".
                     charge_park_bonus_min: float = Query(
                         optimizer.CHARGE_PARK_BONUS_MIN, ge=0, le=15),
                     # What an hour is worth. Zero means "I don't care about
                     # costs" - then it optimizes purely for time.
                     time_value_eur_h: float = Query(
                         optimizer.TIME_VALUE_EUR_H, ge=0, le=200),
                     db: Session = Depends(get_db)):
    """The time-optimal sequence of charging stops for a computed trip.

    Calculation is done on the stored energy profile - the demand of a leg
    does not depend on the state of charge, so one pass of the consumption
    model from `/route` is enough. A second call with a different radius thus
    costs neither routing nor weather requests.

    **Via `umplanung.planen` (replanning.schedule) and not past the
    optimizer.** The same chain used to be spelled out here once more:
    search candidates in the corridor, translate them into charging options,
    call the optimizer with thirteen arguments. The block that translates
    candidates was byte-for-byte the same as in `live/replanning.py`, and the
    call had to be maintained twice - with the most recently added
    `km_offset` (km offset) that promptly went wrong, it was only in one of
    the two versions.

    That it was duplication and not intent was shown by this router itself:
    for rating the route variants it already called `umplanung.planen`. A
    plan from km 0 with the starting state of charge is the same process as a
    re-plan on the road, just without a distance already covered.
    """
    trip = db.get(models.Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Fahrt nicht gefunden.")
    if not trip.energy_profile or len(trip.energy_profile) < 2:
        raise HTTPException(409, "Zu dieser Fahrt liegt kein Energieprofil vor.")

    plan = replanning.schedule(db, trip, 0.0, trip.start_soc, {
        "radius_km": radius_km, "min_kw": min_kw, "connector_type": connector_type,
        "detour_limit_min": detour_limit_min,
        "stop_fixed_cost_min": stop_fixed_cost_min,
        "charge_park_bonus_min": charge_park_bonus_min,
        "time_value_eur_h": time_value_eur_h})
    # Return `steckertyp` (connector type) resolved: empty means "the
    # vehicle's", and the UI should be able to show what was searched for.
    return {**plan, "trip_id": trip.id, "demo": routing.is_demo(),
            "connector_type": connector_type or trip.vehicle.connector_type}


@router.get("/fahrten")
def trips_list(db: Session = Depends(get_db), bound: int = Query(30, ge=1, le=200)):
    """The most recently planned trips.

    Deliberately more than start and destination: without consumption,
    outside temperature and payload, a list of past trips is a list of names.
    Only with these numbers does it become what one opens it for - the
    comparison of why the same route needed two charging stops in January and
    one in June.
    """
    trips = (db.query(models.Trip).order_by(models.Trip.id.desc())
               .limit(bound).all())

    result = []
    for f in trips:
        profile = f.energy_profile or []
        kwh = profile[-1].get("kwh") if profile else None
        distance_km = round((f.distance_m or 0) / 1000.0, 1)
        result.append({
            "id": f.id, "start": f.start_text, "destination": f.target_text,
            "created_at": utc_iso(f.created_at),
            "distance_km": distance_km,
            "drive_time_minutes": round((f.drive_time_s or 0) / 60.0),
            "vehicle": f.vehicle.name,
            "start_soc": f.start_soc,
            "soc_at_target": profile[-1].get("soc") if profile else None,
            "kwh_total": round(kwh, 1) if kwh is not None else None,
            "consumption_kwh_100km": (round(kwh / distance_km * 100.0, 1)
                                    if kwh and distance_km else None),
            "outside_temp_c": f.outside_temp_c,
            "speed_factor": f.speed_factor,
            "payload_kg": f.payload_kg,
            # For comparison in the history: a trip with a carrier is not
            # comparable to one without, and a recording not to a draft. One
            # must be able to see both, otherwise one compares apples with
            # oranges and wonders.
            "air_drag_factor": f.air_drag_factor,
            "trailer_kg": f.trailer_kg,
            "speed_max_kmh": f.speed_max_kmh,
            "recording": bool(f.recording),
            # Whether this trip was actually driven - a planned trip without
            # a live session is a draft, not a memory.
            "driven": bool(f.live_sessions)})
    return result


@router.delete("/fahrten/{trip_id}")
def delete_trip(trip_id: int, db: Session = Depends(get_db)):
    """Remove a trip from the history.

    Every route calculation creates up to three trips (one per variant) -
    without this endpoint the list grows with every attempt, and the one trip
    one wants to find again disappears among drafts.
    """
    trip = db.get(models.Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Fahrt nicht gefunden.")
    db.delete(trip)
    db.commit()
    return {"ok": True}


# ---------- internal ----------

def _thin_out_profile(profile: list, at_most: int = 400) -> list:
    """A fraction of the points is enough for display.

    The charts in the UI are a few hundred pixels wide - transferring more
    points than pixels brings nothing except loading time.
    """
    if len(profile) <= at_most:
        return profile
    step = len(profile) / at_most
    selected = [profile[int(i * step)] for i in range(at_most)]
    selected.append(profile[-1])
    return selected


def _response(trip, profile, avg, vehicle) -> dict:
    reserve_point = None
    if profile.reserve_at_km is not None:
        for entry in profile.points:
            if entry.km >= profile.reserve_at_km:
                reserve_point = {"km": entry.km, "lat": entry.lat,
                                 "lon": entry.lon}
                break

    return {
        "demo": routing.is_demo(),
        "distance_km": profile.distance_km,
        "drive_time_minutes": round((trip.drive_time_s or 0) / 60.0),
        "kwh_total": profile.kwh_total,
        "consumption_kwh_100km": profile.consumption_kwh_100km,
        "soc_at_target": profile.soc_at_target,
        "reserve_at_km": profile.reserve_at_km,
        "reserve_point": reserve_point,
        "suffices": profile.reserve_at_km is None,
        "weather": {"temp_c": avg.temp_c,
                   "wind_ms": avg.wind_speed_ms},
        "vehicle": {"id": vehicle.id, "name": vehicle.name,
                     "reserve_soc": vehicle.reserve_soc,
                     "battery_net_kwh": vehicle.battery_net_kwh},
        "geometry": trip.geometry or [],
        "profile": _thin_out_profile(trip.energy_profile or []),
    }
