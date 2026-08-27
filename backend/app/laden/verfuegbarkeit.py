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
MELDUNG_GUELTIG_S = 30 * 60


@dataclass
class Zustand:
    frei: int | None          # None = unbekannt
    gesamt: int
    quelle: str               # "unbekannt" | "meldung" | "ocpi"
    stand_s: float = 0.0


class VerfuegbarkeitsQuelle(Protocol):
    def zustand(self, ladepunkt) -> Zustand:
        ...


class Unbekannt:
    """Die Vorgabe: keine Daten, nur die Anzahl der Ladepunkte."""

    def zustand(self, ladepunkt) -> Zustand:
        return Zustand(frei=None, gesamt=ladepunkt.anzahl_punkte or 1,
                       quelle="unbekannt")


class Meldungen:
    """Meldungen der Nutzer, im Speicher gehalten.

    Bewusst nicht in der Datenbank: Die Aussage ist nach dreissig Minuten
    wertlos, und etwas, das schneller verfällt als ein Neustart dauert,
    gehört nicht dauerhaft gespeichert.
    """

    def __init__(self, weiter: VerfuegbarkeitsQuelle | None = None):
        self._belegt: dict[int, float] = {}
        self._weiter = weiter or Unbekannt()

    def melden(self, ladepunkt_id: int) -> None:
        self._belegt[ladepunkt_id] = time.time()

    def freigeben(self, ladepunkt_id: int) -> None:
        self._belegt.pop(ladepunkt_id, None)

    def ist_gemeldet(self, ladepunkt_id: int) -> bool:
        seit = self._belegt.get(ladepunkt_id)
        if seit is None:
            return False
        if time.time() - seit > MELDUNG_GUELTIG_S:
            del self._belegt[ladepunkt_id]
            return False
        return True

    def zustand(self, ladepunkt) -> Zustand:
        if self.ist_gemeldet(ladepunkt.id):
            return Zustand(frei=0, gesamt=ladepunkt.anzahl_punkte or 1,
                           quelle="meldung",
                           stand_s=time.time() - self._belegt[ladepunkt.id])
        return self._weiter.zustand(ladepunkt)


# Ab wie vielen Ladepunkten ein Standort als "gross" gilt und die volle
# Gutschrift bekommt. Darüber wächst nichts mehr - der Sprung von 30 auf 40
# Säulen ändert nichts mehr an der Frage, ob etwas frei ist.
#
# Stand vorher bei rund 15, und das war zu früh: In den Daten einer
# Frankreich-Route bekamen Standorte mit 15, 17, 20, 28 und 30 Ladepunkten
# alle exakt dieselbe Gutschrift. Die Grösse hörte damit genau dort auf zu
# zählen, wo die interessanten Ladeparks anfangen.
GROSSER_PARK = 30
LADEPARK_BONUS_MIN = 4.0


def redundanz_bonus(anzahl_punkte: int, hoechstens: float = LADEPARK_BONUS_MIN
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
    if hoechstens <= 0:
        return 0.0
    anteil = math.log(max(1, anzahl_punkte)) / math.log(GROSSER_PARK)
    return round(min(hoechstens, hoechstens * anteil), 2)


# Zeitgutschrift für einen bevorzugten Anbieter - in derselben Grössenordnung
# wie der Redundanz-Bonus, damit keiner der beiden Effekte den anderen
# systematisch überstimmt.
BETREIBER_BONUS_MIN = 4.0


def betreiber_bonus(betreiber: str, bevorzugte: list[str] | None) -> float:
    """Zeitgutschrift in Minuten, wenn der Betreiber auf der bevorzugten Liste steht.

    Kein harter Filter, sondern wie der Redundanz-Bonus nur ein Gewicht in der
    Stoppwahl: Ein bevorzugter Anbieter macht einen Halt attraktiver, nie
    kostenlos - der Bonus wiegt beim Aufruf ausschliesslich den Umweg auf, nie
    die Ladezeit (siehe optimierer.py).

    Der Vergleich ist eine Teilzeichenkette, klein geschrieben: "EnBW" in der
    Liste trifft "EnBW mobility+" im Datensatz, ohne dass der genaue
    Anbieter-Wortlaut bekannt sein muss.
    """
    if not bevorzugte or not betreiber:
        return 0.0
    betreiber_klein = betreiber.strip().lower()
    for eintrag in bevorzugte:
        if eintrag and eintrag.strip().lower() in betreiber_klein:
            return BETREIBER_BONUS_MIN
    return 0.0


# Eine Instanz für den Prozess. Der Zustand ist bewusst prozesslokal - jolt
# läuft als ein Container für einen Haushalt.
MELDUNGEN = Meldungen()
