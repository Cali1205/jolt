"""TomTom als Berater: Vorschläge und Verkehr - nie als Quelle der gespeicherten Route.

**Was TomTom hier tut, und was nicht.** OpenRouteService bleibt die Quelle jeder
Route, die jolt speichert (Geometrie **mit Höhe**, Tempo je Teilstück - beides
braucht das Verbrauchsmodell, und TomTom liefert keines von beiden). TomTom
beantwortet zwei Fragen, die OpenRouteService nicht beantworten kann:

1. *Welche Wege gibt es noch?* `maxAlternatives` liefert bis zu fünf, ohne
   Längengrenze - OpenRouteService lehnt Alternativen ab 100 km ab.
2. *Was kostet der Verkehr?* Jede Route hat ihre eigene Verzögerung, aus dem
   Live-Verkehr. OpenRouteService kennt keinen.

**Warum nichts davon gespeichert wird.** Die TomTom-Bedingungen (Klausel 11.4)
erlauben Ergebnisse nur kurzzeitig im Zwischenspeicher und verbieten abgeleitete
Datenbanken. Deshalb gilt hier: Aus einem Vorschlag werden *Zwischenpunkte*
gewählt, die das Routing von OpenRouteService abfährt - gespeichert wird dessen
Strasse, nicht die von TomTom. Die Verzögerung geht als Zahl in die Antwort an
den Browser und von dort nirgends hin. Diese Datei schreibt weder in eine
Datenbank noch in eine Datei, und sie loggt keine Koordinaten.

**Der Schlüssel steht in der Adresse.** TomTom nimmt ihn nur als Parameter, und
`requests` hängt die Adresse an jede Ausnahme. Deshalb wird nie `str(fehler)`
weitergegeben, sondern nur der Typ - sonst stünde der Schlüssel im Log.
"""
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("uvicorn.error")

BASIS = "https://api.tomtom.com/routing/1/calculateRoute"

# Eine Antwort mit Geometrie ist für 600 km rund zweieinhalb Megabyte gross.
TIMEOUT_GEOMETRY_S = 40
TIMEOUT_SUMMARY_S = 20

# Eine Abfahrt in den nächsten Minuten ist "jetzt": Dann gilt der Live-Verkehr,
# und `departAt` bleibt weg.
NOW_TOLERANCE = timedelta(minutes=5)

MAX_ALTERNATIVEN = 5
# Wie viele der nicht überholten Vorschläge höchstens nachgefahren werden. Jeder
# kostet eine Anfrage beim Routing von OpenRouteService.
MAX_FOLLOW = 3


class TomTomError(RuntimeError):
    """TomTom hat nicht geantwortet oder abgelehnt - mit einem Grund für das Log.

    Die Planung läuft ohne TomTom weiter; dieser Fehler bricht nichts ab.
    """


@dataclass
class Suggestion:
    """Ein Weg, wie TomTom ihn sieht. Nur Zahlen und Punkte, nichts Gespeichertes."""
    points: list = field(default_factory=list)   # [(lat, lon), ...]
    distance_m: float = 0.0
    time_s: float = 0.0              # mit Verkehr: live, oder zeitabhängig prognostiziert
    without_traffic_s: float = 0.0      # bei freiem Fluss
    traffic_s: float = 0.0           # Unterschied der beiden


@dataclass
class Traffic:
    """Was der Verkehr auf einer bestimmten Strecke kostet - als Zahlen.

    `verzoegerung_s` ist der **ganze** Verkehrseinfluss: Zeit mit Verkehr
    minus Zeit bei freiem Fluss. Nicht `trafficDelayInSeconds`: Das Feld meint
    laut Dokumentation die Verzögerung nach *Echtzeit*-Verkehrsinformation und
    taugt deshalb für eine spätere Abfahrt nicht. Gemessen am 5.10.2026,
    Reutlingen - Hamburg: Für Freitag 16 Uhr stand dort +6,5 min, obwohl die
    zeitabhängige Prognose 28 Minuten über dem freien Fluss liegt; in 90 Tagen
    stand 0,0. Die Differenz der beiden Reisezeiten ist in beiden Fällen
    richtig und für "jetzt" die vollständigere Zahl (+21 gegen +12,9 min).
    """
    delay_s: float
    time_s: float
    without_traffic_s: float
    forecast: bool = False       # zeitabhängig prognostiziert statt live


