"""Der Ladestopp-Optimierer - Abschnitt 4 des Konzepts in Code.

Die Aufgabe: Finde die Folge von Ladestopps und Lademengen, die die
**Gesamtreisezeit** minimiert, unter der Nebenbedingung, dass der SoC nie unter
die Reserve fällt und am Ziel der gewünschte Ziel-SoC erreicht ist.

Das ist kein kürzester Weg, sondern ein kürzester Weg mit einer kontinuierlichen
Entscheidungsvariablen je Knoten: wie viel wird geladen. Deshalb die fünf
Schritte aus dem Konzept:

1. **Kandidaten** - Ladepunkte im Korridor, nach Umwegzeit gefiltert und je
   Streckenabschnitt ausgedünnt.
2. **Graph** - Knoten sind Start, Kandidaten, Ziel; eine Kante existiert, wenn
   die Etappe mit voller Batterie fahrbar ist.
3. **Pareto-Dijkstra** über den Zustand `(Knoten, Ankunfts-SoC)`. Je Knoten wird
   eine Pareto-Front von Labels `(Kosten, SoC)` geführt: Ein Label fällt weg,
   wenn ein anderes gleichzeitig billiger *und* mit mehr Ladung dort ist.
4. **Nachoptimierung** - bei feststehender Stoppfolge wandern die Ladehübe auf
   einem feinen Raster in den steilen Teil der Ladekurve.
5. **Ausweichstandorte** - zu jedem Stopp der beste Alternativstopp, der ohne
   Nachladen noch erreichbar ist.

Warum nicht gierig? Ein gieriger Planer scheitert systematisch an zwei Stellen:
vor langen Lücken ohne Schnelllader, wo man *vorher* mehr hätte laden müssen,
und bei der Wahl zwischen einem 50-kW- und einem 300-kW-Standort zwanzig
Kilometer später. Genau das sind die Fälle, in denen sich ein Planer lohnt.

Dieses Modul kennt weder Datenbank noch Netz: Es bekommt ein fertig gerechnetes
Streckenprofil und eine Liste von Ladeoptionen. Damit lässt es sich in
tools/check_optimierer.py vollständig durchrechnen - so wie das Verbrauchsmodell
auch.
"""
import heapq
import math
from bisect import bisect_left
from dataclasses import dataclass, field

from .kurven import leistung_bei
from .verfuegbarkeit import redundanz_bonus

# Kandidaten mit mehr Umweg fallen raus: Sie gewinnen die Zeit an der Säule
# fast nie zurück. Zehn Minuten Umweg sind zehn Minuten Ladezeit, und die
# bekommt man an einem 150-kW-Lader für rund 20 kWh.
UMWEG_GRENZE_MIN = 10.0

# Raster der Ladeziele in der Suche. Fünf Prozentpunkte halten den Graphen
# klein; die Quantisierung holt Schritt 4 anschliessend wieder herein.
SOC_RASTER = 5.0
# Raster der Nachoptimierung - fünfmal feiner, weil dort die Stoppfolge schon
# feststeht und nur noch die Lademengen gesucht werden.
SOC_RASTER_FEIN = 1.0

# Wie viel an einem Halt mindestens geladen werden muss, damit er sich lohnt.
#
# Acht Prozentpunkte klingen willkürlich, sind aber gerechnet: Ein Stopp
# kostet allein an Fixkosten rund vier Minuten (abfahren, einparken, Kabel
# holen, wieder auffädeln). In derselben Zeit lädt ein Schnelllader rund zehn
# Prozentpunkte. Wer weniger lädt, hat den Halt nicht wieder hereingeholt -
# er hätte dieselbe Energie am nächsten Stopp in weniger Zeit bekommen.
#
# Mit der vorherigen Schranke von zwei Prozentpunkten entstanden Stopps von
# einer Minute Ladezeit, die nur deshalb billig aussahen, weil der
# Redundanzbonus ihren Umweg aufwog.
MIN_LADEHUB = 8.0

# Ausdünnung der Kandidaten: je Streckenabschnitt die besten N. Ohne das
# stünden an einem Autobahnkreuz zwanzig gleichwertige Standorte im Graphen
# und kosteten Rechenzeit ohne Erkenntnis.
KANDIDATEN_JE_ABSCHNITT = 3
ABSCHNITT_KM = 15.0
HOECHSTENS_KANDIDATEN = 60

# Die Ankunfts-SoC der Labels wird auf dieses Raster **abgerundet**. Das hält
# die Pareto-Front endlich, ohne je mehr Ladung zu behaupten, als da ist -
# Abrunden ist die pessimistische Richtung.
LABEL_RASTER = 0.5

_EPS = 1e-9


# ---------------------------------------------------------------------------
# Eingaben
# ---------------------------------------------------------------------------

