#!/usr/bin/env python3
"""Prüft das Wetter entlang der Route - `energie/weather.py` - ohne Netz.

Die Frage, um die es geht: Für welche **Stunde** wird das Wetter geholt?
Bisher galt immer jetzt. Mit einer Abfahrtszeit morgen früh um sechs wäre das
das Nachmittagswetter von heute - bei Kälte der grösste Einzelposten des
Verbrauchs falsch. Jetzt gilt an jedem Stützpunkt die Stunde, in der man dort
ankommt: Abfahrt plus Anteil der Fahrzeit.

Die Open-Meteo-Antwort ist nachgebaut; Temperatur = Stundenindex, damit sich
aus dem Ergebnis ablesen lässt, welche Stunde gewählt wurde.

    ./tools/check_weather.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from examine import Check, application_provide  # noqa: E402

application_provide("wetter", db_name=False)

import requests                                       # noqa: E402

from app.energy import weather                        # noqa: E402
from app.energy.model import Environment               # noqa: E402

verify = Check()

# Eine Strecke von Süd nach Nord: 11 Punkte [lon, lat]; sechs Stützpunkte
# fallen auf Index 0, 2, 4, 6, 8, 10.
POINTS = [[9.0, 48.0 + i * 0.5] for i in range(11)]
START = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) \
    - timedelta(hours=datetime.now(timezone.utc).hour)         # heute 00:00 UTC


def hrs(count=384):
    return [(START + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M")
            for i in range(count)]


def response_forecast(places=6, count=384, offset=0.0):
    """Je Ort: Temperatur = Stundenindex (+ Versatz je Ort), Wind = 5, Richtung 90."""
    entries = [{"hourly": {
        "time": hrs(count),
        "temperature_2m": [i + offset * o for i in range(count)],
        "wind_speed_10m": [5.0] * count,
        "wind_direction_10m": [90] * count}} for o in range(places)]
    return entries if places > 1 else entries[0]


def response_current(places=6):
    entries = [{"current": {"temperature_2m": 11.0 + o, "wind_speed_10m": 3.0,
                              "wind_direction_10m": 200}} for o in range(places)]
    return entries if places > 1 else entries[0]


class Response:
    def __init__(self, records, status=200):
        self._data, self.status_code = records, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


def with_weather(response, fn):
    real = requests.get

    def wrong(url, **kw):
        wrong.calls.append(kw.get("params", {}))
        if isinstance(response, Exception):
            raise response
        return response
    wrong.calls = []
    requests.get = wrong
    try:
        return fn(), wrong.calls
    finally:
        requests.get = real


def temperatures(fetch):
    """Die Temperatur an den sechs Stützpunkten (Index 0, 2, ... 10)."""
    return [fetch(POINTS[i][1], POINTS[i][0]).temp_c for i in range(0, 11, 2)]


def main() -> int:
    verify.section("Wie bisher: jetzt")
    fetch, calls = with_weather(Response(response_current()),
                               lambda: weather.along_route(POINTS))
    verify("current" in calls[0] and "hourly" not in calls[0],
           "ohne Abfahrt wird die aktuelle Messung geholt")
    verify(temperatures(fetch) == [11.0, 12.0, 13.0, 14.0, 15.0, 16.0],
           "und je Stützpunkt ausgewertet", str(temperatures(fetch)))
    now_soon = datetime.now(timezone.utc) + timedelta(minutes=10)
    _, calls = with_weather(Response(response_current()),
                            lambda: weather.along_route(POINTS, departure=now_soon, duration_s=3600))
    verify("current" in calls[0],
           "eine Abfahrt in zehn Minuten ist jetzt - die aktuelle Messung ist die genauere")

    verify.section("Abfahrt morgen früh")
    tomorrow_6 = (START + timedelta(days=1, hours=6)).replace(tzinfo=timezone.utc)
    # 12 Stunden Fahrzeit, sechs Stützpunkte: Ankunft nach 0, 2.4, 4.8, 7.2, 9.6, 12 h.
    fetch, calls = with_weather(Response(response_forecast()),
                               lambda: weather.along_route(POINTS, departure=tomorrow_6,
                                                            duration_s=12 * 3600))
    p = calls[0]
    verify("hourly" in p and "current" not in p and p["timezone"] == "UTC"
           and p["forecast_days"] == 16,
           "die stündliche Vorhersage in UTC, 16 Tage", str(p))
    basis = 24 + 6                         # Index von morgen 06:00
    expected = [basis + 0, basis + 2, basis + 5, basis + 7, basis + 10, basis + 12]
    verify(temperatures(fetch) == expected,
           "jeder Stützpunkt bekommt die Stunde seiner Ankunft: Abfahrt plus "
           "Anteil der Fahrzeit (0, 2,4, 4,8, 7,2, 9,6, 12 h auf die volle Stunde)",
           f"{temperatures(fetch)} statt {expected}")
    wind = fetch(POINTS[0][1], POINTS[0][0])
    verify(wind.wind_speed_ms == 5.0 and wind.wind_direction_degree == 90,
           "Wind und Richtung kommen mit")

    verify.section("Zeitzonen")
    plus2 = tomorrow_6.astimezone(timezone(timedelta(hours=2)))
    hole2, _ = with_weather(Response(response_forecast()),
                          lambda: weather.along_route(POINTS, departure=plus2, duration_s=0))
    verify(temperatures(hole2)[0] == basis,
           "dieselbe Abfahrt in +02:00 gibt dieselbe Stunde - Open-Meteo liefert "
           "UTC, und eine Verwechslung wären zwei Stunden Unterschied",
           str(temperatures(hole2)[0]))
    naiv = tomorrow_6.replace(tzinfo=None)
    hole3, _ = with_weather(Response(response_forecast()),
                          lambda: weather.along_route(POINTS, departure=naiv, duration_s=0))
    verify(temperatures(hole3)[0] == basis, "ohne Zeitzone gilt UTC")

    verify.section("Grenzen")
    without_duration, _ = with_weather(Response(response_forecast()),
                               lambda: weather.along_route(POINTS, departure=tomorrow_6, duration_s=0))
    verify(len(set(temperatures(without_duration))) == 1,
           "ohne bekannte Fahrzeit gilt überall die Abfahrtsstunde - kein Absturz")
    far = datetime.now(timezone.utc) + timedelta(days=20)
    fetch, calls = with_weather(Response(response_current()),
                               lambda: weather.along_route(POINTS, departure=far, duration_s=3600))
    verify("current" in calls[0] and temperatures(fetch)[0] == 11.0,
           "in 20 Tagen gibt es keine Vorhersage - es gilt das aktuelle Wetter, "
           "statt gar keine Route")
    short = response_forecast(count=40)
    end = (START + timedelta(days=1, hours=14)).replace(tzinfo=timezone.utc)
    fetch, _ = with_weather(Response(short), lambda: weather.along_route(POINTS, departure=end, duration_s=48 * 3600))
    verify(max(temperatures(fetch)) == 39,
           "reicht die Vorhersage nicht bis zur Ankunft, gilt die letzte Stunde "
           "statt eines Fehlers", str(temperatures(fetch)))
    hr = weather._hour(hrs(), START.replace(tzinfo=timezone.utc) - timedelta(hours=5))
    verify(hr == 0, "und vor dem Anfang der Liste die erste")
    verify(weather._hour(hrs(), START.replace(tzinfo=timezone.utc) + timedelta(hours=7, minutes=29)) == 7
           and weather._hour(hrs(), START.replace(tzinfo=timezone.utc) + timedelta(hours=7, minutes=31)) == 8,
           "die nächstgelegene Stunde wird gewählt, nicht die vorherige")

    verify.section("Wenn Open-Meteo nicht liefert")
    fallback = Environment(temp_c=-3.0)
    for name, response in (("ein HTTP-Fehler", Response({}, 500)),
                          ("ein Netzfehler", requests.ConnectionError("weg"))):
        fetch, _ = with_weather(response if isinstance(response, Exception) else response,
                             lambda: weather.along_route(POINTS, preset=fallback,
                                                          departure=tomorrow_6, duration_s=3600))
        verify(fetch(48.0, 9.0).temp_c == -3.0,
               f"{name}: Die Route wird trotzdem gerechnet, mit der Vorgabe")
    broken = [{"hourly": {"time": hrs(10)}} for _ in range(6)]       # Werte fehlen
    fetch, _ = with_weather(Response(broken),
                         lambda: weather.along_route(POINTS, preset=fallback,
                                                      departure=tomorrow_6, duration_s=3600))
    verify(fetch(48.0, 9.0).temp_c == -3.0,
           "eine Antwort ohne die erwarteten Werte ist kein Grund, die Route "
           "zu verwerfen")
    gap = response_forecast()
    for e in gap:
        e["hourly"]["temperature_2m"][basis] = None          # Open-Meteo kennt Lücken
    fetch, _ = with_weather(Response(gap),
                         lambda: weather.along_route(POINTS, departure=tomorrow_6, duration_s=0))
    verify(fetch(POINTS[0][1], POINTS[0][0]).temp_c == 15.0,
           "fehlt ein einzelner Wert, gilt die Vorgabe von 15 °C für diesen Wert, "
           "nicht ein Absturz")

    verify.section("Mittelwert für die Anzeige")
    avg, _ = with_weather(Response(response_forecast()),
                           lambda: weather.mean(POINTS, departure=tomorrow_6, duration_s=12 * 3600))
    verify(abs(avg.temp_c - sum(expected) / 6) < 0.2,
           "der Mittelwert für die Anzeige gilt für dieselben Stunden",
           str(avg.temp_c))
    avg_now, _ = with_weather(Response(response_current()), lambda: weather.mean(POINTS))
    verify(abs(avg_now.temp_c - 13.5) < 0.2, "und ohne Abfahrt wie bisher",
           str(avg_now.temp_c))

    return verify.balance()


if __name__ == "__main__":
    sys.exit(main())
