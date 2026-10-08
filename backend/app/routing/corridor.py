"""Find charge points in the corridor around a route.

This is the only geo query jolt needs - and the reason there is no PostGIS
here. What is searched for is not "everything within X of a point" but
"everything close to this polyline". It is enough to split the route into
anchor points, query one rectangle per anchor (the index on (lat, lon)
supports that), and then measure precisely with haversine.

With around 150,000 German charge points this takes milliseconds - and the
SQLite fallback for local development is preserved.
"""
import math
from dataclasses import dataclass

from sqlalchemy import and_, or_

from .. import models
from ..geo import haversine_m

KM_PER_DEGREE_LAT = 111.32
# Access and return do not run over the motorway. 45 km/h is a generous
# figure, but the number should overestimate rather than underestimate the
# detour: an overly optimistically planned side trip costs real minutes on
# the road.
ACCESS_ROAD_KMH = 45.0
# Fixed cost of every stop regardless of distance: leaving the road,
# searching, parking, fetching the cable, merging back at the end.
FIXED_MINUTES = 4.0


@dataclass
class Candidate:
    charge_point: models.ChargePoint
    km_on_route: float
    spacing_m: float
    detour_minutes: float

    def as_dict(self) -> dict:
        lp = self.charge_point
        return {"id": lp.id, "name": lp.name, "operator": lp.operator,
                "lat": lp.lat, "lon": lp.lon, "city": lp.city, "address": lp.address,
                "max_kw": lp.max_kw, "point_count": lp.point_count,
                "connector_types": lp.connector_types, "source": lp.source,
                "km_on_route": round(self.km_on_route, 1),
                "spacing_m": round(self.spacing_m),
                "detour_minutes": round(self.detour_minutes, 1)}


def _chainage(points: list) -> list[float]:
    """Cumulative kilometers per vertex of the route."""
    km = [0.0]
    for i in range(len(points) - 1):
        km.append(km[-1] + haversine_m(points[i][1], points[i][0],
                                       points[i + 1][1], points[i + 1][0]) / 1000.0)
    return km


def _anchor(points: list, km_list: list[float],
           spacing_km: float) -> list[tuple[float, float, float, int]]:
    """Thin the route out to anchor points: (lat, lon, km_on_route, index).

    The anchors only serve preselection - the rectangle for the database and
    the question of *roughly where* a charge point lies along the route.
    Measuring is done afterwards at the real vertices; see `suchen`.
    """
    if not points:
        return []
    anchor = [(points[0][1], points[0][0], 0.0, 0)]
    km_since_anchor = 0.0
    for i in range(len(points) - 1):
        km_since_anchor += km_list[i + 1] - km_list[i]
        if km_since_anchor >= spacing_km:
            anchor.append((points[i + 1][1], points[i + 1][0],
                          km_list[i + 1], i + 1))
            km_since_anchor = 0.0
    if anchor[-1][3] < len(points) - 1:
        anchor.append((points[-1][1], points[-1][0], km_list[-1], len(points) - 1))
    return anchor


def seek(db, points: list, radius_km: float = 8.0, min_kw: float = 50.0,
           connector_type: str = "CCS", at_most: int = 400) -> list[Candidate]:
    """All matching charge points along the route, ordered by progress.

    `radius_km` is straight-line distance to the route. Eight kilometers
    sounds like a lot, but on a motorway it is reached quickly when the next
    exit comes late - the decision is made via `detour_minutes` (detour
    minutes) anyway, not via the straight-line distance.
    """
    if not points:
        return []

    km_list = _chainage(points)
    anchor = _anchor(points, km_list, max(3.0, radius_km * 0.75))
    d_lat = radius_km / KM_PER_DEGREE_LAT

    rectangles = []
    for lat, lon, _, _ in anchor:
        d_lon = radius_km / (KM_PER_DEGREE_LAT * max(0.1, math.cos(math.radians(lat))))
        rectangles.append(and_(models.ChargePoint.lat.between(lat - d_lat, lat + d_lat),
                              models.ChargePoint.lon.between(lon - d_lon, lon + d_lon)))

    lookup = db.query(models.ChargePoint).filter(or_(*rectangles))
    if min_kw > 0:
        lookup = lookup.filter(models.ChargePoint.max_kw >= min_kw)
    if connector_type:
        lookup = lookup.filter(models.ChargePoint.connector_types.contains(connector_type))
    # What the source explicitly reports as out of service does not belong
    # in a plan. `isnot(False)` and not `is_(True)`: NULL means **unknown**,
    # and that is the case for most of the database. Treating unknown like
    # excluded loses almost all candidates and then no planning happens at
    # all.
    lookup = lookup.filter(models.ChargePoint.operational.isnot(False))

    candidates: list[Candidate] = []
    for lp in lookup.limit(at_most * 5).all():
        # Measure precisely, and at the real vertices - not at the nearest
        # anchor. With a 25 km radius the anchors are about 19 km apart; a
        # charger right next to the road could be up to 9 km from an anchor
        # and would be charged 25 minutes of detour as a result. It would
        # then drop out of every plan although it lies directly on the way.
        # So the anchor only says *which stretch* of the route to check;
        # measuring happens in the window up to the neighboring anchors.
        upcoming = min(range(len(anchor)),
                        key=lambda i: haversine_m(lp.lat, lp.lon,
                                                  anchor[i][0], anchor[i][1]))
        begin = anchor[upcoming - 1][3] if upcoming > 0 else 0
        upto = (anchor[upcoming + 1][3] if upcoming + 1 < len(anchor)
               else len(points) - 1)

        spacing = float("inf")
        km_on_route = anchor[upcoming][2]
        for i in range(begin, upto + 1):
            d = haversine_m(lp.lat, lp.lon, points[i][1], points[i][0])
            if d < spacing:
                spacing, km_on_route = d, km_list[i]

        if spacing > radius_km * 1000:
            continue
        detour = FIXED_MINUTES + (2 * spacing / 1000.0) / ACCESS_ROAD_KMH * 60.0
        candidates.append(Candidate(lp, km_on_route, spacing, detour))

    candidates.sort(key=lambda k: k.km_on_route)
    return candidates[:at_most]


def point_on_route(points: list, lat: float, lon: float) -> tuple[float, float]:
    """How far along the route is a position?

    Returns: (km_on_route, distance_m). Needed by the live tracking to
    compare actual and planned SoC at the same spot - and to detect that
    someone has left the route.
    """
    if not points:
        return 0.0, 0.0
    best_km, best_spacing = 0.0, float("inf")
    km_cum = 0.0
    for i, p in enumerate(points):
        if i > 0:
            km_cum += haversine_m(points[i - 1][1], points[i - 1][0],
                                  p[1], p[0]) / 1000.0
        spacing = haversine_m(lat, lon, p[1], p[0])
        if spacing < best_spacing:
            best_spacing, best_km = spacing, km_cum
    return best_km, best_spacing
