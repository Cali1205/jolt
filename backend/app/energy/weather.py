"""Weather along the route via Open-Meteo (no key, no registration).

Not one value for the whole trip: Hamburg-Munich is 800 km, and in winter
there are regularly ten degrees and a different wind between start and
destination. Therefore several support points are queried in a single
request - Open-Meteo accepts comma-separated coordinate lists.
"""
import logging
from datetime import datetime, timedelta, timezone

import requests

from ..geo import haversine_m
from .model import Environment

API = "https://api.open-meteo.com/v1/forecast"
SUPPORT_POINTS = 6
TIMEOUT = 8

# Open-Meteo delivers hourly values for 16 days. A reserve of one day, so that
# the last hour does not lie right at the edge.
FORECAST_DAYS = 15
# A departure in the next few minutes is "now": the difference between the
# current and the hourly forecast is smaller there than the measurement
# uncertainty, and the current query is the more accurate one.
NOW_TOLERANCE = timedelta(minutes=30)

log = logging.getLogger("uvicorn.error")


def _select(points: list, count: int) -> list:
    """Evenly spaced support points; start and destination always included."""
    if len(points) <= count:
        return list(points)
    step = (len(points) - 1) / (count - 1)
    return [points[round(i * step)] for i in range(count)]


def _forecast_for(departure: datetime | None) -> bool:
    """Should the hourly forecast apply instead of the current measurement?

    Yes, if the departure is more than half an hour away - and still within
    what Open-Meteo knows. Beyond that the calculation uses the current
    weather and says so in the log: better wrong weather that is recognisable
    as such than no route at all.
    """
    if departure is None:
        return False
    now = datetime.now(timezone.utc)
    if departure.tzinfo is None:
        departure = departure.replace(tzinfo=timezone.utc)
    if departure <= now + NOW_TOLERANCE:
        return False
    if departure > now + timedelta(days=FORECAST_DAYS):
        log.warning("Departure in more than %d days - there is no weather "
                    "forecast for that, the current weather applies.", FORECAST_DAYS)
        return False
    return True


def _hour(times: list, moment: datetime) -> int:
    """Index of the hour in `times` that is closest to the point in time.

    With `timezone=UTC` Open-Meteo delivers the times as "2026-10-06T08:00"
    without a zone, ascending and hourly. The index follows from the first
    entry and the spacing, without searching the list; it is clamped to the
    available values.
    """
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    first_item = datetime.fromisoformat(times[0])
    index = round((moment - first_item).total_seconds() / 3600.0)
    return max(0, min(len(times) - 1, index))


def along_route(points: list, count: int = SUPPORT_POINTS,
                  preset: Environment | None = None,
                  departure: datetime | None = None, duration_s: float = 0.0):
    """Returns a function (lat, lon) -> environment.

    `departure` and `duration_s`: if the departure is more than half an hour
    away, the hourly forecast applies - at each support point for the hour in
    which one arrives there: departure plus a share of the driving time,
    spread evenly over the distance. A trip tomorrow morning at six should not
    be calculated with this afternoon's weather; the heating is the largest
    single item of the cold.

    If the query fails, it does not abort but continues with `preset`: a
    route without weather is much better than no route at all, and the live
    tracking corrects the error within the first few kilometres anyway.

    `preset` is sensibly empty when planning - then 15 °C and no wind apply.
    **En route** that is exactly the wrong assumption: someone who re-plans a
    trip calculated at -5 °C in winter and falls back to 15 °C loses the
    heating load - the largest single item of the cold - and calculates the
    remaining distance too optimistically. That is why the re-planning there
    passes in the temperature of the trip instead of relying on the preset.
    """
    fallback = preset or Environment()
    probes = _select(points, count)
    if not probes:
        return lambda lat, lon: fallback

    lats = ",".join(f"{p[1]:.4f}" for p in probes)
    lons = ",".join(f"{p[0]:.4f}" for p in probes)
    forecast = _forecast_for(departure)
    large = "temperature_2m,wind_speed_10m,wind_direction_10m"
    request = {"latitude": lats, "longitude": lons, "wind_speed_unit": "ms"}
    if forecast:
        request.update({"hourly": large, "timezone": "UTC", "forecast_days": 16})
    else:
        request["current"] = large
    try:
        response = requests.get(API, params=request, timeout=TIMEOUT)
        response.raise_for_status()
        raw = response.json()
    except (requests.RequestException, ValueError) as failure:
        log.warning("Weather query failed (%s) - calculating with %.0f °C.",
                    failure, fallback.temp_c)
        return lambda lat, lon: fallback

    # For a single coordinate Open-Meteo returns an object, for several a
    # list. Bring both into the same shape.
    entries = raw if isinstance(raw, list) else [raw]

    measurements: list[tuple[float, float, Environment]] = []
    for nr, (probe, entry) in enumerate(zip(probes, entries)):
        if forecast:
            try:
                vals = _hourly_values(entry, departure, duration_s, nr, len(probes))
            except (KeyError, IndexError, ValueError, TypeError) as failure:
                # A response that does not look as expected is no reason to
                # discard the route.
                log.warning("Weather forecast not readable (%s) - calculating "
                            "with %.0f °C.", type(failure).__name__, fallback.temp_c)
                return lambda lat, lon: fallback
        else:
            vals = entry.get("current") or {}
        measurements.append((probe[1], probe[0], Environment(
            temp_c=float(vals.get("temperature_2m", 15.0)),
            wind_speed_ms=float(vals.get("wind_speed_10m", 0.0)),
            wind_direction_degree=float(vals.get("wind_direction_10m", 0.0)))))

    if not measurements:
        return lambda lat, lon: fallback

    def look_up(lat: float, lon: float) -> Environment:
        top = min(measurements, key=lambda m: haversine_m(lat, lon, m[0], m[1]))
        return top[2]

    return look_up


def _hourly_values(entry: dict, departure: datetime, duration_s: float,
                  nr: int, count: int) -> dict:
    """The values of the hour in which one arrives at support point `nr`.

    The support points lie evenly on the route; uniform driving is assumed.
    That is rough - a break or a charging stop shifts the arrival -, but an
    hour of deviation changes the temperature by a degree or two and not by
    ten.
    """
    share = nr / (count - 1) if count > 1 else 0.0
    moment = departure + timedelta(seconds=share * max(0.0, duration_s))
    hourly = entry["hourly"]
    i = _hour(hourly["time"], moment)
    return {name: hourly[name][i] for name in
            ("temperature_2m", "wind_speed_10m", "wind_direction_10m")
            if hourly[name][i] is not None}


def mean(points: list, departure: datetime | None = None,
               duration_s: float = 0.0) -> Environment:
    """A single value for the display ("calculated at 4 °C")."""
    fetch = along_route(points, departure=departure, duration_s=duration_s)
    probes = _select(points, SUPPORT_POINTS)
    vals = [fetch(p[1], p[0]) for p in probes] or [Environment()]
    return Environment(
        temp_c=round(sum(w.temp_c for w in vals) / len(vals), 1),
        wind_speed_ms=round(
            sum(w.wind_speed_ms for w in vals) / len(vals), 1),
        wind_direction_degree=vals[len(vals) // 2].wind_direction_degree)
