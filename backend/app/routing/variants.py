"""Echte Routenalternativen erzeugen, wo das Routing keine liefert.

**Das Problem.** Für ein Elektroauto ist die schnellste Strasse nicht
automatisch die beste: Jeder Umweg kostet Energie, und Energie kostet
Ladezeit. Der Vergleich lohnt sich also - nur gibt es nichts zu vergleichen.
`/api/route` fragte openrouteservice nach `fastest`, und dabei blieb es.

**Warum die naheliegenden Wege nicht gehen** - beides an der Strecke
Mâcon → Reutlingen (598 km) gemessen, nicht vermutet:

- `alternative_routes` von ORS wäre genau das Richtige und liefert bis zu
  drei Wege in *einer* Anfrage. Der öffentliche Dienst lehnt sie aber ab,
  sobald die Route länger als **100 km** ist (Fehler 2004). Damit fällt sie
  für jede Fahrt aus, bei der eine Alternative überhaupt etwas ändern würde.
- `shortest` liefert 477 km in 10,7 Stunden gegen 598 km in 5,6. Das ist
  keine Alternative, das ist eine andere Fortbewegungsart.
- `recommended` liefert auf Autobahnstrecken dieselbe Strasse wie `fastest` -
  gemessen bis auf den Meter dieselbe Zahl.

**Was geht.** Zwischenpunkte. Eine Route, die durch einen Punkt neben der
schnellsten Strasse gezwungen wird, ist eine echte dritte Möglichkeit, und
Zwischenpunkte unterliegen keiner Längenbeschränkung. Auf derselben Strecke:

    fastest                 598,3 km   5,56 h
    Zwischenpunkt 75 %, +6 %  548,0 km   5,98 h   <- 50 km kürzer
    (tatsächlich gefahren)  ~545 km    ~6,0 h

Der Fahrer hatte diesen Weg selbst gefunden; der Planer kannte ihn nicht.

**Wie die Punkte gewählt werden.** Auf der Luftlinie zwischen Start und Ziel
wird ein Bruchteil abgegriffen und senkrecht dazu versetzt. Die Versatzweite
hängt an der Streckenlänge und nicht an einer festen Kilometerzahl: 30 km
neben einer 600-km-Strecke sind eine Nuance, neben einer 150-km-Strecke ein
anderes Bundesland.

**Und was davon taugt: bisher nichts.** Auf der Messstrecke wurden zwölf
geometrische Kandidaten durchgerechnet - Abgriff bei 50/75/80/85/90 %, Versatz
4/6/8 % - und **jeder einzelne** verlor gegen die schnellste Route, sobald
Ladezeit und Stromkosten gegen die Mehrfahrzeit gerechnet wurden. Der beste
lag noch 2 € daneben, die meisten deutlich weiter. Der Grund ist
naheliegend: Ein Punkt, der geometrisch neben der Autobahn liegt, liegt
verkehrlich irgendwo, und ORS baut daraus eine Route über Landstrassen.

Die Route, die tatsächlich gewann - 539,6 km in 5,77 h, also 59 km kürzer
für 13 Minuten mehr und unterm Strich 2 bis 5 € billiger, je nach
Zeitwert -, entstand aus **zwei Punkten der wirklich gefahrenen Strecke**.
Nicht aus Geometrie, sondern aus Erfahrung.

Deshalb steht `umwege_pruefen` in `Routenanfrage` auf `False`. Was hier
liegt, ist die funktionierende Mechanik - Kandidaten erzeugen, aussichtslose
vorab verwerfen, den Rest am Ladeplan messen -, und ein Kandidatenlieferant,
der sie noch nicht füllt. Der naheliegende bessere sind aufgezeichnete
Fahrten: `live/recording.py` schreibt sie ohnehin mit, und wer eine
Strecke schon einmal gefahren ist, kennt einen Weg, den kein Kantengewicht
kennt.
"""
import logging

from ..geo import haversine_m, bearing_degree, offset_point

log = logging.getLogger("uvicorn.error")

# Unterhalb dieser Luftlinie lohnt die Suche nicht: Auf kurzen Strecken gibt
# es selten mehr als einen sinnvollen Weg, der Umweg fällt gegen die
# Ladezeit nicht ins Gewicht - und dort funktioniert ORS' eigenes
# `alternative_routes` ohnehin.
MIN_STRAIGHT_LINE_KM = 120.0

# Wo auf der Luftlinie abgegriffen wird. Zwei Stellen, damit sowohl eine
# andere Anfahrt als auch eine andere Zielannäherung entstehen kann.
SHARES = (0.5, 0.75)

# Versatz als Anteil der Luftlinie. 6 % waren auf der Messstrecke die
# brauchbare Weite: Bei 12 % lagen alle Kandidaten deutlich schlechter, bei
# 3 % fielen sie mit der Ausgangsroute zusammen.
OFFSET_SHARE = 0.06


def alternative_points(start: tuple[float, float], destination: tuple[float, float],
                   shares: tuple = SHARES,
                   offset_share: float = OFFSET_SHARE) -> list[dict]:
    """Kandidaten für Zwischenpunkte, je als {"punkt", "etikett"}.

    Leere Liste heisst "für diese Strecke lohnt es nicht" - der Aufrufer
    rechnet dann wie bisher nur die schnellste Route.
    """
    straight_line = haversine_m(start[0], start[1], destination[0], destination[1])
    if straight_line / 1000.0 < MIN_STRAIGHT_LINE_KM:
        return []

    bearing = bearing_degree(start[0], start[1], destination[0], destination[1])
    offset_m = straight_line * offset_share

    origin_of = []
    for share in shares:
        # Punkt auf der Luftlinie: in Richtung Ziel um den Bruchteil weiter.
        middle = offset_point(start[0], start[1], bearing, straight_line * share)
        for sign in (+1, -1):
            across = (bearing + sign * 90.0) % 360.0
            point = offset_point(middle[0], middle[1], across, offset_m)
            # Die Himmelsrichtung aus dem Ergebnis ablesen und nicht aus dem
            # Vorzeichen: `peilung + 90°` dreht nach rechts, und rechts von
            # einer ostwärts führenden Strecke liegt Süden. Wer das Etikett
            # ans Vorzeichen hängt, beschriftet die halbe Landkarte falsch.
            direction = "nördlich" if point[0] > middle[0] else "südlich"
            origin_of.append({
                "point": point,
                # Das Etikett beschreibt die *Herkunft*, nicht die Qualität -
                # die steht erst nach der Bewertung fest.
                "label": f"{direction} bei {round(share * 100)} %",
            })
    return origin_of


def actual_dominated(candidate_m: float, candidate_s: float,
                  basis_m: float, basis_s: float,
                  tolerance: float = 0.005) -> bool:
    """Ist der Kandidat sowohl länger als auch langsamer als die Basis?

    Dann kann er nicht gewinnen - egal wie der Ladeplan ausfällt, denn mehr
    Strecke heisst mehr Energie und mehr Zeit heisst mehr Zeit. Solche
    Kandidaten fliegen raus, *bevor* Wetter, Verbrauchsprofil und Ladeplan
    für sie gerechnet werden; das ist der teure Teil.

    Die Toleranz verhindert, dass ein Kandidat, der bis auf ein Promille
    dieselbe Strasse ist, als eigener Vorschlag stehenbleibt.
    """
    return (candidate_m >= basis_m * (1 - tolerance)
            and candidate_s >= basis_s * (1 - tolerance))
