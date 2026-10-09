"""TomTom as an advisor: suggestions and traffic - never as the source of the stored route.

**What TomTom does here, and what it does not.** OpenRouteService remains the
source of every route that jolt stores (geometry **with elevation**, speed per
segment - the consumption model needs both, and TomTom delivers neither).
TomTom answers two questions that OpenRouteService cannot:

1. *Which other routes are there?* `maxAlternatives` returns up to five,
   without a length limit - OpenRouteService rejects alternatives from 100 km.
2. *What does traffic cost?* Every route has its own delay, from live
   traffic. OpenRouteService has none.

**Why none of it is stored.** The TomTom terms (clause 11.4) allow results
only in a short-term cache and prohibit derived databases. So here: from a
suggestion, *waypoints* are chosen that the OpenRouteService routing drives
through - what is stored is its road, not TomTom's. The delay goes into the
response to the browser as a number and from there nowhere. This file writes
neither to a database nor to a file, and it does not log coordinates.

**The key is in the URL.** TomTom takes it only as a parameter, and
`requests` attaches the URL to every exception. So `str(fehler)` (error) is
never passed on, only the type - otherwise the key would end up in the log.
"""
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("uvicorn.error")

BASIS = "https://api.tomtom.com/routing/1/calculateRoute"

# A response with geometry is about two and a half megabytes for 600 km.
TIMEOUT_GEOMETRY_S = 40
TIMEOUT_SUMMARY_S = 20

# A departure within the next few minutes is "now": live traffic applies,
# and `departAt` is omitted.
NOW_TOLERANCE = timedelta(minutes=5)

MAX_ALTERNATIVEN = 5
# How many of the non-overtaken suggestions are followed up at most. Each
# costs one request to the OpenRouteService routing.
MAX_FOLLOW = 3


class TomTomError(RuntimeError):
    """TomTom did not respond or refused - with a reason for the log.

    Planning continues without TomTom; this error does not abort anything.
    """


@dataclass
class Suggestion:
    """A route as TomTom sees it. Only numbers and points, nothing stored."""
    points: list = field(default_factory=list)   # [(lat, lon), ...]
    distance_m: float = 0.0
    time_s: float = 0.0              # with traffic: live, or forecast time-dependently
    without_traffic_s: float = 0.0      # in free flow
    traffic_s: float = 0.0           # difference between the two


@dataclass
class Traffic:
    """What traffic costs on a specific route - as numbers.

    `delay_s` (delay) is the **entire** traffic influence: time with
    traffic minus time in free flow. Not `trafficDelayInSeconds`: according
    to the documentation, that field means the delay based on *real-time*
    traffic information and is therefore unsuitable for a later departure.
    Measured on 5 Oct 2026, Reutlingen - Hamburg: for Friday 4 pm it showed
    +6.5 min, although the time-dependent forecast is 28 minutes above free
    flow; in 90 days it showed 0.0. The difference of the two travel times
    is correct in both cases and, for "now", the more complete figure (+21
    versus +12.9 min).
    """
    delay_s: float
    time_s: float
    without_traffic_s: float
    forecast: bool = False       # forecast time-dependently instead of live


def api_key() -> str:
    return os.environ.get("TOMTOM_API_KEY", "").strip()


def obtainable() -> bool:
    return bool(api_key())


def _departure(departure: datetime | None) -> datetime | None:
    """The departure if it is later than a few minutes from now - otherwise None."""
    if departure is None:
        return None
    if departure.tzinfo is None:
        departure = departure.replace(tzinfo=timezone.utc)
    if departure <= datetime.now(timezone.utc) + NOW_TOLERANCE:
        return None
    return departure.astimezone(timezone.utc)