@dataclass
class Ladeoption:
    """Ein möglicher Halt - alles, was der Optimierer über ihn wissen muss.

    Bewusst kein ORM-Objekt: Der Optimierer soll ohne Datenbank prüfbar sein,
    und ein hypothetischer Standort ("was wäre, wenn hier ein 300-kW-Lader
    stünde?") ist so eine Zeile Code statt eines DB-Eintrags.
    """
    id: int
    km_auf_route: float
    umweg_minuten: float
    max_kw: float
    anzahl_punkte: int = 1
    name: str = ""
    betreiber: str = ""
    ort: str = ""
    lat: float = 0.0
    lon: float = 0.0
    # Als belegt gemeldet: fällt aus der Planung, bleibt aber als Datensatz da.
    gesperrt: bool = False

    def als_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "betreiber": self.betreiber,
                "ort": self.ort, "lat": self.lat, "lon": self.lon,
                "max_kw": self.max_kw, "anzahl_punkte": self.anzahl_punkte,
                "km_auf_route": round(self.km_auf_route, 1),
                "umweg_minuten": round(self.umweg_minuten, 1)}


@dataclass
class Streckenprofil:
    """Kumulierte Energie und Zeit über den Kilometerstand.

    Der entscheidende Punkt: Der Energiebedarf einer Etappe hängt **nicht** vom
    Ladestand ab - ein E-Auto wird beim Laden nicht schwerer. Deshalb genügt
    ein einziger Durchlauf des Verbrauchsmodells, und der Optimierer liest den
    Bedarf jeder Etappe als Differenz zweier kumulierter Werte ab, statt das
    Profil für jede Variante neu zu rechnen.
    """
    km: list[float]
    kwh: list[float]
    minuten: list[float]

    @classmethod
    def aus_profil(cls, profil) -> "Streckenprofil":
        return cls(km=[p.km for p in profil.punkte],
                   kwh=[p.kwh_kumuliert for p in profil.punkte],
                   minuten=[p.minuten_kumuliert for p in profil.punkte])

    @classmethod
    def aus_dicts(cls, eintraege: list[dict]) -> "Streckenprofil":
        """Aus dem gespeicherten `Fahrt.energieprofil`."""
        return cls(km=[float(e.get("km", 0.0)) for e in eintraege],
                   kwh=[float(e.get("kwh", 0.0)) for e in eintraege],
                   minuten=[float(e.get("minuten", 0.0)) for e in eintraege])

    @property
    def gesamt_km(self) -> float:
        return self.km[-1] if self.km else 0.0

    @property
    def gesamt_minuten(self) -> float:
        return self.minuten[-1] if self.minuten else 0.0

    def wert(self, werte: list[float], km: float) -> float:
        """Linear interpoliert - die Stützpunkte liegen rund 250 m auseinander."""
        if not self.km:
            return 0.0
        if km <= self.km[0]:
            return werte[0]
        if km >= self.km[-1]:
            return werte[-1]
        i = bisect_left(self.km, km)
        k0, k1 = self.km[i - 1], self.km[i]
        w0, w1 = werte[i - 1], werte[i]
        if k1 - k0 <= _EPS:
            return w1
        return w0 + (w1 - w0) * (km - k0) / (k1 - k0)


# ---------------------------------------------------------------------------
# Ergebnis
# ---------------------------------------------------------------------------

@dataclass
class Stopp:
    option: Ladeoption
    ankunft_soc: float
    abfahrt_soc: float
    ladezeit_minuten: float
    umweg_minuten: float
    kwh_geladen: float
    # Minuten seit Abfahrt, inklusive aller vorherigen Stopps und Umwege.
    ankunft_minute: float
    abfahrt_minute: float
    # Der Standort, an den man ohne Nachladen noch käme, wenn hier alles
    # belegt ist. None = es gibt keinen.
    ausweich: dict | None = None

    def als_dict(self) -> dict:
        return {**self.option.als_dict(),
                "ankunft_soc": round(self.ankunft_soc, 1),
                "abfahrt_soc": round(self.abfahrt_soc, 1),
                "ladezeit_minuten": round(self.ladezeit_minuten, 1),
                "umweg_minuten": round(self.umweg_minuten, 1),
                "kwh_geladen": round(self.kwh_geladen, 1),
                "ankunft_minute": round(self.ankunft_minute),
                "abfahrt_minute": round(self.abfahrt_minute),
                "ausweich": self.ausweich}


@dataclass
class Ladeplan:
    machbar: bool = False
    grund: str = ""
    stopps: list[Stopp] = field(default_factory=list)
    fahrzeit_minuten: float = 0.0
    ladezeit_minuten: float = 0.0
    umwegzeit_minuten: float = 0.0
    gesamt_minuten: float = 0.0
    soc_am_ziel: float = 0.0
    gepruefte_kandidaten: int = 0

    def als_dict(self) -> dict:
        return {"machbar": self.machbar, "grund": self.grund,
                "anzahl_stopps": len(self.stopps),
                "stopps": [s.als_dict() for s in self.stopps],
                "fahrzeit_minuten": round(self.fahrzeit_minuten),
                "ladezeit_minuten": round(self.ladezeit_minuten),
                "umwegzeit_minuten": round(self.umwegzeit_minuten),
                "gesamt_minuten": round(self.gesamt_minuten),
                "soc_am_ziel": round(self.soc_am_ziel, 1),
                "gepruefte_kandidaten": self.gepruefte_kandidaten}


