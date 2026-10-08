#!/usr/bin/env python3
"""Checks the TomTom integration - `routing/tomtom.py` - without network.

TomTom is only an adviser here: suggestions and traffic, none of it is
stored. What is checked is what can go wrong without anyone noticing:

- that the key never ends up in an error message (it is part of the
  address, and `requests` attaches the address to every exception),
- that a suggestion that cannot rescue a charging plan is not even
  driven (each one costs a request to the routing service),
- that broken responses do not drag the planning down with them.

The numbers for the suggestions were measured on real routes (5 Oct 2026).

    ./tools/check_tomtom.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from examine import Check, application_provide  # noqa: E402

application_provide("tomtom", db_name=False)

import requests                                       # noqa: E402

from app.routing import tomtom                        # noqa: E402
from app.routing.tomtom import TomTomError, Suggestion  # noqa: E402

verify = Check()
SECRET = "SCHLUESSEL-NIE-IM-LOG-0123456789"


class Response:
    def __init__(self, status=200, records=None, invalid=False):
        self.status_code = status
        self._data = records
        self._invalid = invalid

    def json(self):
        if self._invalid:
            raise ValueError("kein JSON")
        return self._data


def v(km, mins, traffic_min=0.0):
    return Suggestion(points=[(48.0, 9.0), (48.1, 9.1)], distance_m=km * 1000.0,
                     time_s=mins * 60.0, without_traffic_s=(mins - traffic_min) * 60.0,
                     traffic_s=traffic_min * 60.0)


def route_json(km, seconds, points=3, traffic=0, without=None):
    return {"summary": {"lengthInMeters": km * 1000, "travelTimeInSeconds": seconds,
                        "trafficDelayInSeconds": traffic,
                        "noTrafficTravelTimeInSeconds": without or seconds},
            "legs": [{"points": [{"latitude": 48.0 + i * 0.01, "longitude": 9.0}
                                 for i in range(points)]}]}


def with_response(response_or_error, fn):
    """Run `fn` while `requests.get` returns the given response
    (or raises the exception). Returns what `fn` returns or
    the exception."""
    real = requests.get

    def wrong(url, **kw):
        wrong.calls.append((url, kw))
        if isinstance(response_or_error, Exception):
            raise response_or_error
        return response_or_error
    wrong.calls = []
    requests.get = wrong
    try:
        return fn(), wrong.calls
    except Exception as failure:      # noqa: BLE001
        return failure, wrong.calls
    finally:
        requests.get = real


def main() -> int:
    os.environ["TOMTOM_API_KEY"] = SECRET

    verify.section("Verfügbarkeit")
    verify(tomtom.obtainable(), "mit Schlüssel verfügbar")
    os.environ["TOMTOM_API_KEY"] = "  "
    verify(not tomtom.obtainable(), "ein leerer Schlüssel zählt als keiner")
    del os.environ["TOMTOM_API_KEY"]
    verify(not tomtom.obtainable(), "ohne Schlüssel nicht - die Planung läuft dann wie bisher")
    os.environ["TOMTOM_API_KEY"] = SECRET

    verify.section("Antwort lesen")
    records = {"routes": [route_json(610, 19862, traffic=37, without=19269),
                        route_json(657, 22203, traffic=162)]}
    vs = tomtom.read_suggestions(records)
    verify(len(vs) == 2 and abs(vs[0].distance_m - 610000) < 1,
           "zwei Vorschläge mit Länge", str(len(vs)))
    verify(vs[0].time_s == 19862 and vs[0].without_traffic_s == 19269
           and vs[0].traffic_s == 593,
           "mit Zeit, Zeit ohne Verkehr und dem Verkehrseinfluss - der Differenz, "
           "nicht dem Feld trafficDelayInSeconds (37), das nur die "
           "Echtzeit-Verzögerung meint", str(vs[0].traffic_s))
    verify(len(vs[0].points) == 3 and vs[0].points[0] == (48.0, 9.0),
           "und der Geometrie als (Breite, Länge)")
    broken = {"routes": [route_json(610, 19862), {"summary": {}, "legs": []},
                         {"summary": {"lengthInMeters": 0}, "legs": [{"points": []}]},
                         route_json(5, 100, points=1)]}
    verify(len(tomtom.read_suggestions(broken)) == 1,
           "ein Eintrag ohne Länge oder ohne Punkte fällt heraus, die anderen bleiben")
    verify(tomtom.read_suggestions({}) == [] and tomtom.read_suggestions({"routes": None}) == [],
           "keine Routen heisst keine Vorschläge, kein Fehler")
    missing = {"routes": [{"summary": {"lengthInMeters": 100000, "travelTimeInSeconds": 3600},
                         "legs": [{"points": [{"latitude": 1, "longitude": 2},
                                              {"latitude": 1.1, "longitude": 2}]}]}]}
    f = tomtom.read_suggestions(missing)[0]
    verify(f.traffic_s == 0 and f.without_traffic_s == 3600,
           "fehlt die Zeit ohne Verkehr, gilt die mit - keine Verzögerung erfunden")

    verify.section("Nicht überholte Vorschläge")
    # Measured: Reutlingen - Gueugnon. Every alternative longer AND slower.
    gueugnon = [v(538, 353), v(663, 394), v(682, 430), v(559, 460), v(618, 457), v(758, 502)]
    verify(len(tomtom.not_overtaken(gueugnon)) == 1,
           "wo jede Alternative länger und langsamer ist, bleibt nur die beste - "
           "keine Anfrage beim Routing für aussichtslose Wege",
           str(len(tomtom.not_overtaken(gueugnon))))
    # Measured: Reutlingen - München. Two routes cannot be beaten.
    munich = [v(264, 149), v(257, 182), v(215, 163), v(240, 195), v(280, 222), v(304, 225)]
    front = tomtom.not_overtaken(munich)
    verify([round(x.distance_m / 1000) for x in front] == [264, 215],
           "die 215-km-Variante ist 48 km kürzer für 14 Minuten mehr und bleibt - "
           "genau der Fall, für den es sich lohnt, sie zu rechnen; nach Zeit sortiert",
           str([round(x.distance_m / 1000) for x in front]))
    verify(len(tomtom.not_overtaken([v(100, 60), v(100, 60)])) == 2,
           "zwei gleiche Vorschläge überholen einander nicht (das Routing legt "
           "sie später selbst zusammen)")
    many = [v(300 - 10 * i, 150 + 15 * i) for i in range(6)]       # none overtaken
    verify(len(tomtom.not_overtaken(many)) == tomtom.MAX_FOLLOW,
           "und höchstens drei werden nachgefahren - jeder kostet ein Stück vom "
           "Tageskontingent", str(len(tomtom.not_overtaken(many))))
    verify(tomtom.not_overtaken([]) == [], "nichts bleibt nichts")

    verify.section("Fehler")
    city = (48.0, 9.0)
    for status, word in ((403, "abgelehnt"), (401, "abgelehnt"),
                         (429, "Kontingent"), (500, "HTTP 500")):
        failure, _ = with_response(Response(status), lambda: tomtom.alternativen(city, (49.0, 9.0)))
        verify(isinstance(failure, TomTomError) and word in str(failure),
               f"HTTP {status} wird zu einem Fehler, der es sagt", str(failure))
    failure, _ = with_response(Response(invalid=True), lambda: tomtom.alternativen(city, (49.0, 9.0)))
    verify(isinstance(failure, TomTomError), "eine Antwort, die kein JSON ist, auch")

    # The key is part of the address, and requests attaches the address to the
    # exception. It must never appear in the message.
    bad = requests.exceptions.ConnectionError(
        f"HTTPSConnectionPool: Max retries exceeded with url: /routing/1/x?key={SECRET}")
    failure, _ = with_response(bad, lambda: tomtom.alternativen(city, (49.0, 9.0)))
    verify(isinstance(failure, TomTomError) and SECRET not in str(failure),
           "ein Verbindungsfehler verrät den Schlüssel nicht - sonst stünde er im Log",
           str(failure))
    verify(isinstance(failure, TomTomError) and failure.__cause__ is None,
           "und auch nicht über die verkettete Ausnahme, die ein Log ausgibt")
    for status in (403, 429, 500):
        failure, _ = with_response(Response(status), lambda: tomtom.traffic(city, (49.0, 9.0), []))
        verify(isinstance(failure, TomTomError) and SECRET not in str(failure),
               f"HTTP {status} nennt den Schlüssel nicht")

    verify.section("Anfrage")
    _, calls = with_response(Response(200, {"routes": [route_json(10, 600)]}),
                             lambda: tomtom.alternativen((48.0, 9.0), (49.0, 10.0)))
    url, kw = calls[0]
    verify("48.00000,9.00000:49.00000,10.00000" in url,
           "Start und Ziel stehen als Breite,Länge in der Adresse", url)
    verify(kw["params"]["traffic"] == "true" and kw["params"]["maxAlternatives"] == 5,
           "mit Verkehr und fünf Alternativen")
    verify(kw["params"].get("routeRepresentation") != "summaryOnly",
           "und mit Geometrie - die braucht es für die Zwischenpunkte")
    verify(kw["timeout"] >= 30,
           "mit Zeit für eine Antwort von zweieinhalb Megabyte")
    _, calls = with_response(Response(200, {"routes": [route_json(10, 600)]}),
                             lambda: tomtom.alternativen((48.0, 9.0), (49.0, 10.0), maximal=99))
    verify(calls[0][1]["params"]["maxAlternatives"] == 5,
           "mehr als fünf verlangt TomTom nicht - zu hoch gegriffen wird gedeckelt")

    verify.section("Verkehr")
    # trafficDelayInSeconds is deliberately placed next to it and is wrong: it
    # only means the real-time delay (measured +12.9 min, where the travel times
    # show a difference of 21 minutes).
    summary = {"routes": [{"summary": {"travelTimeInSeconds": 23340,
                                               "trafficDelayInSeconds": 100,
                                               "noTrafficTravelTimeInSeconds": 22638}}]}
    result, calls = with_response(
        Response(200, summary),
        lambda: tomtom.traffic((48.0, 9.0), (53.0, 10.0), [(50.0, 9.5), (51.5, 9.8)]))
    verify(result is not None and abs(result.delay_s / 60 - 11.7) < 0.01,
           "11,7 Minuten Verkehrseinfluss aus der Differenz der Reisezeiten, nicht "
           "aus dem Echtzeit-Feld daneben", str(result))
    url, kw = calls[0]
    verify(url.split("calculateRoute/")[1].count(":") == 3 and "50.00000,9.50000" in url,
           "die Zwischenpunkte stehen in der Adresse - sie zwingen TomTom auf "
           "dieselbe Strasse, die jolt fährt", url)
    verify(kw["params"]["routeRepresentation"] == "summaryOnly",
           "und es kommt nur die Zusammenfassung zurück, nicht die Geometrie")
    empty, _ = with_response(Response(200, {"routes": []}),
                          lambda: tomtom.traffic(city, (49.0, 9.0), []))
    verify(empty is None, "keine Route heisst: keine Angabe, kein Fehler")
    null, _ = with_response(Response(200, {"routes": [{"summary": {}}]}),
                          lambda: tomtom.traffic(city, (49.0, 9.0), []))
    verify(null is None, "und eine Zusammenfassung ohne Zeit auch")

    verify.section("Abfahrtszeit")
    from datetime import datetime, timedelta, timezone
    later = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=4)
    ant = {"routes": [route_json(723, 23400, without=21720)]}      # Fri 4 pm, measured

    def params_from(departure):
        _, calls = with_response(Response(200, ant),
                                 lambda: tomtom.alternativen((48.0, 9.0), (53.0, 10.0),
                                                             departure=departure))
        return calls[0][1]["params"]

    verify(params_from(later)["departAt"] == later.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "eine spätere Abfahrt geht als departAt in UTC mit Z hinaus")
    verify("departAt" not in params_from(None), "ohne Abfahrt gibt es kein departAt")
    verify("departAt" not in params_from(datetime.now(timezone.utc) + timedelta(minutes=1)),
           "und eine Abfahrt in der nächsten Minute ist jetzt - dann gilt der Live-Verkehr")
    verify("departAt" not in params_from(datetime.now(timezone.utc) - timedelta(days=1)),
           "eine vergangene Abfahrt wird nie an TomTom geschickt")
    naiv = (later + timedelta(hours=1)).replace(tzinfo=None)
    verify(params_from(naiv)["departAt"].endswith("Z")
           and params_from(naiv)["departAt"].startswith(naiv.strftime("%Y-%m-%dT%H")),
           "ohne Zeitzone gilt UTC - nicht die des Startpunkts, die TomTom sonst "
           "annähme und die eine andere Uhrzeit meinte")
    plus2 = datetime(2026, 10, 9, 16, 0, tzinfo=timezone(timedelta(hours=2))) + timedelta(days=365)
    verify(params_from(plus2)["departAt"].endswith("T14:00:00Z"),
           "16 Uhr in +02:00 wird zu 14 Uhr UTC")

    friday, calls = with_response(
        Response(200, {"routes": [{"summary": {"travelTimeInSeconds": 23400,
                                              "trafficDelayInSeconds": 390,
                                              "noTrafficTravelTimeInSeconds": 21720}}]}),
        lambda: tomtom.traffic(city, (53.0, 10.0), [], departure=later))
    verify(abs(friday.delay_s / 60 - 28.0) < 0.01 and friday.forecast is True,
           "Freitag 16 Uhr: 28 Minuten Verkehrseinfluss (gemessen), als Prognose "
           "gekennzeichnet - nicht die 6,5 aus dem Echtzeit-Feld",
           str(friday))
    now_v, _ = with_response(Response(200, summary),
                             lambda: tomtom.traffic(city, (53.0, 10.0), []))
    verify(now_v.forecast is False, "ohne Abfahrt ist es live")
    far, _ = with_response(
        Response(200, {"routes": [{"summary": {"travelTimeInSeconds": 22440,
                                              "trafficDelayInSeconds": 0,
                                              "noTrafficTravelTimeInSeconds": 21720}}]}),
        lambda: tomtom.traffic(city, (53.0, 10.0), [], departure=later))
    verify(abs(far.delay_s / 60 - 12.0) < 0.01,
           "weit in der Zukunft steht im Echtzeit-Feld 0, die zeitabhängige "
           "Prognose aber 12 Minuten - die Differenz ist die richtige Zahl")
    faster, _ = with_response(
        Response(200, {"routes": [{"summary": {"travelTimeInSeconds": 21000,
                                              "noTrafficTravelTimeInSeconds": 21720}}]}),
        lambda: tomtom.traffic(city, (53.0, 10.0), []))
    verify(faster.delay_s == 0.0,
           "eine Reisezeit unter dem freien Fluss gibt keine negative Verzögerung")

    return verify.balance()


if __name__ == "__main__":
    sys.exit(main())
