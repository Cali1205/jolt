"""Wetter entlang der Route über Open-Meteo (ohne Schlüssel, ohne Anmeldung).

Nicht ein Wert für die ganze Fahrt: Hamburg-München sind 800 km, da liegen
zwischen Start und Ziel im Winter regelmässig zehn Grad und ein anderer Wind.
Abgefragt werden deshalb mehrere Stützpunkte in einer einzigen Anfrage -
Open-Meteo nimmt kommagetrennte Koordinatenlisten entgegen.
"""
import logging
from datetime import datetime, timedelta, timezone

import requests

from ..geo import haversine_m
from .model import Environment

API = "https://api.open-meteo.com/v1/forecast"
SUPPORT_POINTS = 6
TIMEOUT = 8

# Open-Meteo liefert stündliche Werte für 16 Tage. Eine Reserve von einem Tag,
# damit die letzte Stunde nicht gerade am Rand liegt.
FORECAST_DAYS = 15
# Eine Abfahrt in den nächsten Minuten ist "jetzt": Der Unterschied zwischen
# der aktuellen und der stündlichen Vorhersage ist dort kleiner als die
# Messunsicherheit, und die aktuelle Abfrage ist die genauere.
NOW_TOLERANCE = timedelta(minutes=30)

log = logging.getLogger("uvicorn.error")


def _select(points: list, count: int) -> list:
    """Gleichmässig verteilte Stützpunkte, Start und Ziel immer dabei."""
    if len(points) <= count:
        return list(points)
    step = (len(points) - 1) / (count - 1)
    return [points[round(i * step)] for i in range(count)]


def _forecast_for(departure: datetime | None) -> bool:
    """Soll die stündliche Vorhersage gelten statt der aktuellen Messung?

    Ja, wenn die Abfahrt später als in einer halben Stunde liegt - und noch
    innerhalb dessen, was Open-Meteo kennt. Darüber hinaus wird mit dem
    aktuellen Wetter gerechnet und das im Log gesagt: Besser ein falsches
    Wetter, das man als solches erkennt, als gar keine Route.
    """
    if departure is None:
        return False
    now_ts = datetime.now(timezone.utc)
    if departure.tzinfo is None:
        departure = departure.replace(tzinfo=timezone.utc)
    if departure <= now_ts + NOW_TOLERANCE:
        return False
    if departure > now_ts + timedelta(days=FORECAST_DAYS):
        log.warning("Abfahrt in mehr als %d Tagen - dafür gibt es keine "
                    "Wettervorhersage, es gilt das aktuelle Wetter.", FORECAST_DAYS)
        return False
    return True


def _hour(times: list, moment: datetime) -> int:
    """Index der Stunde in `zeiten`, die dem Zeitpunkt am nächsten liegt.

    Open-Meteo liefert die Zeiten bei `timezone=UTC` als "2026-10-06T08:00"
    ohne Zone, aufsteigend und stündlich. Aus dem ersten Eintrag und dem
    Abstand ergibt sich der Index, ohne die Liste zu durchsuchen; er wird auf
    die vorhandenen Werte begrenzt.
    """
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    first_item = datetime.fromisoformat(times[0])
    index = round((moment - first_item).total_seconds() / 3600.0)
    return max(0, min(len(times) - 1, index))


def along_route(points: list, count: int = SUPPORT_POINTS,
                  preset: Environment | None = None,
                  departure: datetime | None = None, duration_s: float = 0.0):
    """Gibt eine Funktion (lat, lon) -> Umgebung zurück.

    `abfahrt` und `dauer_s`: Liegt die Abfahrt später als in einer halben
    Stunde, gilt die stündliche Vorhersage - und zwar an jedem Stützpunkt für
    die Stunde, in der man dort ankommt: Abfahrt plus Anteil der Fahrzeit,
    gleichmässig auf die Strecke verteilt. Eine Fahrt morgen früh um sechs
    soll nicht mit dem Nachmittagswetter von heute gerechnet werden; die
    Heizung ist der grösste Einzelposten der Kälte.

    Fällt die Abfrage aus, wird nicht abgebrochen, sondern mit `vorgabe`
    weitergerechnet: Eine Route ohne Wetter ist deutlich besser als gar keine
    Route, und die Live-Nachführung korrigiert den Fehler ohnehin innerhalb
    der ersten Kilometer.

    `vorgabe` ist beim Planen sinnvollerweise leer - dann gelten 15 °C und
    Windstille. **Unterwegs** ist genau das die falsche Annahme: Wer eine im
    Winter bei -5 °C gerechnete Fahrt neu plant und dabei auf 15 °C
    zurückfällt, verliert die Heizlast - den grössten Einzelposten der Kälte -
    und rechnet die Reststrecke zu optimistisch. Deshalb reicht die Umplanung
    dort die Temperatur der Fahrt herein statt sich auf die Vorgabe zu
    verlassen.
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
        log.warning("Wetterabfrage fehlgeschlagen (%s) - rechne mit %.0f °C.",
                    failure, fallback.temp_c)
        return lambda lat, lon: fallback

    # Bei einer einzelnen Koordinate liefert Open-Meteo ein Objekt, bei
    # mehreren eine Liste. Beides auf dieselbe Form bringen.
    entries = raw if isinstance(raw, list) else [raw]

    measurements: list[tuple[float, float, Environment]] = []
    for nr, (probe, entry) in enumerate(zip(probes, entries)):
        if forecast:
            try:
                vals = _hourly_values(entry, departure, duration_s, nr, len(probes))
            except (KeyError, IndexError, ValueError, TypeError) as failure:
                # Eine Antwort, die nicht so aussieht wie erwartet, ist kein
                # Grund, die Route zu verwerfen.
                log.warning("Wettervorhersage nicht lesbar (%s) - rechne mit "
                            "%.0f °C.", type(failure).__name__, fallback.temp_c)
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
    """Die Werte der Stunde, in der man am Stützpunkt `nr` ankommt.

    Die Stützpunkte liegen gleichmässig auf der Strecke; angenommen wird
    gleichmässige Fahrt. Das ist grob - eine Pause oder ein Ladestopp
    verschiebt die Ankunft -, aber eine Stunde Abweichung ändert die
    Temperatur um ein, zwei Grad und nicht um zehn.
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
    """Ein einzelner Wert für die Anzeige ("bei 4 °C gerechnet")."""
    fetch = along_route(points, departure=departure, duration_s=duration_s)
    probes = _select(points, SUPPORT_POINTS)
    vals = [fetch(p[1], p[0]) for p in probes] or [Environment()]
    return Environment(
        temp_c=round(sum(w.temp_c for w in vals) / len(vals), 1),
        wind_speed_ms=round(
            sum(w.wind_speed_ms for w in vals) / len(vals), 1),
        wind_direction_degree=vals[len(vals) // 2].wind_direction_degree)
