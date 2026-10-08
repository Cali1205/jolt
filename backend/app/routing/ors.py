"""Adapter for openrouteservice.

Chosen because it is the only free service that delivers an **elevation
profile** with the route (`elevation=true`). Without elevation the
consumption model would be limited to flat terrain, and thus blind in
exactly the cases where a charging planner is worthwhile.

Quota of the free tier: 2,500 requests/day, 40,000/month.
Key: https://openrouteservice.org/dev/#/signup
"""
import logging
import os

import requests

from .provider import City, Route, RoutingError

BASIS = "https://api.openrouteservice.org"
TIMEOUT = 25
# If a segment comes without a speed value (happens at intersections and at
# the destination point), this value is used to carry on instead of aborting.
SPEED_FALLBACK_MS = 22.0        # ~80 km/h

log = logging.getLogger("uvicorn.error")


class ORS:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("ORS_API_KEY", "")

    # ---------- internal ----------

    def _header(self) -> dict:
        if not self.api_key:
            raise RoutingError(
                "Kein ORS_API_KEY gesetzt - ohne Schlüssel lässt sich keine "
                "Route rechnen. Kostenlos unter openrouteservice.org/dev")
        return {"Authorization": self.api_key,
                "Content-Type": "application/json; charset=utf-8"}

    @staticmethod
    def _speed_per_segment(attrs: dict, point_count: int) -> list:
        """Turn the routing steps into a speed per segment.

        ORS returns distance and duration per step as well as the indices of
        the associated geometry points (`way_points`). From these, the
        average speed of that step is distributed over all its segments.

        This is more accurate than "total distance divided by total time": a
        plan that computes the pass through a town at motorway speed
        underestimates the consumption on the motorway - and that is where
        it is decided.
        """
        velocity = [0.0] * max(0, point_count - 1)
        for section in attrs.get("segments", []):
            for step in section.get("steps", []):
                duration = step.get("duration") or 0.0
                distance = step.get("distance") or 0.0
                wp = step.get("way_points") or []
                if duration <= 0 or distance <= 0 or len(wp) != 2:
                    continue
                v = distance / duration
                for i in range(wp[0], min(wp[1], len(velocity))):
                    velocity[i] = v
        return [v if v > 0 else SPEED_FALLBACK_MS for v in velocity]

    # ---------- public ----------

    def route(self, start: tuple[float, float], destination: tuple[float, float],
              intermediate_stops: list[tuple[float, float]] | None = None,
              preference: str = "recommended",
              toll_free: bool = False) -> Route:
        coordinates = [[start[1], start[0]]]
        for stop in (intermediate_stops or []):
            coordinates.append([stop[1], stop[0]])
        coordinates.append([destination[1], destination[0]])

        try:
            response = requests.post(
                f"{BASIS}/v2/directions/driving-car/geojson",
                headers=self._header(), timeout=TIMEOUT,
                json={"coordinates": coordinates, "elevation": True,
                      "instructions": True, "units": "m",
                      "preference": preference,
                      # Only set when requested: ORS rejects an empty
                      # `avoid_features` with HTTP 400.
                      **({"options": {"avoid_features": ["tollways"]}}
                         if toll_free else {})})
        except requests.RequestException as failure:
            raise RoutingError(f"Routing nicht erreichbar: {failure}") from failure

        if response.status_code == 401:
            raise RoutingError("ORS_API_KEY wird abgelehnt - Schlüssel prüfen.")
        if response.status_code == 429:
            raise RoutingError(
                "Tageskontingent von openrouteservice erschöpft (2.500 Anfragen).")
        if response.status_code >= 400:
            raise RoutingError(f"Routing meldet HTTP {response.status_code}: "
                                f"{response.text[:200]}")

        data = response.json()
        features = data.get("features") or []
        if not features:
            raise RoutingError("Keine Route gefunden - Start oder Ziel prüfen.")

        geometry = features[0].get("geometry", {}).get("coordinates") or []
        attrs = features[0].get("properties", {})
        summary = attrs.get("summary", {})

        if geometry and len(geometry[0]) < 3:
            log.warning("Received route without elevation values - consumption "
                        "is computed on flat terrain and comes out too low "
                        "in hilly areas.")

        return Route(points=geometry,
                     speed_ms=self._speed_per_segment(attrs, len(geometry)),
                     distance_m=float(summary.get("distance") or 0.0),
                     drive_time_s=float(summary.get("duration") or 0.0))

    def elevations(self, points: list) -> list | None:
        """Elevations via /elevation/line - same key as for routing.

        One request per recorded trip, i.e. once at the end and not on the
        road. On failure nothing is raised, None is returned instead: a
        recording without elevations is still a recording, and losing it for
        that reason would be the worse trade.
        """
        if not points or len(points) < 2:
            return None
        try:
            response = requests.post(
                f"{BASIS}/elevation/line", headers=self._header(), timeout=TIMEOUT,
                json={"format_in": "polyline", "format_out": "polyline",
                      "geometry": [[float(p[0]), float(p[1])] for p in points]})
            response.raise_for_status()
            geometry = (response.json() or {}).get("geometry")
        except (requests.RequestException, ValueError) as failure:
            log.warning("Elevation request to ORS failed: %s", failure)
            return None
        if not isinstance(geometry, list) or len(geometry) != len(points):
            log.warning("Elevation response does not match the request (%s "
                        "instead of %s points).", len(geometry or []), len(points))
            return None
        return [[p[0], p[1], p[2] if len(p) > 2 else 0.0] for p in geometry]

    def seek(self, text: str, country: str = "") -> list[City]:
        # Without a country filter ORS searches worldwide - which is exactly
        # what a destination across the border needs. Only if `land`
        # (country) is explicitly set (e.g. to distinguish an input like
        # "Hamburg" from places of the same name elsewhere) is it restricted.
        params = {"text": text, "size": 6}
        if country:
            params["boundary.country"] = country
        try:
            # Key in the header, never in the URL: otherwise it appears in
            # every error message that goes to the client.
            response = requests.get(f"{BASIS}/geocode/search", timeout=TIMEOUT,
                                   params=params, headers=self._header())
            response.raise_for_status()
        except requests.RequestException as failure:
            raise RoutingError(
                f"Ortssuche nicht erreichbar ({type(failure).__name__})") from failure

        hit = []
        for feature in response.json().get("features", []):
            coord = feature.get("geometry", {}).get("coordinates") or []
            if len(coord) < 2:
                continue
            hit.append(City(name=feature.get("properties", {}).get("label", text),
                               lat=coord[1], lon=coord[0]))
        return hit