def _departure_parameter(departure: datetime | None) -> dict:
    """`departAt` for TomTom: RFC 3339 in UTC. Without a time zone TomTom
    would assume that of the start point - which would mean reading a
    browser time differently from how it was meant."""
    later = _departure(departure)
    if later is None:
        return {}
    return {"departAt": later.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _places(start, destination, between=None) -> str:
    sequence = [start] + list(between or []) + [destination]
    return ":".join(f"{lat:.5f},{lon:.5f}" for lat, lon in sequence)


def _query(places: str, timeout: int, **parameter) -> dict:
    try:
        response = requests.get(
            f"{BASIS}/{places}/json", timeout=timeout,
            params={"key": api_key(), "traffic": "true",
                    "computeTravelTimeFor": "all", "routeType": "fastest",
                    "travelMode": "car", **parameter})
    except requests.RequestException as failure:
        # Only the type: `str(fehler)` would contain the URL including the key.
        raise TomTomError(f"TomTom nicht erreichbar ({type(failure).__name__})") from None
    if response.status_code in (401, 403):
        raise TomTomError("TOMTOM_API_KEY wird abgelehnt - Schlüssel prüfen.")
    if response.status_code == 429:
        raise TomTomError("TomTom-Kontingent erschöpft.")
    if response.status_code >= 400:
        raise TomTomError(f"TomTom meldet HTTP {response.status_code}.")
    try:
        return response.json()
    except ValueError:
        raise TomTomError("TomTom lieferte keine lesbare Antwort.") from None


def _number(summary: dict, name: str) -> float:
    value = summary.get(name)
    return float(value) if isinstance(value, (int, float)) else 0.0


def read_suggestions(data: dict) -> list[Suggestion]:
    """The routes from a calculateRoute response. Faulty entries are dropped
    instead of discarding everything: a route without points or without
    length is not a route for jolt."""
    suggestions = []
    for route in data.get("routes") or []:
        z = route.get("summary") or {}
        points = [(p["latitude"], p["longitude"])
                  for leg in route.get("legs") or []
                  for p in leg.get("points") or []
                  if isinstance(p, dict) and "latitude" in p and "longitude" in p]
        distance = _number(z, "lengthInMeters")
        if len(points) < 2 or distance <= 0:
            continue
        timestamp = _number(z, "travelTimeInSeconds")
        without = _number(z, "noTrafficTravelTimeInSeconds") or timestamp
        suggestions.append(Suggestion(points=points, distance_m=distance, time_s=timestamp,
                             without_traffic_s=without, traffic_s=max(0.0, timestamp - without)))
    return suggestions


def alternativen(start: tuple[float, float], destination: tuple[float, float],
                 maximal: int = MAX_ALTERNATIVEN,
                 departure: datetime | None = None) -> list[Suggestion]:
    """The best route and up to `maximal` (maximum) alternatives, with geometry.

    With `abfahrt` (departure) TomTom calculates time-dependently: on Friday
    at four a different road is the fastest than on Sunday at three
    (measured: 723 km instead of 712 km on Reutlingen - Hamburg).
    """
    data = _query(_places(start, destination), TIMEOUT_GEOMETRY_S,
                      maxAlternatives=max(0, min(maximal, MAX_ALTERNATIVEN)),
                      **_departure_parameter(departure))
    return read_suggestions(data)


def traffic(start: tuple[float, float], destination: tuple[float, float],
            between: list[tuple[float, float]],
            departure: datetime | None = None) -> Traffic | None:
    """What traffic currently costs on the way through `zwischen` (waypoints).

    Only the summary - one kilobyte instead of two and a half megabytes. The
    waypoints force TomTom onto the same road jolt drives; without them
    TomTom would calculate for *its own* route, and the number would have
    nothing to do with the route.
    """
    data = _query(_places(start, destination, between), TIMEOUT_SUMMARY_S,
                      routeRepresentation="summaryOnly", **_departure_parameter(departure))
    routes = data.get("routes") or []
    if not routes:
        return None
    z = routes[0].get("summary") or {}
    timestamp = _number(z, "travelTimeInSeconds")
    if timestamp <= 0:
        return None
    without = _number(z, "noTrafficTravelTimeInSeconds") or timestamp
    return Traffic(delay_s=max(0.0, timestamp - without), time_s=timestamp,
                   without_traffic_s=without, forecast=_departure(departure) is not None)


def not_overtaken(suggestions: list[Suggestion]) -> list[Suggestion]:
    """The suggestions that none of the others beats by being both shorter
    and faster - sorted by time, at most `MAX_FOLLOW` (max follow).

    Everything else cannot have a charging plan that saves it: more distance
    means more energy, more time means more time. This saves the routing
    requests that would otherwise be made for hopeless routes - on four out
    of five routes where this was measured, that was every alternative.
    """
    left = [v for v in suggestions
              if not any(a is not v and a.distance_m <= v.distance_m
                         and a.time_s <= v.time_s
                         and (a.distance_m < v.distance_m or a.time_s < v.time_s)
                         for a in suggestions)]
    return sorted(left, key=lambda v: v.time_s)[:MAX_FOLLOW]
