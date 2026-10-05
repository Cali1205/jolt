#!/usr/bin/env python3
"""Prüft das Wetter entlang der Route - `energie/wetter.py` - ohne Netz.

Die Frage, um die es geht: Für welche **Stunde** wird das Wetter geholt?
Bisher galt immer jetzt. Mit einer Abfahrtszeit morgen früh um sechs wäre das
das Nachmittagswetter von heute - bei Kälte der grösste Einzelposten des
Verbrauchs falsch. Jetzt gilt an jedem Stützpunkt die Stunde, in der man dort
ankommt: Abfahrt plus Anteil der Fahrzeit.

Die Open-Meteo-Antwort ist nachgebaut; Temperatur = Stundenindex, damit sich
aus dem Ergebnis ablesen lässt, welche Stunde gewählt wurde.

    ./tools/check_wetter.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pruefen import Pruefung, anwendung_bereitstellen  # noqa: E402

anwendung_bereitstellen("wetter", datenbank=False)

import requests                                       # noqa: E402

from app.energie import wetter                        # noqa: E402
from app.energie.modell import Umgebung               # noqa: E402

pruefe = Pruefung()

# Eine Strecke von Süd nach Nord: 11 Punkte [lon, lat]; sechs Stützpunkte
# fallen auf Index 0, 2, 4, 6, 8, 10.
PUNKTE = [[9.0, 48.0 + i * 0.5] for i in range(11)]
START = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) \
    - timedelta(hours=datetime.now(timezone.utc).hour)         # heute 00:00 UTC


def stunden(anzahl=384):
    return [(START + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M")
            for i in range(anzahl)]


def antwort_prognose(orte=6, anzahl=384, versetzt=0.0):
    """Je Ort: Temperatur = Stundenindex (+ Versatz je Ort), Wind = 5, Richtung 90."""
    eintraege = [{"hourly": {
        "time": stunden(anzahl),
        "temperature_2m": [i + versetzt * o for i in range(anzahl)],
        "wind_speed_10m": [5.0] * anzahl,
        "wind_direction_10m": [90] * anzahl}} for o in range(orte)]
    return eintraege if orte > 1 else eintraege[0]


def antwort_aktuell(orte=6):
    eintraege = [{"current": {"temperature_2m": 11.0 + o, "wind_speed_10m": 3.0,
                              "wind_direction_10m": 200}} for o in range(orte)]
    return eintraege if orte > 1 else eintraege[0]


class Antwort:
    def __init__(self, daten, status=200):
        self._daten, self.status_code = daten, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._daten


def mit_wetter(antwort, funktion):
    echt = requests.get

    def falsch(url, **kw):
        falsch.aufrufe.append(kw.get("params", {}))
        if isinstance(antwort, Exception):
            raise antwort
        return antwort
    falsch.aufrufe = []
    requests.get = falsch
    try:
        return funktion(), falsch.aufrufe
    finally:
        requests.get = echt


def temperaturen(hole):
    """Die Temperatur an den sechs Stützpunkten (Index 0, 2, ... 10)."""
    return [hole(PUNKTE[i][1], PUNKTE[i][0]).temp_c for i in range(0, 11, 2)]


def main() -> int:
    pruefe.abschnitt("Wie bisher: jetzt")
    hole, aufrufe = mit_wetter(Antwort(antwort_aktuell()),
                               lambda: wetter.entlang_route(PUNKTE))
    pruefe("current" in aufrufe[0] and "hourly" not in aufrufe[0],
           "ohne Abfahrt wird die aktuelle Messung geholt")
    pruefe(temperaturen(hole) == [11.0, 12.0, 13.0, 14.0, 15.0, 16.0],
           "und je Stützpunkt ausgewertet", str(temperaturen(hole)))
    jetzt_bald = datetime.now(timezone.utc) + timedelta(minutes=10)
    _, aufrufe = mit_wetter(Antwort(antwort_aktuell()),
                            lambda: wetter.entlang_route(PUNKTE, abfahrt=jetzt_bald, dauer_s=3600))
    pruefe("current" in aufrufe[0],
           "eine Abfahrt in zehn Minuten ist jetzt - die aktuelle Messung ist die genauere")

    pruefe.abschnitt("Abfahrt morgen früh")
    morgen_6 = (START + timedelta(days=1, hours=6)).replace(tzinfo=timezone.utc)
    # 12 Stunden Fahrzeit, sechs Stützpunkte: Ankunft nach 0, 2.4, 4.8, 7.2, 9.6, 12 h.
    hole, aufrufe = mit_wetter(Antwort(antwort_prognose()),
                               lambda: wetter.entlang_route(PUNKTE, abfahrt=morgen_6,
                                                            dauer_s=12 * 3600))
    p = aufrufe[0]
    pruefe("hourly" in p and "current" not in p and p["timezone"] == "UTC"
           and p["forecast_days"] == 16,
           "die stündliche Vorhersage in UTC, 16 Tage", str(p))
    basis = 24 + 6                         # Index von morgen 06:00
    erwartet = [basis + 0, basis + 2, basis + 5, basis + 7, basis + 10, basis + 12]
    pruefe(temperaturen(hole) == erwartet,
           "jeder Stützpunkt bekommt die Stunde seiner Ankunft: Abfahrt plus "
           "Anteil der Fahrzeit (0, 2,4, 4,8, 7,2, 9,6, 12 h auf die volle Stunde)",
           f"{temperaturen(hole)} statt {erwartet}")
    wind = hole(PUNKTE[0][1], PUNKTE[0][0])
    pruefe(wind.windgeschwindigkeit_ms == 5.0 and wind.windrichtung_grad == 90,
           "Wind und Richtung kommen mit")

    pruefe.abschnitt("Zeitzonen")
    plus2 = morgen_6.astimezone(timezone(timedelta(hours=2)))
    hole2, _ = mit_wetter(Antwort(antwort_prognose()),
                          lambda: wetter.entlang_route(PUNKTE, abfahrt=plus2, dauer_s=0))
    pruefe(temperaturen(hole2)[0] == basis,
           "dieselbe Abfahrt in +02:00 gibt dieselbe Stunde - Open-Meteo liefert "
           "UTC, und eine Verwechslung wären zwei Stunden Unterschied",
           str(temperaturen(hole2)[0]))
    naiv = morgen_6.replace(tzinfo=None)
    hole3, _ = mit_wetter(Antwort(antwort_prognose()),
                          lambda: wetter.entlang_route(PUNKTE, abfahrt=naiv, dauer_s=0))
    pruefe(temperaturen(hole3)[0] == basis, "ohne Zeitzone gilt UTC")

    pruefe.abschnitt("Grenzen")
    ohne_dauer, _ = mit_wetter(Antwort(antwort_prognose()),
                               lambda: wetter.entlang_route(PUNKTE, abfahrt=morgen_6, dauer_s=0))
    pruefe(len(set(temperaturen(ohne_dauer))) == 1,
           "ohne bekannte Fahrzeit gilt überall die Abfahrtsstunde - kein Absturz")
    weit = datetime.now(timezone.utc) + timedelta(days=20)
    hole, aufrufe = mit_wetter(Antwort(antwort_aktuell()),
                               lambda: wetter.entlang_route(PUNKTE, abfahrt=weit, dauer_s=3600))
    pruefe("current" in aufrufe[0] and temperaturen(hole)[0] == 11.0,
           "in 20 Tagen gibt es keine Vorhersage - es gilt das aktuelle Wetter, "
           "statt gar keine Route")
    kurz = antwort_prognose(anzahl=40)
    ende = (START + timedelta(days=1, hours=14)).replace(tzinfo=timezone.utc)
    hole, _ = mit_wetter(Antwort(kurz), lambda: wetter.entlang_route(PUNKTE, abfahrt=ende, dauer_s=48 * 3600))
    pruefe(max(temperaturen(hole)) == 39,
           "reicht die Vorhersage nicht bis zur Ankunft, gilt die letzte Stunde "
           "statt eines Fehlers", str(temperaturen(hole)))
    stunde = wetter._stunde(stunden(), START.replace(tzinfo=timezone.utc) - timedelta(hours=5))
    pruefe(stunde == 0, "und vor dem Anfang der Liste die erste")
    pruefe(wetter._stunde(stunden(), START.replace(tzinfo=timezone.utc) + timedelta(hours=7, minutes=29)) == 7
           and wetter._stunde(stunden(), START.replace(tzinfo=timezone.utc) + timedelta(hours=7, minutes=31)) == 8,
           "die nächstgelegene Stunde wird gewählt, nicht die vorherige")

    pruefe.abschnitt("Wenn Open-Meteo nicht liefert")
    ersatz = Umgebung(temp_c=-3.0)
    for name, antwort in (("ein HTTP-Fehler", Antwort({}, 500)),
                          ("ein Netzfehler", requests.ConnectionError("weg"))):
        hole, _ = mit_wetter(antwort if isinstance(antwort, Exception) else antwort,
                             lambda: wetter.entlang_route(PUNKTE, vorgabe=ersatz,
                                                          abfahrt=morgen_6, dauer_s=3600))
        pruefe(hole(48.0, 9.0).temp_c == -3.0,
               f"{name}: Die Route wird trotzdem gerechnet, mit der Vorgabe")
    kaputt = [{"hourly": {"time": stunden(10)}} for _ in range(6)]       # Werte fehlen
    hole, _ = mit_wetter(Antwort(kaputt),
                         lambda: wetter.entlang_route(PUNKTE, vorgabe=ersatz,
                                                      abfahrt=morgen_6, dauer_s=3600))
    pruefe(hole(48.0, 9.0).temp_c == -3.0,
           "eine Antwort ohne die erwarteten Werte ist kein Grund, die Route "
           "zu verwerfen")
    luecke = antwort_prognose()
    for e in luecke:
        e["hourly"]["temperature_2m"][basis] = None          # Open-Meteo kennt Lücken
    hole, _ = mit_wetter(Antwort(luecke),
                         lambda: wetter.entlang_route(PUNKTE, abfahrt=morgen_6, dauer_s=0))
    pruefe(hole(PUNKTE[0][1], PUNKTE[0][0]).temp_c == 15.0,
           "fehlt ein einzelner Wert, gilt die Vorgabe von 15 °C für diesen Wert, "
           "nicht ein Absturz")

    pruefe.abschnitt("Mittelwert für die Anzeige")
    mittel, _ = mit_wetter(Antwort(antwort_prognose()),
                           lambda: wetter.mittelwert(PUNKTE, abfahrt=morgen_6, dauer_s=12 * 3600))
    pruefe(abs(mittel.temp_c - sum(erwartet) / 6) < 0.2,
           "der Mittelwert für die Anzeige gilt für dieselben Stunden",
           str(mittel.temp_c))
    mittel_jetzt, _ = mit_wetter(Antwort(antwort_aktuell()), lambda: wetter.mittelwert(PUNKTE))
    pruefe(abs(mittel_jetzt.temp_c - 13.5) < 0.2, "und ohne Abfahrt wie bisher",
           str(mittel_jetzt.temp_c))

    return pruefe.bilanz()


if __name__ == "__main__":
    sys.exit(main())
