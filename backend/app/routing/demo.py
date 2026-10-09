"""A routing adapter without network and without a key - development only.

It invents a route: a straight line between start and destination, plus a
synthetic elevation profile and a speed profile that starts slowly, rises to
motorway speed and drops again at the destination.

What it is good for: the consumption model, the corridor search, the live
scaffolding and the UI can be exercised completely with it, without an ORS
key being available or the daily quota being used. That is what one needs
most often during development.

It steps in **only** if no ORS_API_KEY is set, and states in every response
that it was the one - an invented route must never be mistaken for a real
one unnoticed.

Important limitation: the distance is the straight line and thus about a
fifth shorter than any real road. That is enough for checking the
calculation chain; it is not suitable as a statement about a real trip.
"""
import math

from ..geo import haversine_m
from .provider import City, Route

# Rough coordinates of a few cities so that the place search can return
# something offline. Not geocoding, just a handful of reference points.
PLACES = {
    "hamburg": (53.5511, 9.9937), "münchen": (48.1351, 11.5820),
    "munich": (48.1351, 11.5820), "berlin": (52.5200, 13.4050),
    "köln": (50.9375, 6.9603), "koeln": (50.9375, 6.9603),
    "frankfurt": (50.1109, 8.6821), "stuttgart": (48.7758, 9.1829),
    "hannover": (52.3759, 9.7320), "leipzig": (51.3397, 12.3731),
    "nürnberg": (49.4521, 11.0767), "nuernberg": (49.4521, 11.0767),
    "bremen": (53.0793, 8.8017), "dortmund": (51.5136, 7.4653),
    "kassel": (51.3127, 9.4797), "würzburg": (49.7913, 9.9534),
}

POINT_DISTANCE_KM = 1.0


class DemoRouting:
    is_demo = True

    def route(self, start, destination, intermediate_stops=None,
             preference: str = "recommended",
             toll_free: bool = False) -> Route:
        # There is no real road network from which the fastest and
        # recommended routes could be told apart, and an invented straight
        # line has no toll roads either. `praeferenz` and `mautfrei` are
        # therefore accepted and ignored; /api/route detects the identical
        # results itself and merges them into one variant with several
        # labels.
        stations = [start] + list(intermediate_stops or []) + [destination]
        points: list[list[float]] = []
        velocity: list[float] = []

        for a, b in zip(stations, stations[1:]):
            part_points, part_speed = self._section(a, b, first=not points)
            points.extend(part_points)
            velocity.extend(part_speed)

        distance = sum(self._spacing_m(points[i][1], points[i][0],
                                      points[i + 1][1], points[i + 1][0])
                      for i in range(len(points) - 1))
        drive_time = sum(
            self._spacing_m(points[i][1], points[i][0],
                            points[i + 1][1], points[i + 1][0]) / max(1.0, velocity[i])
            for i in range(len(points) - 1))

        return Route(points=points, speed_ms=velocity, distance_m=distance,
                     drive_time_s=drive_time)

    def elevations(self, points: list) -> list | None:
        """Demo routing invents routes, but not elevations.

        An invented elevation would be more harmful here than none at all: it
        would look like a measurement and feed into the correction factor.
        """
        return None

    def seek(self, text: str, country: str = "") -> list[City]:
        query = (text or "").strip().lower()
        for name, (lat, lon) in PLACES.items():
            if query and query in name:
                return [City(name=f"{name.capitalize()} (Demo)", lat=lat, lon=lon)]
        return []

    # ---------- internal ----------

    @staticmethod
    def _spacing_m(lat1, lon1, lat2, lon2) -> float:
        return haversine_m(lat1, lon1, lat2, lon2)

    def _section(self, a, b, first: bool):
        total_m = self._spacing_m(a[0], a[1], b[0], b[1])
        count = max(2, int(total_m / 1000.0 / POINT_DISTANCE_KM))

        points, velocity = [], []
        for i in range(count + 1):
            t = i / count
            lat = a[0] + (b[0] - a[0]) * t
            lon = a[1] + (b[1] - a[1]) * t
            # Two superimposed waves: a long low mountain range and smaller
            # hilltops. That gives the elevation profile climbs and
            # descents, and regeneration is actually exercised.
            elevation = (120.0 + 220.0 * math.sin(math.pi * t)
                     + 45.0 * math.sin(t * 14.0))
            if i > 0 or first:
                points.append([round(lon, 6), round(lat, 6), round(elevation, 1)])
            if i < count:
                # Slower on the on-ramp and off-ramp, motorway in between.
                edge = min(t, 1.0 - t)
                v = 16.0 + 20.0 * min(1.0, edge / 0.04)
                velocity.append(round(v, 2))
        return points, velocity
