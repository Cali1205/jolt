#!/usr/bin/env python3
"""Prüft die Anbindung an TomTom - `routing/tomtom.py` - ohne Netz.

TomTom ist hier nur Berater: Vorschläge und Verkehr, nichts davon wird
gespeichert. Geprüft wird, was schiefgehen kann, ohne dass es jemand merkt:

- dass der Schlüssel nie in einer Fehlermeldung landet (er steht in der
  Adresse, und `requests` hängt die Adresse an jede Ausnahme),
- dass ein Vorschlag, der keinen Ladeplan retten kann, gar nicht erst
  nachgefahren wird (jeder kostet eine Anfrage beim Routing),
- dass kaputte Antworten die Planung nicht mitreissen.

Die Zahlen für die Vorschläge sind an echten Strecken gemessen (5.10.2026).

    ./tools/check_tomtom.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pruefen import Pruefung, anwendung_bereitstellen  # noqa: E402

anwendung_bereitstellen("tomtom", datenbank=False)

import requests                                       # noqa: E402

from app.routing import tomtom                        # noqa: E402
from app.routing.tomtom import TomTomFehler, Vorschlag  # noqa: E402

pruefe = Pruefung()
GEHEIM = "SCHLUESSEL-NIE-IM-LOG-0123456789"


class Antwort:
    def __init__(self, status=200, daten=None, ungueltig=False):
        self.status_code = status
        self._daten = daten
        self._ungueltig = ungueltig

    def json(self):
        if self._ungueltig:
            raise ValueError("kein JSON")
        return self._daten


def v(km, minuten, verkehr_min=0.0):
    return Vorschlag(punkte=[(48.0, 9.0), (48.1, 9.1)], strecke_m=km * 1000.0,
                     zeit_s=minuten * 60.0, ohne_verkehr_s=(minuten - verkehr_min) * 60.0,
                     verkehr_s=verkehr_min * 60.0)


def route_json(km, sekunden, punkte=3, verkehr=0, ohne=None):
    return {"summary": {"lengthInMeters": km * 1000, "travelTimeInSeconds": sekunden,
                        "trafficDelayInSeconds": verkehr,
                        "noTrafficTravelTimeInSeconds": ohne or sekunden},
            "legs": [{"points": [{"latitude": 48.0 + i * 0.01, "longitude": 9.0}
                                 for i in range(punkte)]}]}


def mit_antwort(antwort_oder_fehler, funktion):
    """`funktion` ausführen, während `requests.get` die vorgegebene Antwort
    liefert (oder die Ausnahme wirft). Gibt zurück, was `funktion` liefert oder
    die Ausnahme."""
    echt = requests.get

    def falsch(url, **kw):
        falsch.aufrufe.append((url, kw))
        if isinstance(antwort_oder_fehler, Exception):
            raise antwort_oder_fehler
        return antwort_oder_fehler
    falsch.aufrufe = []
    requests.get = falsch
    try:
        return funktion(), falsch.aufrufe
    except Exception as fehler:      # noqa: BLE001
        return fehler, falsch.aufrufe
    finally:
        requests.get = echt


def main() -> int:
    os.environ["TOMTOM_API_KEY"] = GEHEIM

    pruefe.abschnitt("Verfügbarkeit")
    pruefe(tomtom.verfuegbar(), "mit Schlüssel verfügbar")
    os.environ["TOMTOM_API_KEY"] = "  "
    pruefe(not tomtom.verfuegbar(), "ein leerer Schlüssel zählt als keiner")
    del os.environ["TOMTOM_API_KEY"]
    pruefe(not tomtom.verfuegbar(), "ohne Schlüssel nicht - die Planung läuft dann wie bisher")
    os.environ["TOMTOM_API_KEY"] = GEHEIM

    pruefe.abschnitt("Antwort lesen")
    daten = {"routes": [route_json(610, 19862, verkehr=37, ohne=19269),
                        route_json(657, 22203, verkehr=162)]}
    vs = tomtom.vorschlaege_lesen(daten)
    pruefe(len(vs) == 2 and abs(vs[0].strecke_m - 610000) < 1,
           "zwei Vorschläge mit Länge", str(len(vs)))
    pruefe(vs[0].zeit_s == 19862 and vs[0].ohne_verkehr_s == 19269
           and vs[0].verkehr_s == 37,
           "mit Zeit, Zeit ohne Verkehr und Verzögerung")
    pruefe(len(vs[0].punkte) == 3 and vs[0].punkte[0] == (48.0, 9.0),
           "und der Geometrie als (Breite, Länge)")
    kaputt = {"routes": [route_json(610, 19862), {"summary": {}, "legs": []},
                         {"summary": {"lengthInMeters": 0}, "legs": [{"points": []}]},
                         route_json(5, 100, punkte=1)]}
    pruefe(len(tomtom.vorschlaege_lesen(kaputt)) == 1,
           "ein Eintrag ohne Länge oder ohne Punkte fällt heraus, die anderen bleiben")
    pruefe(tomtom.vorschlaege_lesen({}) == [] and tomtom.vorschlaege_lesen({"routes": None}) == [],
           "keine Routen heisst keine Vorschläge, kein Fehler")
    fehlt = {"routes": [{"summary": {"lengthInMeters": 100000, "travelTimeInSeconds": 3600},
                         "legs": [{"points": [{"latitude": 1, "longitude": 2},
                                              {"latitude": 1.1, "longitude": 2}]}]}]}
    f = tomtom.vorschlaege_lesen(fehlt)[0]
    pruefe(f.verkehr_s == 0 and f.ohne_verkehr_s == 3600,
           "fehlt die Zeit ohne Verkehr, gilt die mit - keine Verzögerung erfunden")

    pruefe.abschnitt("Nicht überholte Vorschläge")
    # Gemessen: Reutlingen - Gueugnon. Jede Alternative länger UND langsamer.
    gueugnon = [v(538, 353), v(663, 394), v(682, 430), v(559, 460), v(618, 457), v(758, 502)]
    pruefe(len(tomtom.nicht_ueberholt(gueugnon)) == 1,
           "wo jede Alternative länger und langsamer ist, bleibt nur die beste - "
           "keine Anfrage beim Routing für aussichtslose Wege",
           str(len(tomtom.nicht_ueberholt(gueugnon))))
    # Gemessen: Reutlingen - München. Zwei Wege sind nicht zu schlagen.
    muenchen = [v(264, 149), v(257, 182), v(215, 163), v(240, 195), v(280, 222), v(304, 225)]
    front = tomtom.nicht_ueberholt(muenchen)
    pruefe([round(x.strecke_m / 1000) for x in front] == [264, 215],
           "die 215-km-Variante ist 48 km kürzer für 14 Minuten mehr und bleibt - "
           "genau der Fall, für den es sich lohnt, sie zu rechnen; nach Zeit sortiert",
           str([round(x.strecke_m / 1000) for x in front]))
    pruefe(len(tomtom.nicht_ueberholt([v(100, 60), v(100, 60)])) == 2,
           "zwei gleiche Vorschläge überholen einander nicht (das Routing legt "
           "sie später selbst zusammen)")
    viele = [v(300 - 10 * i, 150 + 15 * i) for i in range(6)]       # alle nicht überholt
    pruefe(len(tomtom.nicht_ueberholt(viele)) == tomtom.MAX_NACHFAHREN,
           "und höchstens drei werden nachgefahren - jeder kostet ein Stück vom "
           "Tageskontingent", str(len(tomtom.nicht_ueberholt(viele))))
    pruefe(tomtom.nicht_ueberholt([]) == [], "nichts bleibt nichts")

    pruefe.abschnitt("Fehler")
    ort = (48.0, 9.0)
    for status, wort in ((403, "abgelehnt"), (401, "abgelehnt"),
                         (429, "Kontingent"), (500, "HTTP 500")):
        fehler, _ = mit_antwort(Antwort(status), lambda: tomtom.alternativen(ort, (49.0, 9.0)))
        pruefe(isinstance(fehler, TomTomFehler) and wort in str(fehler),
               f"HTTP {status} wird zu einem Fehler, der es sagt", str(fehler))
    fehler, _ = mit_antwort(Antwort(ungueltig=True), lambda: tomtom.alternativen(ort, (49.0, 9.0)))
    pruefe(isinstance(fehler, TomTomFehler), "eine Antwort, die kein JSON ist, auch")

    # Der Schlüssel steht in der Adresse, und requests hängt die Adresse an
    # die Ausnahme. Sie darf nie in der Meldung stehen.
    boese = requests.exceptions.ConnectionError(
        f"HTTPSConnectionPool: Max retries exceeded with url: /routing/1/x?key={GEHEIM}")
    fehler, _ = mit_antwort(boese, lambda: tomtom.alternativen(ort, (49.0, 9.0)))
    pruefe(isinstance(fehler, TomTomFehler) and GEHEIM not in str(fehler),
           "ein Verbindungsfehler verrät den Schlüssel nicht - sonst stünde er im Log",
           str(fehler))
    pruefe(isinstance(fehler, TomTomFehler) and fehler.__cause__ is None,
           "und auch nicht über die verkettete Ausnahme, die ein Log ausgibt")
    for status in (403, 429, 500):
        fehler, _ = mit_antwort(Antwort(status), lambda: tomtom.verkehr(ort, (49.0, 9.0), []))
        pruefe(isinstance(fehler, TomTomFehler) and GEHEIM not in str(fehler),
               f"HTTP {status} nennt den Schlüssel nicht")

    pruefe.abschnitt("Anfrage")
    _, aufrufe = mit_antwort(Antwort(200, {"routes": [route_json(10, 600)]}),
                             lambda: tomtom.alternativen((48.0, 9.0), (49.0, 10.0)))
    url, kw = aufrufe[0]
    pruefe("48.00000,9.00000:49.00000,10.00000" in url,
           "Start und Ziel stehen als Breite,Länge in der Adresse", url)
    pruefe(kw["params"]["traffic"] == "true" and kw["params"]["maxAlternatives"] == 5,
           "mit Verkehr und fünf Alternativen")
    pruefe(kw["params"].get("routeRepresentation") != "summaryOnly",
           "und mit Geometrie - die braucht es für die Zwischenpunkte")
    pruefe(kw["timeout"] >= 30,
           "mit Zeit für eine Antwort von zweieinhalb Megabyte")
    _, aufrufe = mit_antwort(Antwort(200, {"routes": [route_json(10, 600)]}),
                             lambda: tomtom.alternativen((48.0, 9.0), (49.0, 10.0), maximal=99))
    pruefe(aufrufe[0][1]["params"]["maxAlternatives"] == 5,
           "mehr als fünf verlangt TomTom nicht - zu hoch gegriffen wird gedeckelt")

    pruefe.abschnitt("Verkehr")
    zusammenfassung = {"routes": [{"summary": {"travelTimeInSeconds": 23340,
                                               "trafficDelayInSeconds": 702,
                                               "noTrafficTravelTimeInSeconds": 22638}}]}
    ergebnis, aufrufe = mit_antwort(
        Antwort(200, zusammenfassung),
        lambda: tomtom.verkehr((48.0, 9.0), (53.0, 10.0), [(50.0, 9.5), (51.5, 9.8)]))
    pruefe(ergebnis is not None and abs(ergebnis.verzoegerung_s / 60 - 11.7) < 0.01,
           "11,7 Minuten Verzögerung (Reutlingen - Hamburg, 5.10.)",
           str(ergebnis))
    url, kw = aufrufe[0]
    pruefe(url.split("calculateRoute/")[1].count(":") == 3 and "50.00000,9.50000" in url,
           "die Zwischenpunkte stehen in der Adresse - sie zwingen TomTom auf "
           "dieselbe Strasse, die jolt fährt", url)
    pruefe(kw["params"]["routeRepresentation"] == "summaryOnly",
           "und es kommt nur die Zusammenfassung zurück, nicht die Geometrie")
    leer, _ = mit_antwort(Antwort(200, {"routes": []}),
                          lambda: tomtom.verkehr(ort, (49.0, 9.0), []))
    pruefe(leer is None, "keine Route heisst: keine Angabe, kein Fehler")
    null, _ = mit_antwort(Antwort(200, {"routes": [{"summary": {}}]}),
                          lambda: tomtom.verkehr(ort, (49.0, 9.0), []))
    pruefe(null is None, "und eine Zusammenfassung ohne Zeit auch")

    return pruefe.bilanz()


if __name__ == "__main__":
    sys.exit(main())
