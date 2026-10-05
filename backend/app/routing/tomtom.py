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
TIMEOUT_GEOMETRIE_S = 40
TIMEOUT_ZUSAMMENFASSUNG_S = 20

# Eine Abfahrt in den nächsten Minuten ist "jetzt": Dann gilt der Live-Verkehr,
# und `departAt` bleibt weg.
JETZT_TOLERANZ = timedelta(minutes=5)

MAX_ALTERNATIVEN = 5
# Wie viele der nicht überholten Vorschläge höchstens nachgefahren werden. Jeder
# kostet eine Anfrage beim Routing von OpenRouteService.
MAX_NACHFAHREN = 3


class TomTomFehler(RuntimeError):
    """TomTom hat nicht geantwortet oder abgelehnt - mit einem Grund für das Log.

    Die Planung läuft ohne TomTom weiter; dieser Fehler bricht nichts ab.
    """


@dataclass
class Vorschlag:
    """Ein Weg, wie TomTom ihn sieht. Nur Zahlen und Punkte, nichts Gespeichertes."""
    punkte: list = field(default_factory=list)   # [(lat, lon), ...]
    strecke_m: float = 0.0
    zeit_s: float = 0.0              # mit Verkehr: live, oder zeitabhängig prognostiziert
    ohne_verkehr_s: float = 0.0      # bei freiem Fluss
    verkehr_s: float = 0.0           # Unterschied der beiden


@dataclass
class Verkehr:
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
    verzoegerung_s: float
    zeit_s: float
    ohne_verkehr_s: float
    prognose: bool = False       # zeitabhängig prognostiziert statt live


def schluessel() -> str:
    return os.environ.get("TOMTOM_API_KEY", "").strip()


def verfuegbar() -> bool:
    return bool(schluessel())


def _abfahrt(abfahrt: datetime | None) -> datetime | None:
    """Die Abfahrt, wenn sie später als in ein paar Minuten liegt - sonst None."""
    if abfahrt is None:
        return None
    if abfahrt.tzinfo is None:
        abfahrt = abfahrt.replace(tzinfo=timezone.utc)
    if abfahrt <= datetime.now(timezone.utc) + JETZT_TOLERANZ:
        return None
    return abfahrt.astimezone(timezone.utc)


