"""Gefahrene Strecken als Routenkandidaten.

**Warum.** Für ein Elektroauto ist die schnellste Strasse nicht automatisch die
beste, und die Alternativen, die sich rechnen lassen, taugen nichts: Auf der
Messstrecke Mâcon - Reutlingen verlor jeder der zwölf geometrisch erzeugten
Umwege gegen die schnellste Route (siehe `varianten.py`). Die Route, die
tatsächlich gewann - 59 km kürzer, 13 Minuten länger, unterm Strich 2 bis 5 €
billiger -, kam aus **zwei Punkten der wirklich gefahrenen Strecke**.
Erfahrung schlägt Geometrie: Wer eine Strecke schon gefahren ist, kennt einen
Weg, den kein Kantengewicht kennt.

**Was hier passiert.** Aus den Messpunkten einer früheren Fahrt (egal ob
aufgezeichnet oder geplant und gefahren) wird ein Pfad. Passt er zu Start und
Ziel der neuen Anfrage - vorwärts oder rückwärts, ganz oder als Teilstück -,
werden Zwischenpunkte entlang des Pfads gewählt. Das Routing fährt sie ab; was
dabei herauskommt, ist eine Strasse und keine Luftlinie.

Dieses Modul kennt weder Datenbank noch Netz. Es nimmt Punktlisten und gibt
Punktlisten zurück - deshalb lässt es sich ohne beides prüfen.
"""
from dataclasses import dataclass

from ..geo import haversine_m

# Wie nah Start und Ziel der Anfrage am Pfad liegen müssen. Als Anteil der
# Luftlinie, mit Ober- und Untergrenze: Auf 600 km sind 24 km "dasselbe
# Ziel", auf 30 km wären es zwei Stadtteile zu viel.
#
# Die Obergrenze ist an einer echten Fahrt gemessen: Die Aufzeichnung der
# Fahrt Gueugnon - Reutlingen beginnt 15,7 km nach dem geplanten Start, weil
# erst nach der Abfahrt losgedrückt wurde. Das ist der Normalfall, nicht die
# Ausnahme - und mit 15 km Obergrenze wäre genau diese Fahrt durchgefallen.
# Das Stück bis zum Pfad legt das Routing selbst zurück.
RADIUS_ANTEIL = 0.04
RADIUS_MIN_KM = 3.0
RADIUS_MAX_KM = 30.0

# Der Abschnitt des Pfads muss die Anfrage überhaupt abdecken. Ein Pfad, der
# nur ein Zehntel der Strecke deckt, ist kein Kandidat, sondern ein Zufall.
MINDEST_ABDECKUNG = 0.7

# Abstand der Zwischenpunkte entlang des Pfads. Dichter heisst, dass das
# Routing der gefahrenen Strasse genauer folgt, aber jede Zwischenstation ist
# eine Stelle, an der es hängenbleiben kann; weiter auseinander lässt ihm
# Raum, den Weg selbst zu wählen. 25 km sind ein Stück Autobahn.
ABSTAND_KM = 25.0

# Ein Routing-Aufruf nimmt nur eine begrenzte Zahl von Zwischenpunkten an.
MAX_ZWISCHENPUNKTE = 20

# Punkte unter diesem Tempo sind Pausen, Ladeplätze und Parkplätze. Als
# Zwischenpunkt zwängen sie die Route zu einem Abstecher von der Strasse
# weg - genau der Fehler, den eine echte Fahrt mit Ladestopp sonst einbaute.
MINDEST_TEMPO_KMH = 25.0


@dataclass
class Abschnitt:
    """Das Stück eines früheren Pfads, das zur Anfrage passt."""
    punkte: list                # [(lat, lon, tempo_kmh | None), ...] in Fahrtrichtung der Anfrage
    gegenrichtung: bool         # Der Pfad wurde andersherum gefahren
    abstand_start_km: float     # wie weit Start der Anfrage vom Pfad entfernt liegt
    abstand_ziel_km: float
    laenge_km: float            # entlang des Pfads


def radius_km(luftlinie_km: float) -> float:
    return min(RADIUS_MAX_KM, max(RADIUS_MIN_KM, luftlinie_km * RADIUS_ANTEIL))


def _naechster(pfad: list, lat: float, lon: float) -> tuple[int, float]:
    """Index des nächsten Punkts und sein Abstand in m."""
    bester, abstand = 0, float("inf")
    for i, p in enumerate(pfad):
        d = haversine_m(lat, lon, p[0], p[1])
        if d < abstand:
            bester, abstand = i, d
    return bester, abstand


def _laenge_m(punkte: list) -> float:
    return sum(haversine_m(a[0], a[1], b[0], b[1])
               for a, b in zip(punkte, punkte[1:]))