# ---------------------------------------------------------------------------
# Ladezeit als Tabelle
# ---------------------------------------------------------------------------

class _Ladezeittabelle:
    """Kumulierte Ladezeit von 0 % bis x %, für eine feste Säulenleistung.

    Der Trick, der die Suche bezahlbar macht: Die Ladezeit von a nach b ist
    `T(b) - T(a)`, weil das Integral über die Ladekurve additiv ist. Statt in
    jeder der zehntausenden Kantenauswertungen erneut zu integrieren, wird
    einmal je vorkommender Säulenleistung eine Tabelle gebaut.
    """

    def __init__(self, kurve, akku_netto_kwh: float, max_saeule_kw: float,
                 max_fahrzeug_kw: float, temperatur_faktor: float,
                 schritt: float = 0.5):
        self.schritt = schritt
        self.werte: list[float] = [0.0]
        summe = 0.0
        soc = 0.0
        while soc < 100.0 - _EPS:
            kw = leistung_bei(kurve, soc + schritt / 2.0, max_saeule_kw,
                              max_fahrzeug_kw, temperatur_faktor)
            if kw <= 0.1:
                # Ab hier nimmt das Auto nichts mehr an - alles darüber ist
                # unerreichbar, nicht "dauert lange".
                summe = math.inf
            elif summe != math.inf:
                summe += (akku_netto_kwh * schritt / 100.0) / kw * 60.0
            self.werte.append(summe)
            soc += schritt

    def _bis(self, soc: float) -> float:
        if soc <= 0.0:
            return 0.0
        if soc >= 100.0:
            return self.werte[-1]
        pos = soc / self.schritt
        i = int(pos)
        if i + 1 >= len(self.werte):
            return self.werte[-1]
        a, b = self.werte[i], self.werte[i + 1]
        if a == math.inf or b == math.inf:
            return math.inf
        return a + (b - a) * (pos - i)

    def zeit(self, von_soc: float, bis_soc: float) -> float:
        if bis_soc <= von_soc + _EPS:
            return 0.0
        ende = self._bis(bis_soc)
        if ende == math.inf:
            return math.inf
        return max(0.0, ende - self._bis(von_soc))


# ---------------------------------------------------------------------------
# Die Planung
# ---------------------------------------------------------------------------

def planen(profil: Streckenprofil, optionen: list[Ladeoption], fz,
           kurve: list[tuple[float, float]], start_soc: float,
           ziel_soc: float = 20.0, max_fahrzeug_kw: float = 1e9,
           temperatur_faktor: float = 1.0,
           umweg_grenze_min: float = UMWEG_GRENZE_MIN) -> Ladeplan:
    """Die zeitoptimale Folge von Ladestopps.

    `fz` sind die Fahrzeugwerte aus dem Verbrauchsmodell (`akku_netto_kwh` und
    `reserve_soc` werden gebraucht). `kurve` sind die Stützstellen der
    Ladekurve als `(SoC, kW)`.
    """
    plan = Ladeplan()
    if not profil.km or len(profil.km) < 2 or fz.akku_netto_kwh <= 0:
        plan.grund = "Kein brauchbares Streckenprofil."
        return plan

    gesamt_km = profil.gesamt_km
    ziel_soc = max(0.0, min(100.0, ziel_soc))

    gefiltert = _kandidaten_ausduennen(optionen, gesamt_km, umweg_grenze_min)
    plan.gepruefte_kandidaten = len(gefiltert)

    graph = _Graph(profil, gefiltert, fz, kurve, max_fahrzeug_kw,
                   temperatur_faktor)

    # Schritt 3: Pareto-Dijkstra.
    weg = graph.suchen(start_soc, ziel_soc)
    if weg is None:
        plan.grund = graph.luecke_beschreiben(start_soc, gefiltert)
        return plan
    stopps, abfahrten_grob = weg

    # Schritt 4: Nachoptimierung auf feinem Raster bei fester Stoppfolge. Sie
    # kann nur besser werden als die Suche - findet sie nichts, gilt deren
    # Ergebnis unverändert weiter.
    abfahrten = graph.nachoptimieren(stopps, start_soc, ziel_soc) or abfahrten_grob

    # Schritt 5 und Zusammenbau.
    return graph.plan_bauen(plan, stopps, abfahrten, start_soc)


