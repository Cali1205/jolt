"""Driven routes as route candidates.

**Why.** For an electric car the fastest road is not automatically the best,
and the alternatives that can be computed are no good: on the measured route
Mâcon - Reutlingen, each of the twelve geometrically generated detours lost
against the fastest route (see `variants.py`). The route that actually won -
59 km shorter, 13 minutes longer, 2 to 5 EUR cheaper overall - came from
**two points of the route actually driven**. Experience beats geometry:
whoever has driven a route before knows a way that no edge weight knows.

**What happens here.** A path is built from the measurement points of an
earlier trip (whether recorded or planned and driven). If it fits the start
and destination of the new request - forward or backward, in full or as a
partial section - waypoints are chosen along the path. The routing drives
through them; what comes out is a road and not a straight line.

This module knows neither database nor network. It takes lists of points and
returns lists of points - so it can be tested without either.
"""
from dataclasses import dataclass

from ..geo import haversine_m

# How close the start and destination of the request must be to the path.
# As a share of the straight-line distance, with upper and lower limits: on
# 600 km, 24 km is "the same destination"; on 30 km it would be two city
# districts too many.
#
# The upper limit was measured on a real trip: the recording of the trip
# Gueugnon - Reutlingen starts 15.7 km after the planned start because
# recording was only started after departure. That is the normal case, not
# the exception - and with a 15 km upper limit exactly this trip would have
# failed. The routing itself covers the stretch up to the path.
RADIUS_SHARE = 0.04
RADIUS_MIN_KM = 3.0
RADIUS_MAX_KM = 30.0

# The path section has to cover the request at all. A path that covers only
# a tenth of the route is not a candidate but a coincidence.
MIN_COVERAGE = 0.7

# Spacing of the waypoints along the path. Denser means the routing follows
# the driven road more closely, but every waypoint is a place where it can
# get stuck; further apart leaves it room to choose the way itself. 25 km is
# a stretch of motorway.
SPACING_KM = 25.0

# A routing call accepts only a limited number of waypoints.
MAX_WAYPOINTS = 20

# Points below this speed are breaks, charging spots and parking lots. As a
# waypoint they would force the route into a detour off the road - exactly
# the error a real trip with a charging stop would otherwise build in.
MIN_SPEED_KMH = 25.0


@dataclass
class Section:
    """The part of an earlier path that fits the request."""
    points: list                # [(lat, lon, speed_kmh | None), ...] in the request's direction of travel
    opposite: bool         # The path was driven in the opposite direction
    spacing_start_km: float     # how far the request's start is from the path
    spacing_target_km: float
    length_km: float            # along the path


def radius_km(straight_line_km: float) -> float:
    return min(RADIUS_MAX_KM, max(RADIUS_MIN_KM, straight_line_km * RADIUS_SHARE))


def _next(path: list, lat: float, lon: float) -> tuple[int, float]:
    """Index of the nearest point and its distance in m."""
    best, spacing = 0, float("inf")
    for i, p in enumerate(path):
        d = haversine_m(lat, lon, p[0], p[1])
        if d < spacing:
            best, spacing = i, d
    return best, spacing


def _length_m(points: list) -> float:
    return sum(haversine_m(a[0], a[1], b[0], b[1])
               for a, b in zip(points, points[1:]))


def fitting_section(path: list, start: tuple[float, float],
                        destination: tuple[float, float]) -> Section | None:
    """The part of the path between start and destination - or None.

    `pfad` (path): [(lat, lon, speed_kmh), ...] in driving order.

    The path point closest to the start and the one closest to the
    destination are found. If they are in path order, it is a partial
    section forward; if reversed, the route was driven the other way round
    and the section is flipped. Both count: whoever has driven
    Gueugnon - Reutlingen also knows Reutlingen - Gueugnon.
    """
    if len(path) < 2:
        return None
    straight_line_km = haversine_m(start[0], start[1], destination[0], destination[1]) / 1000.0
    if straight_line_km <= 0:
        return None
    limit_m = radius_km(straight_line_km) * 1000.0

    i, d_start = _next(path, start[0], start[1])
    j, d_target = _next(path, destination[0], destination[1])
    if d_start > limit_m or d_target > limit_m or i == j:
        return None

    against = j < i
    section = path[j:i + 1][::-1] if against else path[i:j + 1]
    length_km = _length_m(section) / 1000.0
    # The path is at least as long as the straight line; significantly less
    # means measurement points are missing - a gap about which nothing can
    # be said.
    if length_km < straight_line_km * MIN_COVERAGE:
        return None
    return Section(points=section, opposite=against,
                     spacing_start_km=d_start / 1000.0,
                     spacing_target_km=d_target / 1000.0, length_km=length_km)


def waypoints(section: Section, spacing_km: float = SPACING_KM,
                   maximal: int = MAX_WAYPOINTS) -> list[tuple[float, float]]:
    """Waypoints along the section, excluding start and destination.

    Every `spacing_km` (spacing) along the path, the nearest point that was
    **driven**, i.e. above `MIN_SPEED_KMH` (minimum speed), is chosen. A
    point without a speed value counts as driven: some sources provide none,
    and a path without any such data should not fail at this point.

    If the count is not enough for the desired spacing, the spacing is
    increased instead of dropping points - an even distribution is better
    than one that breaks off at the end.
    """
    points = section.points
    if len(points) < 3:
        return []
    total_m = section.length_km * 1000.0
    step_m = max(spacing_km * 1000.0, total_m / (maximal + 1))

    # Distance along the path up to each point.
    cumulative = [0.0]
    for a, b in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + haversine_m(a[0], a[1], b[0], b[1]))

    def driven(p) -> bool:
        return p[2] is None or p[2] >= MIN_SPEED_KMH

    waypoints: list[tuple[float, float]] = []
    target_m = step_m
    # Not all the way to the end: a point shortly before the destination
    # achieves nothing and forces the route to knock where it is heading
    # anyway.
    while target_m < total_m - step_m * 0.5:
        candidate = min(
            (i for i in range(1, len(points) - 1) if driven(points[i])),
            key=lambda i: abs(cumulative[i] - target_m), default=None)
        if candidate is not None:
            p = (points[candidate][0], points[candidate][1])
            if not waypoints or haversine_m(waypoints[-1][0], waypoints[-1][1], p[0], p[1]) > 1000.0:
                waypoints.append(p)
        target_m += step_m
    return waypoints[:maximal]


def path_from_samples(rows: list) -> list[tuple[float, float, float | None]]:
    """[(lat, lon, speed_kmh), ...] from the rows of a query; rows without
    coordinates are dropped."""
    return [(lat, lon, velocity) for lat, lon, velocity in rows
            if lat is not None and lon is not None]