def passender_abschnitt(pfad: list, start: tuple[float, float],
                        ziel: tuple[float, float]) -> Abschnitt | None:
    """Das Stück des Pfads zwischen Start und Ziel - oder None.

    `pfad`: [(lat, lon, tempo_kmh), ...] in Fahrtreihenfolge.

    Gefunden wird der Punkt des Pfads, der Start am nächsten liegt, und der,
    der dem Ziel am nächsten liegt. Stehen sie in der Reihenfolge des Pfads,
    ist es ein Teilstück vorwärts; stehen sie umgekehrt, wurde die Strecke
    andersherum gefahren und der Abschnitt wird gedreht. Beides gilt: Wer
    Gueugnon - Reutlingen gefahren ist, kennt auch Reutlingen - Gueugnon.
    """
    if len(pfad) < 2:
        return None
    luftlinie_km = haversine_m(start[0], start[1], ziel[0], ziel[1]) / 1000.0
    if luftlinie_km <= 0:
        return None
    grenze_m = radius_km(luftlinie_km) * 1000.0

    i, d_start = _naechster(pfad, start[0], start[1])
    j, d_ziel = _naechster(pfad, ziel[0], ziel[1])
    if d_start > grenze_m or d_ziel > grenze_m or i == j:
        return None

    gegen = j < i
    abschnitt = pfad[j:i + 1][::-1] if gegen else pfad[i:j + 1]
    laenge_km = _laenge_m(abschnitt) / 1000.0
    # Der Pfad ist mindestens so lang wie die Luftlinie; deutlich weniger
    # heisst, dass Messpunkte fehlen - eine Lücke, über die man nichts sagen
    # kann.
    if laenge_km < luftlinie_km * MINDEST_ABDECKUNG:
        return None
    return Abschnitt(punkte=abschnitt, gegenrichtung=gegen,
                     abstand_start_km=d_start / 1000.0,
                     abstand_ziel_km=d_ziel / 1000.0, laenge_km=laenge_km)


def zwischenpunkte(abschnitt: Abschnitt, abstand_km: float = ABSTAND_KM,
                   maximal: int = MAX_ZWISCHENPUNKTE) -> list[tuple[float, float]]:
    """Zwischenpunkte entlang des Abschnitts, ohne Start und Ziel selbst.

    Gewählt wird alle `abstand_km` entlang des Pfads der nächste Punkt, der
    **gefahren** wurde, also über `MINDEST_TEMPO_KMH` lag. Ein Punkt ohne
    Tempoangabe gilt als gefahren: Manche Quellen liefern keines, und ein
    Pfad ohne jede Angabe soll nicht an dieser Stelle scheitern.

    Reicht die Zahl nicht für den gewünschten Abstand, wird der Abstand
    vergrössert statt Punkte wegzulassen - eine gleichmässige Verteilung ist
    besser als eine, die hinten abbricht.
    """
    punkte = abschnitt.punkte
    if len(punkte) < 3:
        return []
    gesamt_m = abschnitt.laenge_km * 1000.0
    schritt_m = max(abstand_km * 1000.0, gesamt_m / (maximal + 1))

    # Strecke entlang des Pfads bis zu jedem Punkt.
    kumuliert = [0.0]
    for a, b in zip(punkte, punkte[1:]):
        kumuliert.append(kumuliert[-1] + haversine_m(a[0], a[1], b[0], b[1]))

    def gefahren(p) -> bool:
        return p[2] is None or p[2] >= MINDEST_TEMPO_KMH

    aus: list[tuple[float, float]] = []
    ziel_m = schritt_m
    # Nicht bis ganz ans Ende: Ein Punkt kurz vor dem Ziel bringt nichts und
    # zwingt die Route, dort anzuklopfen, wo sie ohnehin hinfährt.
    while ziel_m < gesamt_m - schritt_m * 0.5:
        kandidat = min(
            (i for i in range(1, len(punkte) - 1) if gefahren(punkte[i])),
            key=lambda i: abs(kumuliert[i] - ziel_m), default=None)
        if kandidat is not None:
            p = (punkte[kandidat][0], punkte[kandidat][1])
            if not aus or haversine_m(aus[-1][0], aus[-1][1], p[0], p[1]) > 1000.0:
                aus.append(p)
        ziel_m += schritt_m
    return aus[:maximal]


def pfad_aus_messpunkten(zeilen: list) -> list[tuple[float, float, float | None]]:
    """[(lat, lon, tempo_kmh), ...] aus Zeilen einer Abfrage; Zeilen ohne
    Koordinate fallen heraus."""
    return [(lat, lon, tempo) for lat, lon, tempo in zeilen
            if lat is not None and lon is not None]
