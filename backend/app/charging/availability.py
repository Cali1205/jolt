"""Ist die Säule frei? - und der ehrliche Umgang damit, dass wir es nicht wissen.

Echte Belegungsdaten öffentlicher Ladesäulen sind in Deutschland nicht frei
verfügbar. Wer sie hat, hat sie über OCPI-Verträge mit Betreibern oder über
kommerzielle Aggregatoren. Für ein Privatprojekt ist das vorerst zu.

Statt Verfügbarkeit vorzutäuschen, macht jolt drei Dinge:

1. Dieses Interface steht, damit eine OCPI-Anbindung später ein Adapter ist
   und kein Umbau.
2. Solange keine Daten da sind, zählt **Redundanz**: Ein Standort mit acht
   Ladepunkten ist einem mit zwei vorzuziehen, auch wenn er zwei Minuten
   Umweg kostet. Das ist die beste verfügbare Näherung an "da ist
   wahrscheinlich was frei".
3. Der Nutzer kann melden, dass ein Standort belegt ist. Das gilt für die
   laufende Fahrt - und ist die einzige Information, die wirklich stimmt.
"""
import time
from dataclasses import dataclass
from typing import Protocol

# Wie lange eine Meldung "belegt" gilt. Eine halbe Stunde ist die Zeit, die
# ein Schnellladevorgang typischerweise dauert - danach ist die Aussage
# wertlos und würde nur einen brauchbaren Standort dauerhaft ausschliessen.
REPORT_VALID_S = 30 * 60


@dataclass
class State:
    free: int | None          # None = unbekannt
    total: int
    source: str               # "unbekannt" | "meldung" | "ocpi"
    as_of_s: float = 0.0


class AvailabilitySource(Protocol):
    def state(self, charge_point) -> State:
        ...


class Unknown:
    """Die Vorgabe: keine Daten, nur die Anzahl der Ladepunkte."""

    def state(self, charge_point) -> State:
        return State(free=None, total=charge_point.point_count or 1,
                       source="unbekannt")


class Reports:
    """Meldungen der Nutzer, im Speicher gehalten.

    Bewusst nicht in der Datenbank: Die Aussage ist nach dreissig Minuten
    wertlos, und etwas, das schneller verfällt als ein Neustart dauert,
    gehört nicht dauerhaft gespeichert.
    """

    def __init__(self, onward: AvailabilitySource | None = None):
        self._occupied: dict[int, float] = {}
        self._next = onward or Unknown()

    def report(self, charge_point_id: int) -> None:
        self._occupied[charge_point_id] = time.time()

    def release(self, charge_point_id: int) -> None:
        self._occupied.pop(charge_point_id, None)

    def actual_reported(self, charge_point_id: int) -> bool:
        since = self._occupied.get(charge_point_id)
        if since is None:
            return False
        if time.time() - since > REPORT_VALID_S:
            del self._occupied[charge_point_id]
            return False
        return True

    def state(self, charge_point) -> State:
        if self.actual_reported(charge_point.id):
            return State(free=0, total=charge_point.point_count or 1,
                           source="meldung",
                           as_of_s=time.time() - self._occupied[charge_point.id])
        return self._next.state(charge_point)


# Ab wie vielen Ladepunkten ein Standort als "gross" gilt und die volle
# Gutschrift bekommt. Darüber wächst nichts mehr - der Sprung von 30 auf 40
# Säulen ändert nichts mehr an der Frage, ob etwas frei ist.
#
# Stand vorher bei rund 15, und das war zu früh: In den Daten einer
# Frankreich-Route bekamen Standorte mit 15, 17, 20, 28 und 30 Ladepunkten
# alle exakt dieselbe Gutschrift. Die Grösse hörte damit genau dort auf zu
# zählen, wo die interessanten Ladeparks anfangen.
LARGE_PARK = 30
CHARGE_PARK_BONUS_MIN = 4.0


def redundancy_bonus(point_count: int, at_most: float = CHARGE_PARK_BONUS_MIN
                    ) -> float:
    """Zeitgutschrift in Minuten für einen Standort mit vielen Ladepunkten.

    Solange niemand weiss, was frei ist, ist die Anzahl der Ladepunkte die
    einzige belastbare Aussage über das Risiko, vor einer belegten Säule zu
    stehen. Ein Standort mit vielen Punkten ist einen kleinen Umweg wert -
    und, seit die Gutschrift nicht mehr am Umweg hängt, auch einen Vorzug
    gegenüber einem kleineren direkt daneben.

    Der Logarithmus, weil der Sprung von 2 auf 4 Ladepunkten viel mehr
    bedeutet als der von 20 auf 22. Die volle Gutschrift gibt es ab
    `GROSSER_PARK` Punkten.

    `hoechstens` ist einstellbar, weil es eine Vorliebe ist und keine
    Naturkonstante: Wem ein grosser Ladepark wenig bedeutet, stellt es auf
    null, und dann entscheidet allein die Zeit.
    """
    import math
    if at_most <= 0:
        return 0.0
    share = math.log(max(1, point_count)) / math.log(LARGE_PARK)
    return round(min(at_most, at_most * share), 2)


# Zeitgutschrift für einen bevorzugten Anbieter - in derselben Grössenordnung
# wie der Redundanz-Bonus, damit keiner der beiden Effekte den anderen
# systematisch überstimmt.
OPERATOR_BONUS_MIN = 4.0


def operator_bonus(operator: str, preferred: list[str] | None) -> float:
    """Zeitgutschrift in Minuten, wenn der Betreiber auf der bevorzugten Liste steht.

    Kein harter Filter, sondern wie der Redundanz-Bonus nur ein Gewicht in der
    Stoppwahl: Ein bevorzugter Anbieter macht einen Halt attraktiver, nie
    kostenlos - der Bonus wiegt beim Aufruf ausschliesslich den Umweg auf, nie
    die Ladezeit (siehe optimizer.py).

    Der Vergleich ist eine Teilzeichenkette, klein geschrieben: "EnBW" in der
    Liste trifft "EnBW mobility+" im Datensatz, ohne dass der genaue
    Anbieter-Wortlaut bekannt sein muss.
    """
    if not preferred or not operator:
        return 0.0
    operator_small = operator.strip().lower()
    for entry in preferred:
        if entry and entry.strip().lower() in operator_small:
            return OPERATOR_BONUS_MIN
    return 0.0


# Eine Instanz für den Prozess. Der Zustand ist bewusst prozesslokal - jolt
# läuft als ein Container für einen Haushalt.
REPORTS = Reports()