def keyname() -> str:
    return os.environ.get("TOMTOM_API_KEY", "").strip()


def obtainable() -> bool:
    return bool(keyname())


def _departure(departure: datetime | None) -> datetime | None:
    """Die Abfahrt, wenn sie später als in ein paar Minuten liegt - sonst None."""
    if departure is None:
        return None
    if departure.tzinfo is None:
        departure = departure.replace(tzinfo=timezone.utc)
    if departure <= datetime.now(timezone.utc) + NOW_TOLERANCE:
        return None
    return departure.astimezone(timezone.utc)


def _departure_parameter(departure: datetime | None) -> dict:
    """`departAt` für TomTom: RFC 3339 in UTC. Ohne Zeitzone nähme TomTom die
    des Startpunkts an - das hiesse, eine Uhrzeit des Browsers anders zu
    lesen, als sie gemeint war."""
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
            params={"key": keyname(), "traffic": "true",
                    "computeTravelTimeFor": "all", "routeType": "fastest",
                    "travelMode": "car", **parameter})
    except requests.RequestException as failure:
        # Nur der Typ: `str(fehler)` enthielte die Adresse samt Schlüssel.
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
    val = summary.get(name)
    return float(val) if isinstance(val, (int, float)) else 0.0


def read_suggestions(records: dict) -> list[Suggestion]:
    """Die Wege aus einer calculateRoute-Antwort. Fehlerhafte Einträge fallen
    heraus, statt alles zu verwerfen: Eine Route ohne Punkte oder ohne Länge
    ist für jolt keine."""
    origin_of = []
    for route in records.get("routes") or []:
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
        origin_of.append(Suggestion(points=points, distance_m=distance, time_s=timestamp,
                             without_traffic_s=without, traffic_s=max(0.0, timestamp - without)))
    return origin_of


def alternativen(start: tuple[float, float], destination: tuple[float, float],
                 maximal: int = MAX_ALTERNATIVEN,
                 departure: datetime | None = None) -> list[Suggestion]:
    """Die beste Route und bis zu `maximal` Alternativen, mit Geometrie.

    Mit `abfahrt` rechnet TomTom zeitabhängig: Freitag um vier ist eine
    andere Strasse die schnellste als Sonntag um drei (gemessen: 723 km statt
    712 km auf Reutlingen - Hamburg).
    """
    records = _query(_places(start, destination), TIMEOUT_GEOMETRY_S,
                      maxAlternatives=max(0, min(maximal, MAX_ALTERNATIVEN)),
                      **_departure_parameter(departure))
    return read_suggestions(records)


def traffic(start: tuple[float, float], destination: tuple[float, float],
            between: list[tuple[float, float]],
            departure: datetime | None = None) -> Traffic | None:
    """Was der Verkehr auf dem Weg durch `zwischen` gerade kostet.

    Nur die Zusammenfassung - ein Kilobyte statt zweieinhalb Megabyte. Die
    Zwischenpunkte zwingen TomTom auf dieselbe Strasse, die jolt fährt; ohne
    sie rechnete TomTom für *seinen* Weg, und die Zahl hätte mit der Route
    nichts zu tun.
    """
    records = _query(_places(start, destination, between), TIMEOUT_SUMMARY_S,
                      routeRepresentation="summaryOnly", **_departure_parameter(departure))
    routes = records.get("routes") or []
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
    """Die Vorschläge, die keiner der anderen sowohl kürzer als auch schneller
    übertrifft - nach Zeit sortiert, höchstens `MAX_NACHFAHREN`.

    Alles andere kann keinen Ladeplan haben, der es rettet: Mehr Strecke heisst
    mehr Energie, mehr Zeit heisst mehr Zeit. Das spart die Anfragen beim
    Routing, die sonst für aussichtslose Wege gestellt würden - bei vier von
    fünf Strecken, an denen das gemessen wurde, war das jede Alternative.
    """
    left = [v for v in suggestions
              if not any(a is not v and a.distance_m <= v.distance_m
                         and a.time_s <= v.time_s
                         and (a.distance_m < v.distance_m or a.time_s < v.time_s)
                         for a in suggestions)]
    return sorted(left, key=lambda v: v.time_s)[:MAX_FOLLOW]