def _abfahrt_parameter(abfahrt: datetime | None) -> dict:
    """`departAt` für TomTom: RFC 3339 in UTC. Ohne Zeitzone nähme TomTom die
    des Startpunkts an - das hiesse, eine Uhrzeit des Browsers anders zu
    lesen, als sie gemeint war."""
    spaeter = _abfahrt(abfahrt)
    if spaeter is None:
        return {}
    return {"departAt": spaeter.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _orte(start, ziel, zwischen=None) -> str:
    folge = [start] + list(zwischen or []) + [ziel]
    return ":".join(f"{lat:.5f},{lon:.5f}" for lat, lon in folge)


def _abfragen(orte: str, timeout: int, **parameter) -> dict:
    try:
        antwort = requests.get(
            f"{BASIS}/{orte}/json", timeout=timeout,
            params={"key": schluessel(), "traffic": "true",
                    "computeTravelTimeFor": "all", "routeType": "fastest",
                    "travelMode": "car", **parameter})
    except requests.RequestException as fehler:
        # Nur der Typ: `str(fehler)` enthielte die Adresse samt Schlüssel.
        raise TomTomFehler(f"TomTom nicht erreichbar ({type(fehler).__name__})") from None
    if antwort.status_code in (401, 403):
        raise TomTomFehler("TOMTOM_API_KEY wird abgelehnt - Schlüssel prüfen.")
    if antwort.status_code == 429:
        raise TomTomFehler("TomTom-Kontingent erschöpft.")
    if antwort.status_code >= 400:
        raise TomTomFehler(f"TomTom meldet HTTP {antwort.status_code}.")
    try:
        return antwort.json()
    except ValueError:
        raise TomTomFehler("TomTom lieferte keine lesbare Antwort.") from None


def _zahl(zusammenfassung: dict, name: str) -> float:
    wert = zusammenfassung.get(name)
    return float(wert) if isinstance(wert, (int, float)) else 0.0


def vorschlaege_lesen(daten: dict) -> list[Vorschlag]:
    """Die Wege aus einer calculateRoute-Antwort. Fehlerhafte Einträge fallen
    heraus, statt alles zu verwerfen: Eine Route ohne Punkte oder ohne Länge
    ist für jolt keine."""
    aus = []
    for route in daten.get("routes") or []:
        z = route.get("summary") or {}
        punkte = [(p["latitude"], p["longitude"])
                  for leg in route.get("legs") or []
                  for p in leg.get("points") or []
                  if isinstance(p, dict) and "latitude" in p and "longitude" in p]
        strecke = _zahl(z, "lengthInMeters")
        if len(punkte) < 2 or strecke <= 0:
            continue
        zeit = _zahl(z, "travelTimeInSeconds")
        ohne = _zahl(z, "noTrafficTravelTimeInSeconds") or zeit
        aus.append(Vorschlag(punkte=punkte, strecke_m=strecke, zeit_s=zeit,
                             ohne_verkehr_s=ohne, verkehr_s=max(0.0, zeit - ohne)))
    return aus


def alternativen(start: tuple[float, float], ziel: tuple[float, float],
                 maximal: int = MAX_ALTERNATIVEN,
                 abfahrt: datetime | None = None) -> list[Vorschlag]:
    """Die beste Route und bis zu `maximal` Alternativen, mit Geometrie.

    Mit `abfahrt` rechnet TomTom zeitabhängig: Freitag um vier ist eine
    andere Strasse die schnellste als Sonntag um drei (gemessen: 723 km statt
    712 km auf Reutlingen - Hamburg).
    """
    daten = _abfragen(_orte(start, ziel), TIMEOUT_GEOMETRIE_S,
                      maxAlternatives=max(0, min(maximal, MAX_ALTERNATIVEN)),
                      **_abfahrt_parameter(abfahrt))
    return vorschlaege_lesen(daten)


def verkehr(start: tuple[float, float], ziel: tuple[float, float],
            zwischen: list[tuple[float, float]],
            abfahrt: datetime | None = None) -> Verkehr | None:
    """Was der Verkehr auf dem Weg durch `zwischen` gerade kostet.

    Nur die Zusammenfassung - ein Kilobyte statt zweieinhalb Megabyte. Die
    Zwischenpunkte zwingen TomTom auf dieselbe Strasse, die jolt fährt; ohne
    sie rechnete TomTom für *seinen* Weg, und die Zahl hätte mit der Route
    nichts zu tun.
    """
    daten = _abfragen(_orte(start, ziel, zwischen), TIMEOUT_ZUSAMMENFASSUNG_S,
                      routeRepresentation="summaryOnly", **_abfahrt_parameter(abfahrt))
    routen = daten.get("routes") or []
    if not routen:
        return None
    z = routen[0].get("summary") or {}
    zeit = _zahl(z, "travelTimeInSeconds")
    if zeit <= 0:
        return None
    ohne = _zahl(z, "noTrafficTravelTimeInSeconds") or zeit
    return Verkehr(verzoegerung_s=max(0.0, zeit - ohne), zeit_s=zeit,
                   ohne_verkehr_s=ohne, prognose=_abfahrt(abfahrt) is not None)


def nicht_ueberholt(vorschlaege: list[Vorschlag]) -> list[Vorschlag]:
    """Die Vorschläge, die keiner der anderen sowohl kürzer als auch schneller
    übertrifft - nach Zeit sortiert, höchstens `MAX_NACHFAHREN`.

    Alles andere kann keinen Ladeplan haben, der es rettet: Mehr Strecke heisst
    mehr Energie, mehr Zeit heisst mehr Zeit. Das spart die Anfragen beim
    Routing, die sonst für aussichtslose Wege gestellt würden - bei vier von
    fünf Strecken, an denen das gemessen wurde, war das jede Alternative.
    """
    uebrig = [v for v in vorschlaege
              if not any(a is not v and a.strecke_m <= v.strecke_m
                         and a.zeit_s <= v.zeit_s
                         and (a.strecke_m < v.strecke_m or a.zeit_s < v.zeit_s)
                         for a in vorschlaege)]
    return sorted(uebrig, key=lambda v: v.zeit_s)[:MAX_NACHFAHREN]