def _kandidaten_ausduennen(optionen: list[Ladeoption], gesamt_km: float,
                           umweg_grenze_min: float) -> list[Ladeoption]:
    """Schritt 1: filtern und je Streckenabschnitt die besten behalten.

    Gefiltert wird über die Umwegzeit, nicht über die Luftlinie: Acht Kilometer
    neben der Autobahn sind belanglos, wenn die Abfahrt gleich kommt, und
    fatal, wenn nicht.
    """
    brauchbar = [o for o in optionen
                 if not o.gesperrt
                 and o.umweg_minuten <= umweg_grenze_min + _EPS
                 and 0.0 < o.km_auf_route < gesamt_km]
    if not brauchbar:
        return []

    brauchbar.sort(key=lambda o: o.km_auf_route)

    # Abschnittsbreite so wählen, dass die Obergrenze eingehalten wird - auf
    # einer Fahrt über 900 km reichen 15-km-Abschnitte sonst nicht aus.
    abschnitte_max = max(1.0, HOECHSTENS_KANDIDATEN / KANDIDATEN_JE_ABSCHNITT)
    breite = max(ABSCHNITT_KM, gesamt_km / abschnitte_max)

    behalten: list[Ladeoption] = []
    for _, gruppe in _gruppieren(brauchbar, breite):
        # Sortierschlüssel in der Reihenfolge, in der es unterwegs zählt:
        # Leistung zuerst (sie bestimmt die Standzeit), dann Redundanz (das
        # Risiko, vor einer belegten Säule zu stehen), dann der Umweg.
        gruppe.sort(key=lambda o: (-o.max_kw, -(o.anzahl_punkte or 1),
                                   o.umweg_minuten))
        behalten.extend(gruppe[:KANDIDATEN_JE_ABSCHNITT])

    behalten.sort(key=lambda o: o.km_auf_route)
    return behalten[:HOECHSTENS_KANDIDATEN]


