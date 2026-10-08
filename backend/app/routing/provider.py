"""The interface between jolt and the routing service.

Why have an interface at all with exactly one adapter: free openrouteservice
access allows 2,500 requests per day. Once live re-planning (stage 3) runs
regularly, that gets tight, and a self-hosted Valhalla would take its place.
That replacement should be a second adapter and not a rebuild - so the
boundary is fixed now, while it is cheap.

What an adapter has to deliver is dictated by jolt's consumption model:
geometry **with elevation** and a speed per segment. A plain
distance-plus-duration answer is not enough - it would allow computing
neither a gradient nor the v² share of air drag.
"""
from dataclasses import dataclass, field
from typing import Protocol


class RoutingError(RuntimeError):
    """Routing could not deliver a route - with a reason for the user."""


@dataclass
class Route:
    # [[lon, lat, elevation_m], ...] - the lon/lat order is that of GeoJSON, and
    # deviating from it would be a permanent source of errors.
    points: list = field(default_factory=list)
    # Speed in m/s per segment; length len(punkte) - 1.
    speed_ms: list = field(default_factory=list)
    distance_m: float = 0.0
    drive_time_s: float = 0.0


@dataclass
class City:
    name: str
    lat: float
    lon: float


# The three presets openrouteservice knows as "preference" and that
# /api/route queries in parallel: time, distance, and what ORS considers the
# best trade-off without further input. Consumption-optimal is deliberately
# not a request of its own - ORS cannot do that as an edge weight, see below.
PREFERENCES = ("fastest", "shortest", "recommended")


class RoutingProvider(Protocol):
    def route(self, start: tuple[float, float], destination: tuple[float, float],
              intermediate_stops: list[tuple[float, float]] | None = None,
              preference: str = "recommended",
              toll_free: bool = False) -> Route:
        """Route from start to destination. Coordinates as (lat, lon).

        `mautfrei` (toll-free) avoids toll roads. For France it is the only
        alternative really worth computing: `fastest` and `recommended`
        usually return the same road on motorway stretches, `shortest` one
        that nobody drives. Forgoing the autoroute costs hours and easily
        saves forty euros on a long trip - that is a trade-off the driver is
        entitled to make, and can only make if both routes are shown side by
        side.

        `praeferenz` (preference) is one of the three values from
        PRAEFERENZEN. An adapter that cannot make a real distinction (see
        demo.py) may ignore the parameter and always return the same route -
        /api/route detects identical results and shows them only once, with
        several labels.
        """
        ...

    def elevations(self, points: list) -> list | None:
        """Look up elevations for a list [[lon, lat], ...].

        Needed for recorded trips: there the route is only known afterwards,
        and without an elevation profile a consumption measurement cannot be
        interpreted - whether the extra consumption was due to driving style
        or the hill cannot otherwise be told apart.

        Explicitly **not** from GPS: its elevation scatters by ten to twenty
        meters, and summed-up differences yield several hundred meters of
        climb for a trip across flat terrain.

        `None` means "this adapter cannot do that" - the caller then falls
        back to the GPS elevation or computes flat.
        """
        ...

    def seek(self, text: str, country: str = "") -> list[City]:
        """Resolve place names to coordinates.

        `land` (country; ISO 3166 alpha-2, e.g. "DE") restricts the search to
        one country. Left empty it searches worldwide - jolt is not a
        Germany-only planner, a destination may lie across the border.
        """
        ...
