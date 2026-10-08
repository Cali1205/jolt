"""Gefahrene Strecken als Routenkandidaten.

**Warum.** Für ein Elektroauto ist die schnellste Strasse nicht automatisch die
beste, und die Alternativen, die sich rechnen lassen, taugen nichts: Auf der
Messstrecke Mâcon - Reutlingen verlor jeder der zwölf geometrisch erzeugten
Umwege gegen die schnellste Route (siehe `variants.py`). Die Route, die
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
RADIUS_SHARE = 0.04
RADIUS_MIN_KM = 3.0
RADIUS_MAX_KM = 30.0

# Der Abschnitt des Pfads muss die Anfrage überhaupt abdecken. Ein Pfad, der
# nur ein Zehntel der Strecke deckt, ist kein Kandidat, sondern ein Zufall.
MIN_COVERAGE = 0.7

# Abstand der Zwischenpunkte entlang des Pfads. Dichter heisst, dass das
# Routing der gefahrenen Strasse genauer folgt, aber jede Zwischenstation ist
# eine Stelle, an der es hängenbleiben kann; weiter auseinander lässt ihm
# Raum, den Weg selbst zu wählen. 25 km sind ein Stück Autobahn.
SPACING_KM = 25.0

# Ein Routing-Aufruf nimmt nur eine begrenzte Zahl von Zwischenpunkten an.
MAX_WAYPOINTS = 20

# Punkte unter diesem Tempo sind Pausen, Ladeplätze und Parkplätze. Als
# Zwischenpunkt zwängen sie die Route zu einem Abstecher von der Strasse
# weg - genau der Fehler, den eine echte Fahrt mit Ladestopp sonst einbaute.
MIN_SPEED_KMH = 25.0


@dataclass
class Section:
    """Das Stück eines früheren Pfads, das zur Anfrage passt."""
    points: list                # [(lat, lon, tempo_kmh | None), ...] in Fahrtrichtung der Anfrage
    opposite: bool         # Der Pfad wurde andersherum gefahren
    spacing_start_km: float     # wie weit Start der Anfrage vom Pfad entfernt liegt
    spacing_target_km: float
    length_km: float            # entlang des Pfads


def radius_km(straight_line_km: float) -> float:
    return min(RADIUS_MAX_KM, max(RADIUS_MIN_KM, straight_line_km * RADIUS_SHARE))


def _next(fs_path: list, lat: float, lon: float) -> tuple[int, float]:
    """Index des nächsten Punkts und sein Abstand in m."""
    best, spacing = 0, float("inf")
    for i, p in enumerate(fs_path):
        d = haversine_m(lat, lon, p[0], p[1])
        if d < spacing:
            best, spacing = i, d
    return best, spacing


def _length_m(points: list) -> float:
    return sum(haversine_m(a[0], a[1], b[0], b[1])
               for a, b in zip(points, points[1:]))


def fitting_section(fs_path: list, start: tuple[float, float],
                        destination: tuple[float, float]) -> Section | None:
    """Das Stück des Pfads zwischen Start und Ziel - oder None.

    `pfad`: [(lat, lon, tempo_kmh), ...] in Fahrtreihenfolge.

    Gefunden wird der Punkt des Pfads, der Start am nächsten liegt, und der,
    der dem Ziel am nächsten liegt. Stehen sie in der Reihenfolge des Pfads,
    ist es ein Teilstück vorwärts; stehen sie umgekehrt, wurde die Strecke
    andersherum gefahren und der Abschnitt wird gedreht. Beides gilt: Wer
    Gueugnon - Reutlingen gefahren ist, kennt auch Reutlingen - Gueugnon.
    """
    if len(fs_path) < 2:
        return None
    straight_line_km = haversine_m(start[0], start[1], destination[0], destination[1]) / 1000.0
    if straight_line_km <= 0:
        return None
    limit_m = radius_km(straight_line_km) * 1000.0

    i, d_start = _next(fs_path, start[0], start[1])
    j, d_target = _next(fs_path, destination[0], destination[1])
    if d_start > limit_m or d_target > limit_m or i == j:
        return None

    against = j < i
    section = fs_path[j:i + 1][::-1] if against else fs_path[i:j + 1]
    length_km = _length_m(section) / 1000.0
    # Der Pfad ist mindestens so lang wie die Luftlinie; deutlich weniger
    # heisst, dass Messpunkte fehlen - eine Lücke, über die man nichts sagen
    # kann.
    if length_km < straight_line_km * MIN_COVERAGE:
        return None
    return Section(points=section, opposite=against,
                     spacing_start_km=d_start / 1000.0,
                     spacing_target_km=d_target / 1000.0, length_km=length_km)


def waypoints(section: Section, spacing_km: float = SPACING_KM,
                   maximal: int = MAX_WAYPOINTS) -> list[tuple[float, float]]:
    """Zwischenpunkte entlang des Abschnitts, ohne Start und Ziel selbst.

    Gewählt wird alle `abstand_km` entlang des Pfads der nächste Punkt, der
    **gefahren** wurde, also über `MINDEST_TEMPO_KMH` lag. Ein Punkt ohne
    Tempoangabe gilt als gefahren: Manche Quellen liefern keines, und ein
    Pfad ohne jede Angabe soll nicht an dieser Stelle scheitern.

    Reicht die Zahl nicht für den gewünschten Abstand, wird der Abstand
    vergrössert statt Punkte wegzulassen - eine gleichmässige Verteilung ist
    besser als eine, die hinten abbricht.
    """
    points = section.points
    if len(points) < 3:
        return []
    total_m = section.length_km * 1000.0
    step_m = max(spacing_km * 1000.0, total_m / (maximal + 1))

    # Strecke entlang des Pfads bis zu jedem Punkt.
    cumulative = [0.0]
    for a, b in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + haversine_m(a[0], a[1], b[0], b[1]))

    def driven(p) -> bool:
        return p[2] is None or p[2] >= MIN_SPEED_KMH

    origin_of: list[tuple[float, float]] = []
    target_m = step_m
    # Nicht bis ganz ans Ende: Ein Punkt kurz vor dem Ziel bringt nichts und
    # zwingt die Route, dort anzuklopfen, wo sie ohnehin hinfährt.
    while target_m < total_m - step_m * 0.5:
        candidate = min(
            (i for i in range(1, len(points) - 1) if driven(points[i])),
            key=lambda i: abs(cumulative[i] - target_m), default=None)
        if candidate is not None:
            p = (points[candidate][0], points[candidate][1])
            if not origin_of or haversine_m(origin_of[-1][0], origin_of[-1][1], p[0], p[1]) > 1000.0:
                origin_of.append(p)
        target_m += step_m
    return origin_of[:maximal]


def path_from_samples(rows: list) -> list[tuple[float, float, float | None]]:
    """[(lat, lon, tempo_kmh), ...] aus Zeilen einer Abfrage; Zeilen ohne
    Koordinate fallen heraus."""
    return [(lat, lon, velocity) for lat, lon, velocity in rows
            if lat is not None and lon is not None]
