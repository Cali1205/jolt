"""Generate real route alternatives where the routing delivers none.

**The problem.** For an electric car the fastest road is not automatically
the best: every detour costs energy, and energy costs charging time. So the
comparison is worthwhile - only there is nothing to compare.
`/api/route` asked openrouteservice for `fastest`, and that was it.

**Why the obvious ways do not work** - both measured on the route
Mâcon → Reutlingen (598 km), not assumed:

- ORS's `alternative_routes` would be exactly right and delivers up to
  three routes in *one* request. But the public service rejects it as soon
  as the route is longer than **100 km** (error 2004). That rules it out for
  every trip where an alternative would change anything at all.
- `shortest` delivers 477 km in 10.7 hours versus 598 km in 5.6. That is no
  alternative, that is a different mode of transport.
- `recommended` delivers the same road as `fastest` on motorway stretches -
  measured as the identical figure down to the meter.

**What works.** Waypoints. A route forced through a point next to the
fastest road is a real third option, and waypoints are not subject to a
length restriction. On the same route:

    fastest                 598.3 km   5.56 h
    waypoint 75 %, +6 %     548.0 km   5.98 h   <- 50 km shorter
    (actually driven)       ~545 km    ~6.0 h

The driver had found this route himself; the planner did not know it.

**How the points are chosen.** A fraction is taken along the straight line
between start and destination and offset perpendicular to it. The offset
width depends on the route length and not on a fixed number of kilometers:
30 km next to a 600 km route is a nuance, next to a 150 km route it is a
different federal state.

**And what of it is any good: nothing so far.** On the measured route
twelve geometric candidates were calculated - pick-off at 50/75/80/85/90 %,
offset 4/6/8 % - and **every single one** lost against the fastest route
once charging time and electricity costs were weighed against the extra
driving time. The best was still 2 EUR off, most were considerably further.
The reason is obvious: a point that lies geometrically next to the motorway
lies somewhere in traffic terms, and ORS builds a route over country roads
from it.

The route that actually won - 539.6 km in 5.77 h, i.e. 59 km shorter for 13
minutes more and 2 to 5 EUR cheaper overall, depending on the value of time
- came from **two points of the route actually driven**. Not from geometry
but from experience.

That is why `examine_detours` (check detours) in `Routenanfrage` (route
request) is set to `False`. What is here is the working mechanism -
generate candidates, discard hopeless ones up front, measure the rest
against the charging plan - and a candidate supplier that does not fill it
yet. The obvious better one is recorded trips: `live/recording.py` writes
them anyway, and whoever has driven a route before knows a way that no edge
weight knows.
"""
import logging

from ..geo import haversine_m, bearing_degree, offset_point

log = logging.getLogger("uvicorn.error")

# Below this straight-line distance the search is not worthwhile: on short
# routes there is rarely more than one sensible way, the detour does not
# matter against the charging time - and ORS's own `alternative_routes`
# works there anyway.
MIN_STRAIGHT_LINE_KM = 120.0

# Where on the straight line to pick off. Two places, so that both a
# different approach and a different final approach can arise.
SHARES = (0.5, 0.75)

# Offset as a share of the straight line. 6 % was the usable width on the
# measured route: at 12 % all candidates were considerably worse, at 3 %
# they coincided with the original route.
OFFSET_SHARE = 0.06


def alternative_points(start: tuple[float, float], destination: tuple[float, float],
                   shares: tuple = SHARES,
                   offset_share: float = OFFSET_SHARE) -> list[dict]:
    """Candidates for waypoints, each as {"punkt", "etikett"} (point, label).

    An empty list means "not worthwhile for this route" - the caller then
    calculates only the fastest route as before.
    """
    straight_line = haversine_m(start[0], start[1], destination[0], destination[1])
    if straight_line / 1000.0 < MIN_STRAIGHT_LINE_KM:
        return []

    bearing = bearing_degree(start[0], start[1], destination[0], destination[1])
    offset_m = straight_line * offset_share

    candidates = []
    for share in shares:
        # Point on the straight line: onward toward the destination by the fraction.
        middle = offset_point(start[0], start[1], bearing, straight_line * share)
        for sign in (+1, -1):
            across = (bearing + sign * 90.0) % 360.0
            point = offset_point(middle[0], middle[1], across, offset_m)
            # Read the compass direction from the result and not from the
            # sign: `peilung + 90°` (bearing) turns right, and to the right
            # of an eastbound route lies south. Tying the label to the sign
            # mislabels half the map.
            direction = "nördlich" if point[0] > middle[0] else "südlich"
            candidates.append({
                "point": point,
                # The label describes the *origin*, not the quality - that is
                # only known after evaluation.
                "label": f"{direction} bei {round(share * 100)} %",
            })
    return candidates


def actual_dominated(candidate_m: float, candidate_s: float,
                  basis_m: float, basis_s: float,
                  tolerance: float = 0.005) -> bool:
    """Is the candidate both longer and slower than the baseline?

    Then it cannot win - no matter how the charging plan turns out, because
    more distance means more energy and more time means more time. Such
    candidates are dropped *before* weather, consumption profile and
    charging plan are calculated for them; that is the expensive part.

    The tolerance prevents a candidate that is the same road down to a
    per-mille from remaining as a proposal of its own.
    """
    return (candidate_m >= basis_m * (1 - tolerance)
            and candidate_s >= basis_s * (1 - tolerance))