def _gruppieren(optionen: list[Ladeoption], breite_km: float):
    aktuell: list[Ladeoption] = []
    schluessel = None
    for o in optionen:
        k = int(o.km_auf_route // breite_km)
        if schluessel is None or k == schluessel:
            schluessel = k
            aktuell.append(o)
        else:
            yield schluessel, aktuell
            schluessel, aktuell = k, [o]
    if aktuell:
        yield schluessel, aktuell


class _Graph:
    """Knoten, Kanten und die Suche darauf.

    Knoten 0 ist der Start, 1..n die Kandidaten in Streckenreihenfolge, n+1
    das Ziel.
    """

    def __init__(self, profil: Streckenprofil, optionen: list[Ladeoption], fz,
                 kurve, max_fahrzeug_kw: float, temperatur_faktor: float):
        self.profil = profil
        self.optionen = optionen
        self.fz = fz
        self.kurve = kurve
        self.max_fahrzeug_kw = max_fahrzeug_kw
        self.temperatur_faktor = temperatur_faktor
        self.reserve = fz.reserve_soc
        # Umrechnung kWh -> Prozentpunkte. Ab hier rechnet der Optimierer
        # ausschliesslich in SoC; das spart in der inneren Schleife eine
        # Multiplikation je Auswertung und macht die Schranken lesbar.
        self.soc_je_kwh = 100.0 / fz.akku_netto_kwh

        self.km = [0.0] + [o.km_auf_route for o in optionen] + [profil.gesamt_km]
        self.n = len(self.km)
        self.ziel_index = self.n - 1

        self.kwh = [profil.wert(profil.kwh, k) for k in self.km]
        self.minuten = [profil.wert(profil.minuten, k) for k in self.km]

        self._spitze = self._spitzen_rechnen()
        self._tabellen: dict[float, _Ladezeittabelle] = {}

    # ---------- Etappen ----------

    def _spitzen_rechnen(self) -> list[list[float]]:
        """Der grösste kumulierte Bedarf zwischen zwei Knoten, nicht nur der
        Bedarf am Ende.

        Über einen Pass ist das der Unterschied zwischen "geht" und "bleibt
        oben liegen": Auf der Passhöhe ist der Verbrauch am höchsten, auf der
        Abfahrt holt die Rekuperation einen Teil zurück. Wer nur die Bilanz am
        Etappenende prüft, plant eine Etappe, die in der Mitte nicht machbar
        ist.
        """
        spitze = [[0.0] * self.n for _ in range(self.n)]
        for i in range(self.n):
            lauf = self.kwh[i]
            p = bisect_left(self.profil.km, self.km[i])
            for j in range(i + 1, self.n):
                while p < len(self.profil.km) and self.profil.km[p] <= self.km[j]:
                    if self.profil.kwh[p] > lauf:
                        lauf = self.profil.kwh[p]
                    p += 1
                if self.kwh[j] > lauf:
                    lauf = self.kwh[j]
                spitze[i][j] = lauf - self.kwh[i]
        return spitze

    def netto_soc(self, i: int, j: int) -> float:
        return (self.kwh[j] - self.kwh[i]) * self.soc_je_kwh

    def spitze_soc(self, i: int, j: int) -> float:
        return self._spitze[i][j] * self.soc_je_kwh

    def fahrzeit(self, i: int, j: int) -> float:
        return max(0.0, self.minuten[j] - self.minuten[i])

    def tabelle(self, knoten: int) -> _Ladezeittabelle:
        kw = round(self.optionen[knoten - 1].max_kw, 1)
        if kw not in self._tabellen:
            self._tabellen[kw] = _Ladezeittabelle(
                self.kurve, self.fz.akku_netto_kwh, kw, self.max_fahrzeug_kw,
                self.temperatur_faktor)
        return self._tabellen[kw]

    def mindestladung(self, i: int, j: int, ziel_soc: float) -> float:
        """Mit wie viel Prozent muss man an Knoten i losfahren, um j zu schaffen?

        Zwei Bedingungen, und die schärfere gilt: Unterwegs nie unter die
        Reserve (das ist die Spitze), und am Etappenende der geforderte
        Ladestand (Reserve bei einem Zwischenstopp, der Ziel-SoC am Ziel).
        """
        bedarf_am_ende = ziel_soc if j == self.ziel_index else self.reserve
        return max(self.reserve + self.spitze_soc(i, j),
                   bedarf_am_ende + self.netto_soc(i, j))

    def erreichbar_ueberhaupt(self, i: int, j: int) -> bool:
        """Ist die Etappe mit voller Batterie fahrbar? Monoton in j."""
        return self.reserve + self.spitze_soc(i, j) <= 100.0 + _EPS

    # ---------- Schritt 3: Pareto-Dijkstra ----------

    def suchen(self, start_soc: float, ziel_soc: float):
        """Kostenoptimale Stoppfolge, oder None.

        Optimiert wird nicht auf reine Zeit, sondern auf Zeit **minus
        Redundanzbonus**: Ein Standort mit acht Ladepunkten bekommt gut vier
        Minuten gutgeschrieben, weil das Risiko, vor einer belegten Säule zu
        stehen, dort kleiner ist. Solange niemand echte Verfügbarkeitsdaten
        hat, ist die Anzahl der Ladepunkte die beste verfügbare Näherung.
        Ausgewiesen wird am Ende die echte Zeit, nicht die Kosten.
        """
        # labels[i] = (knoten, soc, kosten, vorgaenger, abfahrt_soc_am_vorgaenger)
        labels: list[tuple] = [(0, start_soc, 0.0, -1, start_soc)]
        haufen: list[tuple[float, int]] = [(0.0, 0)]

        # Dominanz durch bereits abgearbeitete Labels. Weil Dijkstra in
        # aufsteigenden Kosten arbeitet, ist jedes später erzeugte Label
        # mindestens so teuer wie jedes bereits entnommene. Ein neues Label mit
        # nicht mehr Ladung als ein entnommenes ist damit dominiert - und diese
        # Prüfung kostet einen Vergleich statt eines Durchlaufs durch die
        # ganze Front. `erledigt[k]` ist das Maximum der Ladestände, mit denen
        # Knoten k schon abgearbeitet wurde.
        erledigt = [-1.0] * self.n
        # Zusätzlich je Knoten und Ladestand die bisher billigsten Kosten.
        # Zusammen ersetzen beide die lineare Pareto-Front, ohne eines ihrer
        # Labels zu Unrecht zu verwerfen.
        beste: list[dict[float, float]] = [{} for _ in range(self.n)]

        while haufen:
            kosten, lid = heapq.heappop(haufen)
            knoten, soc, gespeichert, _, _ = labels[lid]
            if kosten > gespeichert + _EPS or soc <= erledigt[knoten]:
                continue
            erledigt[knoten] = soc
            if knoten == self.ziel_index:
                # Dijkstra mit nichtnegativen Kanten: das erste Label am Ziel
                # ist das billigste.
                return self._weg_zurueckverfolgen(labels, lid)

            for neu in self._nachfolger(knoten, soc, kosten, ziel_soc, erledigt):
                ziel_knoten, neu_soc, neu_kosten, abfahrt = neu
                if neu_soc <= erledigt[ziel_knoten]:
                    continue
                if neu_kosten >= beste[ziel_knoten].get(neu_soc, math.inf) - _EPS:
                    continue
                beste[ziel_knoten][neu_soc] = neu_kosten
                labels.append((ziel_knoten, neu_soc, neu_kosten, lid, abfahrt))
                heapq.heappush(haufen, (neu_kosten, len(labels) - 1))

        return None

    def _nachfolger(self, knoten: int, soc: float, kosten: float,
                    ziel_soc: float, erledigt: list[float]):
        """Alle Labels, die aus diesem einen Label entstehen.

        Erst die Etappen, dann die Ladeziele, dann das Kreuzprodukt - und
        genau in dieser Reihenfolge, weil die Ladezeit nur vom Ladeziel abhängt
        und nicht davon, wohin man anschliessend fährt. Sie einmal je Ladeziel
        zu integrieren statt einmal je Ladeziel *und* Etappe ist der
        Unterschied zwischen Sekunden und Sekundenbruchteilen.
        """
        etappen = []
        for j in range(knoten + 1, self.n):
            if not self.erreichbar_ueberhaupt(knoten, j):
                # Die Spitze wächst monoton mit j - was hier zu weit ist,
                # bleibt es auch für alle folgenden Knoten.
                break
            min_ziel = self.mindestladung(knoten, j, ziel_soc)
            if min_ziel > 100.0 + _EPS:
                continue
            etappen.append((j, min_ziel, self.netto_soc(knoten, j),
                            self.fahrzeit(knoten, j)))
        if not etappen:
            return

        if knoten == 0:
            # Am Start wird nicht geladen - man fährt mit dem los, was da ist.
            abfahrten = [(soc, 0.0)]
        else:
            option = self.optionen[knoten - 1]
            umweg = option.umweg_minuten
            tabelle = self.tabelle(knoten)
            bonus_roh = redundanz_bonus(option.anzahl_punkte or 1)

            untergrenze = soc + MIN_LADEHUB
            # Das Raster **und** die exakt nötigen Ladestände der Etappen: Der
            # gerade noch tragende Ladehub ist der schnellste Halt überhaupt
            # und liegt fast nie auf dem Raster.
            ziele = set(_raster(untergrenze, SOC_RASTER))
            ziele.update(z for _, z, _, _ in etappen if z >= untergrenze)
            abfahrten = []
            for ladeziel in sorted(ziele):
                if ladeziel > 100.0 + _EPS:
                    break
                ladezeit = tabelle.zeit(soc, ladeziel)
                if ladezeit == math.inf:
                    break
                # Der Bonus wiegt den *Umweg* auf, nie die Ladezeit. Er sagt
                # "hierhin zu fahren lohnt sich, weil hier eher etwas frei
                # ist" - nicht "hier zu laden kostet keine Zeit". Ohne diese
                # Grenze wird ein Halt an einem grossen Ladepark rechnerisch
                # gratis, und es entstehen Stopps, die niemand fährt.
                # Nebenbei bleiben so alle Kanten positiv, was Dijkstra
                # ohnehin braucht.
                bonus = min(bonus_roh, umweg)
                abfahrten.append((ladeziel, umweg + ladezeit - bonus))
            if not abfahrten:
                return

        werte = [a for a, _ in abfahrten]
        for j, min_ziel, netto, fahr in etappen:
            # Zwei Schranken: genug Ladung für die Etappe, und mehr Ladung, als
            # der Zielknoten schon abgearbeitet hat. Die zweite ist nur ein
            # schneller Vorfilter - verworfen wird in `suchen` exakt.
            schranke = max(min_ziel, erledigt[j] + netto + LABEL_RASTER)
            for idx in range(bisect_left(werte, schranke - _EPS), len(werte)):
                abfahrt, aufschlag = abfahrten[idx]
                yield (j, _abrunden(abfahrt - netto),
                       kosten + aufschlag + fahr, abfahrt)

    @staticmethod
    def _weg_zurueckverfolgen(labels, lid) -> tuple[list[int], list[float]]:
        """Stoppfolge und die dort gewählten Abfahrts-SoC.

        Jedes Label merkt sich, mit welchem Ladestand es den *Vorgänger*
        verlassen hat. Rückwärts gelesen ergibt das genau die Lademengen des
        gefundenen Weges - ohne sie ein zweites Mal rechnen zu müssen.
        """
        knoten: list[int] = []
        abfahrten: list[float] = []
        aktuell = lid
        while aktuell >= 0:
            k, _soc, _kosten, vorgaenger, abfahrt = labels[aktuell]
            knoten.append(k)
            abfahrten.append(abfahrt)
            aktuell = vorgaenger
        knoten.reverse()
        abfahrten.reverse()
        # knoten = [Start, Stopp .., Ziel]; abfahrten[i] gehört zu knoten[i-1],
        # die ersten beiden Einträge betreffen also den Start.
        return knoten[1:-1], abfahrten[2:]

    # ---------- Schritt 4: Nachoptimierung ----------

    def nachoptimieren(self, stopps: list[int], start_soc: float,
                       ziel_soc: float) -> list[float]:
        """Bei fester Stoppfolge die Lademengen neu verteilen.

        Die Suche in Schritt 3 arbeitet auf einem 5-Prozent-Raster, damit der
        Graph klein bleibt. Steht die Stoppfolge fest, ist das Problem nur noch
        eindimensional - dann lohnt das feine Raster. Der Effekt ist der aus
        Abschnitt 2.4: Ladehübe wandern in den steilen Teil der Kurve, weil
        dort dieselbe Kilowattstunde weniger Zeit kostet.

        Rückgabe: die Abfahrts-SoC je Stopp.
        """
        if not stopps:
            return []

        knotenfolge = [0] + stopps + [self.ziel_index]
        raster = _feines_raster()

        # stufen[t] bildet den Abfahrts-SoC am Stopp t auf (Ladezeit bis
        # hierher, Abfahrts-SoC am Stopp davor) ab. Der Rückverweis macht das
        # Auflösen am Ende eindeutig.
        stufen: list[dict[float, tuple[float, float | None]]] = []

        ankunft = start_soc - self.netto_soc(0, knotenfolge[1])
        if ankunft + _EPS < self.reserve:
            return []
        stufe = self._stufe_fuellen(knotenfolge[1], knotenfolge[2], ziel_soc,
                                    raster, [(ankunft, 0.0, None)])
        if not stufe:
            return []
        stufen.append(stufe)

        for t in range(1, len(stopps)):
            knoten = knotenfolge[t + 1]
            netto = self.netto_soc(knotenfolge[t], knoten)
            spitze = self.spitze_soc(knotenfolge[t], knoten)
            eingaenge = []
            for d_vor, (zeit_vor, _) in stufen[-1].items():
                if d_vor - spitze + _EPS < self.reserve:
                    continue
                eingaenge.append((d_vor - netto, zeit_vor, d_vor))
            stufe = self._stufe_fuellen(knoten, knotenfolge[t + 2], ziel_soc,
                                        raster, eingaenge)
            if not stufe:
                return []
            stufen.append(stufe)

        # Die Bedingung "am Ziel mindestens der Ziel-SoC" steckt bereits in
        # `mindestladung` für die letzte Etappe - hier bleibt nur das Minimum.
        letzte = stufen[-1]
        d = min(letzte, key=lambda wert: letzte[wert][0])

        abfahrten = [0.0] * len(stopps)
        for t in range(len(stopps) - 1, -1, -1):
            abfahrten[t] = d
            d = stufen[t][d][1]
        return abfahrten

    def _stufe_fuellen(self, knoten: int, naechster: int, ziel_soc: float,
                       raster: list[float],
                       eingaenge: list[tuple[float, float, float | None]]):
        """Eine DP-Stufe: alle sinnvollen Abfahrts-SoC an diesem Stopp.

        `eingaenge` sind Tripel (Ankunfts-SoC, Ladezeit bis hierher,
        Abfahrts-SoC am Stopp davor).
        """
        tab = self.tabelle(knoten)
        min_ziel = self.mindestladung(knoten, naechster, ziel_soc)
        stufe: dict[float, tuple[float, float | None]] = {}
        for ankunft, zeit_vor, herkunft in eingaenge:
            if ankunft + _EPS < self.reserve:
                continue
            # Der Mindestladehub gilt auch hier. Ohne ihn schleift die
            # Nachoptimierung die Ladehübe auf das gerade noch Nötige herunter
            # und erzeugt Stopps von einer Minute - der Dijkstra hatte sie
            # verboten, das DP führte sie wieder ein. Zulässig bleibt es: Der
            # Weg aus Schritt 3 erfüllt die Schranke bereits, es gibt hier
            # also immer eine Lösung.
            untergrenze = max(ankunft + MIN_LADEHUB, min_ziel)
            for d in raster:
                if d + _EPS < untergrenze:
                    continue
                zeit = tab.zeit(ankunft, d)
                if zeit == math.inf:
                    break
                gesamt = zeit_vor + zeit
                vorhanden = stufe.get(d)
                if vorhanden is None or gesamt < vorhanden[0] - _EPS:
                    stufe[d] = (gesamt, herkunft)
        return stufe

    # ---------- Schritt 5 und Zusammenbau ----------

    def plan_bauen(self, plan: Ladeplan, stopps: list[int],
                   abfahrten: list[float], start_soc: float) -> Ladeplan:
        plan.machbar = True
        plan.fahrzeit_minuten = self.profil.gesamt_minuten

        soc = start_soc
        uhr = 0.0
        vorher = 0
        for pos, knoten in enumerate(stopps):
            option = self.optionen[knoten - 1]
            uhr += self.fahrzeit(vorher, knoten)
            ankunft = soc - self.netto_soc(vorher, knoten)
            uhr += option.umweg_minuten

            abfahrt = abfahrten[pos] if pos < len(abfahrten) else ankunft
            abfahrt = max(abfahrt, ankunft)
            ladezeit = self.tabelle(knoten).zeit(ankunft, abfahrt)
            if ladezeit == math.inf:
                ladezeit = 0.0
                abfahrt = ankunft

            ankunft_minute = uhr
            uhr += ladezeit

            plan.stopps.append(Stopp(
                option=option, ankunft_soc=ankunft, abfahrt_soc=abfahrt,
                ladezeit_minuten=ladezeit, umweg_minuten=option.umweg_minuten,
                kwh_geladen=(abfahrt - ankunft) / 100.0 * self.fz.akku_netto_kwh,
                ankunft_minute=ankunft_minute, abfahrt_minute=uhr,
                ausweich=self._ausweich(knoten, ankunft)))

            plan.ladezeit_minuten += ladezeit
            plan.umwegzeit_minuten += option.umweg_minuten
            soc = abfahrt
            vorher = knoten

        plan.soc_am_ziel = soc - self.netto_soc(vorher, self.ziel_index)
        plan.gesamt_minuten = (plan.fahrzeit_minuten + plan.ladezeit_minuten
                               + plan.umwegzeit_minuten)
        return plan

    def _ausweich(self, knoten: int, ankunft_soc: float) -> dict | None:
        """Schritt 5: Wohin käme man noch, wenn hier alles belegt ist?

        Bedingung ist "ohne Nachladen erreichbar" - also mit genau dem
        Ladestand, mit dem man hier ankommt. Wer vor einer belegten Säule
        steht, hat keine Reserve für eine Suche; er braucht einen Namen und
        eine Entfernung, sofort.

        Dabei darf der Ausweichweg in die Reserve hineingehen, aber nicht durch
        sie hindurch: Genau für diesen Fall ist sie da. Der Plan selbst rührt
        sie nie an - er kommt überall mit mindestens `reserve_soc` an -, und
        deshalb wäre ein Ausweichstandort, der die volle Reserve stehen lassen
        muss, fast nie erreichbar. Als harte Grenze bleibt die halbe Reserve.

        Gibt es keinen, ist `None` die richtige Antwort und keine Lücke im
        Plan: Sie heisst "wenn hier alles belegt ist, wird es eng" - und das
        ist genau die Information, die man vorher haben will.
        """
        untergrenze = max(2.0, self.reserve / 2.0)
        bester = None
        for j in range(knoten + 1, self.ziel_index):
            option = self.optionen[j - 1]
            if option.gesperrt:
                continue
            rest = ankunft_soc - self.spitze_soc(knoten, j)
            if rest + _EPS < untergrenze:
                # Die Spitze wächst monoton mit j - was von hier aus nicht mehr
                # reicht, reicht auch für alles Weitere nicht.
                break
            kosten = (self.fahrzeit(knoten, j) + option.umweg_minuten
                      - redundanz_bonus(option.anzahl_punkte or 1))
            if bester is None or kosten < bester[0]:
                bester = (kosten, option,
                          ankunft_soc - self.netto_soc(knoten, j))
        if bester is None:
            return None
        _, option, rest_soc = bester
        return {**option.als_dict(), "ankunft_soc": round(rest_soc, 1)}

    # ---------- Diagnose ----------

    def luecke_beschreiben(self, start_soc: float,
                           kandidaten: list[Ladeoption]) -> str:
        """Warum ging es nicht? Die Antwort ist fast immer eine Lücke.

        Ein blosses "nicht machbar" hilft niemandem. Wer weiss, dass zwischen
        km 210 und km 480 kein erreichbarer Schnelllader im Korridor liegt,
        kann den Radius aufziehen, die Mindestleistung senken oder langsamer
        fahren - und sieht sofort, dass nicht die Software das Problem ist,
        sondern womöglich die noch leere Ladepunkt-Tabelle.
        """
        if not kandidaten:
            return ("Ohne Ladestopp nicht machbar, und im Korridor liegt kein "
                    "passender Ladepunkt. Sind die Ladesäulen importiert?")

        # Erreichbarkeit ohne Rücksicht auf Zeit: von jedem erreichten Knoten
        # aus mit voller Batterie weiter.
        erreicht = {0}
        weiteste = 0
        offen = [0]
        while offen:
            i = offen.pop()
            soc = start_soc if i == 0 else 100.0
            for j in range(i + 1, self.n):
                if not self.erreichbar_ueberhaupt(i, j):
                    break
                if j in erreicht:
                    continue
                if soc - self.spitze_soc(i, j) + _EPS < self.reserve:
                    continue
                erreicht.add(j)
                if j != self.ziel_index:
                    offen.append(j)
                    weiteste = max(weiteste, j)

        bis_km = self.km[weiteste]
        naechster_km = None
        for j in range(weiteste + 1, self.ziel_index):
            naechster_km = self.km[j]
            break
        if naechster_km is None:
            return (f"Ab km {bis_km:.0f} liegt kein weiterer Ladepunkt im "
                    f"Korridor - das Ziel ist von dort nicht erreichbar.")
        return (f"Zwischen km {bis_km:.0f} und km {naechster_km:.0f} liegt kein "
                f"erreichbarer Ladepunkt. Grösserer Radius, geringere "
                f"Mindestleistung oder langsamer fahren.")


# ---------------------------------------------------------------------------
# Kleinkram
# ---------------------------------------------------------------------------

def _abrunden(soc: float) -> float:
    """Ankunfts-SoC auf das Labelraster **abrunden**.

    Abrunden statt Runden ist Absicht: Es hält die Pareto-Front endlich, ohne
    je mehr Ladung zu behaupten, als tatsächlich da ist. Ein Plan, der sich um
    ein halbes Prozent zu optimistisch verrechnet, ist schlechter als einer,
    der ein halbes Prozent verschenkt.
    """
    return math.floor(max(0.0, soc) / LABEL_RASTER) * LABEL_RASTER


def _raster(untergrenze: float, schritt: float):
    """Die Ladeziele ab `untergrenze`: erst der exakt nötige Wert, dann das Raster.

    Der exakte Wert muss dabei sein - er ist der schnellste Halt, der die
    nächste Etappe gerade noch trägt, und liegt fast nie auf dem Raster.
    """
    untergrenze = max(0.0, min(100.0, untergrenze))
    yield untergrenze
    wert = math.ceil((untergrenze + _EPS) / schritt) * schritt
    while wert <= 100.0 + _EPS:
        yield min(100.0, wert)
        wert += schritt


def _feines_raster() -> list[float]:
    werte = [i * SOC_RASTER_FEIN for i in range(int(100.0 / SOC_RASTER_FEIN) + 1)]
    if werte[-1] < 100.0:
        werte.append(100.0)
    return werte
