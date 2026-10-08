"""Geometry on the globe. Two functions, no dependencies.

These two used to live in `energy/model.py`, and that was the only layer
violation in the backend: `routing/corridor.py` had to reach into the physics
for a distance between two points. A distance, however, is no statement about
energy, and `routing` sits below `energy`.

Why here and not in `routing/geo.py`, as first proposed: then `energy` would
depend on `routing`, and the violation would merely be turned around. Both
layers need these functions, so they belong **below** both - on the same level
as `models` and `security`.

Deliberately without a single import from the project. A module that knows
nothing can be used from anywhere without ever forming a cycle.
"""
import math

# Mean Earth radius according to WGS84. For distances along a route the
# spherical approximation is accurate enough: the error compared with the
# ellipsoid is below half a percent, and the support points of a route are
# only a few hundred metres apart anyway.
ERDRADIUS_M = 6371008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance between two points on the globe in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * ERDRADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bearing_degree(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Heading from point 1 to point 2, 0 = north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def offset_point(lat: float, lon: float, bearing: float,
                    distance_m: float) -> tuple[float, float]:
    """The point that lies `distance_m` away in the direction `bearing`.

    The inverse of `haversine_m` and `bearing_degree` taken together. Needed
    by `routing/variants.py` to place detour points beside a route - for that
    the spherical approximation suffices, just as for the distance.

    Deliberately not "one degree of latitude is 111.32 km": that is only true
    for latitude. For longitude it depends on the latitude, and anyone who
    forgets that displaces a point in southern France by a third too far.
    """
    d = distance_m / ERDRADIUS_M
    b = math.radians(bearing)
    p1 = math.radians(lat)
    p2 = math.asin(math.sin(p1) * math.cos(d)
                   + math.cos(p1) * math.sin(d) * math.cos(b))
    dl = math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                    math.cos(d) - math.sin(p1) * math.sin(p2))
    # Normalise to -180..180 so that an offset across the date line does not
    # yield a longitude of 190 degrees.
    new_lon = (lon + math.degrees(dl) + 540.0) % 360.0 - 180.0
    return math.degrees(p2), new_lon
