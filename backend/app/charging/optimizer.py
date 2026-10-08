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
tools/check_optimizer.py vollständig durchrechnen - so wie das Verbrauchsmodell
auch.
"""
import heapq
import math
from bisect import bisect_left
from dataclasses import dataclass, field

from .curves import power_at
from .availability import (CHARGE_PARK_BONUS_MIN, operator_bonus,
                             redundancy_bonus)

# Kandidaten mit mehr Umweg fallen raus: Sie gewinnen die Zeit an der Säule
# fast nie zurück. Fünfzehn Minuten Umweg sind fünfzehn Minuten Ladezeit, und
# die bekommt man an einem 150-kW-Lader für rund 30 kWh.
#
# War zuvor 10.0. Auf langen Strecken mit dünnerer Korridor-Abdeckung (z.B.
# Besançon-Dijon-Chalon-Brive auf dem Weg nach Südwestfrankreich) lag jeder
# erreichbare Kandidat 11-23 Minuten abseits der Route und fiel komplett aus
# der Planung, obwohl die Etappe mit einem einzigen zusätzlichen Umweg von
# gut zehn Minuten fahrbar gewesen wäre.
DETOUR_LIMIT_MIN = 15.0

# Raster der Ladeziele in der Suche. Fünf Prozentpunkte halten den Graphen
# klein; die Quantisierung holt Schritt 4 anschliessend wieder herein.
SOC_GRID = 5.0
# Raster der Nachoptimierung - fünfmal feiner, weil dort die Stoppfolge schon
# feststeht und nur noch die Lademengen gesucht werden.
SOC_GRID_FINE = 1.0

# Was ein Halt kostet, bevor das erste Elektron fliesst - und nachdem das
# letzte geflossen ist. Von der Route abfahren und wieder auffädeln steckt
# bereits in `umweg_minuten`; hier stehen Einparken, Kabel holen,
# Freischalten, das Warten auf den Handshake und hinterher dasselbe rückwärts.
#
# **Ohne diesen Posten war die Zielfunktion blind für die Anzahl der Stopps.**
# Sie zählte Ladezeit und Umweg, sonst nichts. Ein Akku lädt bei 10 % aber
# weit schneller als bei 60 %, und deshalb ist es unter dieser Annahme immer
# günstiger, dieselbe Energie auf viele kurze Halte bei niedrigem Ladestand zu
# verteilen, statt auf wenige lange. Der Optimierer tat genau das: auf der
# Fahrt Périgueux-Vichy sieben Stopps von zwei bis sechs Minuten, jedes Mal
# bis auf 10-12 % herunter und dann ein Schluck im steilsten Teil der Kurve.
# Rechnerisch optimal, praktisch Unsinn - niemand fährt siebenmal ab, um
# dreimal zwei Minuten zu laden.
#
# Fünf Minuten sind bewusst nicht knapp gewählt. Wer den Posten zu klein
# ansetzt, bekommt die Zersplitterung abgeschwächt zurück; wer ihn zu gross
# ansetzt, verliert höchstens einen sinnvollen Zwischenstopp - und das ist
# der harmlosere Fehler.
STOP_FIXED_COST_MIN = 5.0

# Wie viel an einem Halt mindestens geladen werden muss, damit er sich lohnt.
#
# Seit die Fixkosten oben in der Zielfunktion stehen, ist das nur noch ein
# Sicherheitsnetz und nicht mehr der Mechanismus: Ein Halt, der sich nicht
# lohnt, wird jetzt schon deshalb nicht gewählt, weil er fünf Minuten kostet.
# Die Schranke bleibt trotzdem - sie hält Halte aus dem Plan, die an einer
# schwachen Säule rechnerisch knapp aufgehen, und kostet nichts.
MIN_CHARGE_SWING = 8.0

# Ausdünnung der Kandidaten: je Streckenabschnitt die besten N. Ohne das
# stünden an einem Autobahnkreuz zwanzig gleichwertige Standorte im Graphen
# und kosteten Rechenzeit ohne Erkenntnis.
CANDIDATES_PER_SECTION = 3
SECTION_KM = 15.0
AT_MOST_CANDIDATES = 60

# Die Ankunfts-SoC der Labels wird auf dieses Raster **abgerundet**. Das hält
# die Pareto-Front endlich, ohne je mehr Ladung zu behaupten, als da ist -
# Abrunden ist die pessimistische Richtung.
LABEL_GRID = 0.5

# Was eine Stunde des Fahrers wert ist, in Euro. Damit wird aus Geld eine
# Zeit, und die Zielfunktion bleibt eine einzige Grösse - Dijkstra braucht
# das, und man kann weiter alles in Minuten lesen.
#
# 30 EUR/h heisst: Ein Euro wiegt zwei Minuten. Wer 60 einstellt, kauft Zeit
# teuer und fährt schneller; wer 10 einstellt, nimmt Umwege für billigen
# Strom in Kauf. **Null heisst "Kosten sind mir gleich"** - dann rechnet
# jolt wie bisher rein auf Zeit.
#
# Ohne diesen Posten war der Wunsch nach einem bestimmten Anbieter nur als
# Zeitgutschrift auszudrücken - eine Vorliebe, als Minuten verkleidet. Der
# Handel, um den es wirklich geht, liess sich damit gar nicht formulieren:
# länger laden, dafür billiger.
TIME_VALUE_EUR_H = 30.0

_EPS = 1e-9


# ---------------------------------------------------------------------------
# Eingaben
# ---------------------------------------------------------------------------

@dataclass
class ChargeOption:
    """Ein möglicher Halt - alles, was der Optimierer über ihn wissen muss.

    Bewusst kein ORM-Objekt: Der Optimierer soll ohne Datenbank prüfbar sein,
    und ein hypothetischer Standort ("was wäre, wenn hier ein 300-kW-Lader
    stünde?") ist so eine Zeile Code statt eines DB-Eintrags.
    """
    id: int
    km_on_route: float
    detour_minutes: float
    max_kw: float
    point_count: int = 1
    name: str = ""
    operator: str = ""
    city: str = ""
    lat: float = 0.0
    lon: float = 0.0
    # Als belegt gemeldet: fällt aus der Planung, bleibt aber als Datensatz da.
    locked: bool = False

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "operator": self.operator,
                "city": self.city, "lat": self.lat, "lon": self.lon,
                "max_kw": self.max_kw, "point_count": self.point_count,
                "km_on_route": round(self.km_on_route, 1),
                "detour_minutes": round(self.detour_minutes, 1)}


@dataclass
class RouteProfile:
    """Kumulierte Energie und Zeit über den Kilometerstand.

    Der entscheidende Punkt: Der Energiebedarf einer Etappe hängt **nicht** vom
    Ladestand ab - ein E-Auto wird beim Laden nicht schwerer. Deshalb genügt
    ein einziger Durchlauf des Verbrauchsmodells, und der Optimierer liest den
    Bedarf jeder Etappe als Differenz zweier kumulierter Werte ab, statt das
    Profil für jede Variante neu zu rechnen.
    """
    km: list[float]
    kwh: list[float]
    mins: list[float]

    @classmethod
    def from_profile(cls, profile) -> "RouteProfile":
        return cls(km=[p.km for p in profile.points],
                   kwh=[p.kwh_cumulative for p in profile.points],
                   mins=[p.minutes_cumulative for p in profile.points])

    @classmethod
    def from_dicts(cls, entries: list[dict]) -> "RouteProfile":
        """Aus dem gespeicherten `Fahrt.energieprofil`."""
        return cls(km=[float(e.get("km", 0.0)) for e in entries],
                   kwh=[float(e.get("kwh", 0.0)) for e in entries],
                   mins=[float(e.get("mins", 0.0)) for e in entries])

    @property
    def total_km(self) -> float:
        return self.km[-1] if self.km else 0.0

    @property
    def total_minutes(self) -> float:
        return self.mins[-1] if self.mins else 0.0

    def val(self, vals: list[float], km: float) -> float:
        """Linear interpoliert - die Stützpunkte liegen rund 250 m auseinander."""
        if not self.km:
            return 0.0
        if km <= self.km[0]:
            return vals[0]
        if km >= self.km[-1]:
            return vals[-1]
        i = bisect_left(self.km, km)
        k0, k1 = self.km[i - 1], self.km[i]
        w0, w1 = vals[i - 1], vals[i]
        if k1 - k0 <= _EPS:
            return w1
        return w0 + (w1 - w0) * (km - k0) / (k1 - k0)


# ---------------------------------------------------------------------------
# Ergebnis
# ---------------------------------------------------------------------------

@dataclass
class Stop:
    option: ChargeOption
    arrival_soc: float
    departure_soc: float
    charge_time_minutes: float
    detour_minutes: float
    kwh_charged: float
    # Minuten seit Abfahrt, inklusive aller vorherigen Stopps und Umwege.
    arrival_minute: float
    departure_minute: float
    # Was diese Ladung kostet. Hinter den Feldern ohne Vorgabewert, weil
    # eine Dataclass das so verlangt - und mit Vorgabe, damit Aufrufer, die
    # keine Preise kennen, unverändert weiterlaufen.
    cost_eur: float = 0.0
    # Der Standort, an den man ohne Nachladen noch käme, wenn hier alles
    # belegt ist. None = es gibt keinen.
    detour_alt: dict | None = None

    def as_dict(self) -> dict:
        return {**self.option.as_dict(),
                "arrival_soc": round(self.arrival_soc, 1),
                "departure_soc": round(self.departure_soc, 1),
                "charge_time_minutes": round(self.charge_time_minutes, 1),
                "cost_eur": round(self.cost_eur, 2),
                "detour_minutes": round(self.detour_minutes, 1),
                "kwh_charged": round(self.kwh_charged, 1),
                "arrival_minute": round(self.arrival_minute),
                "departure_minute": round(self.departure_minute),
                "detour_alt": self.detour_alt}


@dataclass
class ChargePlan:
    feasible: bool = False
    reason: str = ""
    stops: list[Stop] = field(default_factory=list)
    drive_time_minutes: float = 0.0
    charge_time_minutes: float = 0.0
    detour_time_minutes: float = 0.0
    # Einparken, Kabel, Freischalten - je Halt einmal. Eigener Posten, damit
    # in der Bilanz sichtbar bleibt, was die blosse *Anzahl* der Stopps kostet.
    holding_cost_minutes: float = 0.0
    # Was die Ladungen kosten. Kein Zeitposten - er steht neben der Zeit,
    # weil er die zweite Grösse ist, nach der man einen Plan beurteilt.
    cost_eur: float = 0.0
    total_minutes: float = 0.0
    soc_at_target: float = 0.0
    checked_candidates: int = 0

    def as_dict(self) -> dict:
        return {"feasible": self.feasible, "reason": self.reason,
                "stop_count": len(self.stops),
                "stops": [s.as_dict() for s in self.stops],
                "drive_time_minutes": round(self.drive_time_minutes),
                "charge_time_minutes": round(self.charge_time_minutes),
                "detour_time_minutes": round(self.detour_time_minutes),
                "holding_cost_minutes": round(self.holding_cost_minutes),
                "cost_eur": round(self.cost_eur, 2),
                "total_minutes": round(self.total_minutes),
                "soc_at_target": round(self.soc_at_target, 1),
                "checked_candidates": self.checked_candidates}


# ---------------------------------------------------------------------------
# Ladezeit als Tabelle
# ---------------------------------------------------------------------------

class _ChargeTimeTable:
    """Kumulierte Ladezeit von 0 % bis x %, für eine feste Säulenleistung.

    Der Trick, der die Suche bezahlbar macht: Die Ladezeit von a nach b ist
    `T(b) - T(a)`, weil das Integral über die Ladekurve additiv ist. Statt in
    jeder der zehntausenden Kantenauswertungen erneut zu integrieren, wird
    einmal je vorkommender Säulenleistung eine Tabelle gebaut.
    """

    def __init__(self, curve, battery_net_kwh: float, max_charger_kw: float,
                 max_vehicle_kw: float, temperature_factor: float,
                 step: float = 0.5):
        self.step = step
        self.vals: list[float] = [0.0]
        amount_sum = 0.0
        soc = 0.0
        while soc < 100.0 - _EPS:
            kw = power_at(curve, soc + step / 2.0, max_charger_kw,
                              max_vehicle_kw, temperature_factor)
            if kw <= 0.1:
                # Ab hier nimmt das Auto nichts mehr an - alles darüber ist
                # unerreichbar, nicht "dauert lange".
                amount_sum = math.inf
            elif amount_sum != math.inf:
                amount_sum += (battery_net_kwh * step / 100.0) / kw * 60.0
            self.vals.append(amount_sum)
            soc += step

    def _until(self, soc: float) -> float:
        if soc <= 0.0:
            return 0.0
        if soc >= 100.0:
            return self.vals[-1]
        pos = soc / self.step
        i = int(pos)
        if i + 1 >= len(self.vals):
            return self.vals[-1]
        a, b = self.vals[i], self.vals[i + 1]
        if a == math.inf or b == math.inf:
            return math.inf
        return a + (b - a) * (pos - i)

    def timestamp(self, from_soc: float, until_soc: float) -> float:
        if until_soc <= from_soc + _EPS:
            return 0.0
        end = self._until(until_soc)
        if end == math.inf:
            return math.inf
        return max(0.0, end - self._until(from_soc))


# ---------------------------------------------------------------------------
# Die Planung
# ---------------------------------------------------------------------------

def schedule(profile: RouteProfile, options: list[ChargeOption], fz,
           curve: list[tuple[float, float]], start_soc: float,
           target_soc: float = 20.0, max_vehicle_kw: float = 1e9,
           temperature_factor: float = 1.0,
           detour_limit_min: float = DETOUR_LIMIT_MIN,
           preferred_operators: list[str] | None = None,
           stop_fixed_cost_min: float = STOP_FIXED_COST_MIN,
           charge_park_bonus_min: float = CHARGE_PARK_BONUS_MIN,
           price_for=None,
           time_value_eur_h: float = TIME_VALUE_EUR_H,
           km_offset: float = 0.0) -> ChargePlan:
    """Die zeitoptimale Folge von Ladestopps.

    `fz` sind die Fahrzeugwerte aus dem Verbrauchsmodell (`akku_netto_kwh` und
    `reserve_soc` werden gebraucht). `kurve` sind die Stützstellen der
    Ladekurve als `(SoC, kW)`. `bevorzugte_betreiber` begünstigt passende
    Standorte bei der Stoppwahl, schliesst andere aber nicht aus - siehe
    verfuegbarkeit.betreiber_bonus().
    """
    plan = ChargePlan()
    if not profile.km or len(profile.km) < 2 or fz.battery_net_kwh <= 0:
        plan.reason = "Kein brauchbares Streckenprofil."
        return plan

    total_km = profile.total_km
    target_soc = max(0.0, min(100.0, target_soc))

    filtered = _thin_out_candidates(options, total_km, detour_limit_min,
                                       preferred_operators)
    plan.checked_candidates = len(filtered)

    graph = _Graph(profile, filtered, fz, curve, max_vehicle_kw,
                   temperature_factor, preferred_operators,
                   stop_fixed_cost_min, charge_park_bonus_min,
                   price_for, time_value_eur_h)

    # Schritt 3: Pareto-Dijkstra.
    path = graph.seek(start_soc, target_soc)
    if path is None:
        plan.reason = graph.describe_gap(start_soc, filtered,
                                              km_offset)
        return plan
    stops, departures_coarse = path

    # Schritt 4: Nachoptimierung auf feinem Raster bei fester Stoppfolge. Sie
    # kann nur besser werden als die Suche - findet sie nichts, gilt deren
    # Ergebnis unverändert weiter.
    departures = graph.reoptimize(stops, start_soc, target_soc) or departures_coarse

    # Schritt 5 und Zusammenbau.
    return graph.plan_build(plan, stops, departures, start_soc)


def _thin_out_candidates(options: list[ChargeOption], total_km: float,
                           detour_limit_min: float,
                           preferred_operators: list[str] | None = None
                           ) -> list[ChargeOption]:
    """Schritt 1: filtern und je Streckenabschnitt die besten behalten.

    Gefiltert wird über die Umwegzeit, nicht über die Luftlinie: Acht Kilometer
    neben der Autobahn sind belanglos, wenn die Abfahrt gleich kommt, und
    fatal, wenn nicht.
    """
    usable = [o for o in options
                 if not o.locked
                 and o.detour_minutes <= detour_limit_min + _EPS
                 and 0.0 < o.km_on_route < total_km]
    if not usable:
        return []

    usable.sort(key=lambda o: o.km_on_route)

    # Abschnittsbreite so wählen, dass die Obergrenze eingehalten wird - auf
    # einer Fahrt über 900 km reichen 15-km-Abschnitte sonst nicht aus.
    sections_max = max(1.0, AT_MOST_CANDIDATES / CANDIDATES_PER_SECTION)
    extent = max(SECTION_KM, total_km / sections_max)

    keep: list[ChargeOption] = []
    for _, group in _group(usable, extent):
        # Sortierschlüssel in der Reihenfolge, in der es unterwegs zählt:
        # Leistung zuerst (sie bestimmt die Standzeit), dann Redundanz (das
        # Risiko, vor einer belegten Säule zu stehen), dann der Umweg.
        group.sort(key=lambda o: (-o.max_kw, -(o.point_count or 1),
                                   o.detour_minutes))
        selection = group[:CANDIDATES_PER_SECTION]
        # Ein bevorzugter Anbieter darf nicht allein an dieser Sortierung
        # scheitern - sonst bekommt der Betreiber-Bonus aus _nachfolger() ihn
        # in einem dicht besetzten Abschnitt nie zu Gesicht, egal wie klar die
        # Vorgabe war. Ersetzt wird der schwächste Platz, nicht angehängt: die
        # Obergrenze je Abschnitt bleibt bestehen.
        if preferred_operators and not any(
                operator_bonus(o.operator, preferred_operators) > 0
                for o in selection):
            preferred = next(
                (o for o in group[CANDIDATES_PER_SECTION:]
                 if operator_bonus(o.operator, preferred_operators) > 0),
                None)
            if preferred:
                selection = selection[:-1] + [preferred]
        keep.extend(selection)

    keep.sort(key=lambda o: o.km_on_route)
    return keep[:AT_MOST_CANDIDATES]


def _group(options: list[ChargeOption], width_km: float):
    latest: list[ChargeOption] = []
    keyname = None
    for o in options:
        k = int(o.km_on_route // width_km)
        if keyname is None or k == keyname:
            keyname = k
            latest.append(o)
        else:
            yield keyname, latest
            keyname, latest = k, [o]
    if latest:
        yield keyname, latest


class _Graph:
    """Knoten, Kanten und die Suche darauf.

    Knoten 0 ist der Start, 1..n die Kandidaten in Streckenreihenfolge, n+1
    das Ziel.
    """

    def __init__(self, profile: RouteProfile, options: list[ChargeOption], fz,
                 curve, max_vehicle_kw: float, temperature_factor: float,
                 preferred_operators: list[str] | None = None,
                 stop_fixed_cost_min: float = STOP_FIXED_COST_MIN,
                 charge_park_bonus_min: float = CHARGE_PARK_BONUS_MIN,
                 price_for=None, time_value_eur_h: float = TIME_VALUE_EUR_H):
        self.stop_fixed_cost_min = stop_fixed_cost_min
        self.charge_park_bonus_min = charge_park_bonus_min
        self.price_for = price_for or (lambda option: 0.0)
        # Umrechnungsfaktor Euro -> Minuten. Null bei zeitwert 0: Dann sind
        # Kosten gleichgültig und es wird rein auf Zeit optimiert.
        self.cost_weight = (60.0 / time_value_eur_h) if time_value_eur_h > 0 else 0.0
        self.profile = profile
        self.options = options
        self.fz = fz
        self.curve = curve
        self.max_vehicle_kw = max_vehicle_kw
        self.temperature_factor = temperature_factor
        self.preferred_operators = preferred_operators or []
        self.reserve = fz.reserve_soc
        # Umrechnung kWh -> Prozentpunkte. Ab hier rechnet der Optimierer
        # ausschliesslich in SoC; das spart in der inneren Schleife eine
        # Multiplikation je Auswertung und macht die Schranken lesbar.
        self.soc_per_kwh = 100.0 / fz.battery_net_kwh

        self.km = [0.0] + [o.km_on_route for o in options] + [profile.total_km]
        self.n = len(self.km)
        self.target_index = self.n - 1

        self.kwh = [profile.val(profile.kwh, k) for k in self.km]
        self.mins = [profile.val(profile.mins, k) for k in self.km]

        self._peak = self._compute_peaks()
        self._tables: dict[float, _ChargeTimeTable] = {}

    # ---------- Etappen ----------

    def _compute_peaks(self) -> list[list[float]]:
        """Der grösste kumulierte Bedarf zwischen zwei Knoten, nicht nur der
        Bedarf am Ende.

        Über einen Pass ist das der Unterschied zwischen "geht" und "bleibt
        oben liegen": Auf der Passhöhe ist der Verbrauch am höchsten, auf der
        Abfahrt holt die Rekuperation einen Teil zurück. Wer nur die Bilanz am
        Etappenende prüft, plant eine Etappe, die in der Mitte nicht machbar
        ist.
        """
        peak = [[0.0] * self.n for _ in range(self.n)]
        for i in range(self.n):
            cycle = self.kwh[i]
            p = bisect_left(self.profile.km, self.km[i])
            for j in range(i + 1, self.n):
                while p < len(self.profile.km) and self.profile.km[p] <= self.km[j]:
                    if self.profile.kwh[p] > cycle:
                        cycle = self.profile.kwh[p]
                    p += 1
                if self.kwh[j] > cycle:
                    cycle = self.kwh[j]
                peak[i][j] = cycle - self.kwh[i]
        return peak

    def net_soc(self, i: int, j: int) -> float:
        return (self.kwh[j] - self.kwh[i]) * self.soc_per_kwh

    def peak_soc(self, i: int, j: int) -> float:
        return self._peak[i][j] * self.soc_per_kwh

    def drive_time(self, i: int, j: int) -> float:
        return max(0.0, self.mins[j] - self.mins[i])

    def table(self, node: int) -> _ChargeTimeTable:
        kw = round(self.options[node - 1].max_kw, 1)
        if kw not in self._tables:
            self._tables[kw] = _ChargeTimeTable(
                self.curve, self.fz.battery_net_kwh, kw, self.max_vehicle_kw,
                self.temperature_factor)
        return self._tables[kw]

    def min_charge(self, i: int, j: int, target_soc: float) -> float:
        """Mit wie viel Prozent muss man an Knoten i losfahren, um j zu schaffen?

        Zwei Bedingungen, und die schärfere gilt: Unterwegs nie unter die
        Reserve (das ist die Spitze), und am Etappenende der geforderte
        Ladestand (Reserve bei einem Zwischenstopp, der Ziel-SoC am Ziel).
        """
        demand_at_end = target_soc if j == self.target_index else self.reserve
        return max(self.reserve + self.peak_soc(i, j),
                   demand_at_end + self.net_soc(i, j))

    def reachable_at_all(self, i: int, j: int) -> bool:
        """Ist die Etappe mit voller Batterie fahrbar? Monoton in j."""
        return self.reserve + self.peak_soc(i, j) <= 100.0 + _EPS

    # ---------- Schritt 3: Pareto-Dijkstra ----------

    def seek(self, start_soc: float, target_soc: float):
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
        done = [-1.0] * self.n
        # Zusätzlich je Knoten und Ladestand die bisher billigsten Kosten.
        # Zusammen ersetzen beide die lineare Pareto-Front, ohne eines ihrer
        # Labels zu Unrecht zu verwerfen.
        top: list[dict[float, float]] = [{} for _ in range(self.n)]

        while haufen:
            cost, lid = heapq.heappop(haufen)
            node, soc, saved, _, _ = labels[lid]
            if cost > saved + _EPS or soc <= done[node]:
                continue
            done[node] = soc
            if node == self.target_index:
                # Dijkstra mit nichtnegativen Kanten: das erste Label am Ziel
                # ist das billigste.
                return self._path_trace_back(labels, lid)

            for fresh in self._successor(node, soc, cost, target_soc, done):
                target_node, new_soc, new_cost, departure = fresh
                if new_soc <= done[target_node]:
                    continue
                if new_cost >= top[target_node].get(new_soc, math.inf) - _EPS:
                    continue
                top[target_node][new_soc] = new_cost
                labels.append((target_node, new_soc, new_cost, lid, departure))
                heapq.heappush(haufen, (new_cost, len(labels) - 1))

        return None

    def _successor(self, node: int, soc: float, cost: float,
                    target_soc: float, done: list[float]):
        """Alle Labels, die aus diesem einen Label entstehen.

        Erst die Etappen, dann die Ladeziele, dann das Kreuzprodukt - und
        genau in dieser Reihenfolge, weil die Ladezeit nur vom Ladeziel abhängt
        und nicht davon, wohin man anschliessend fährt. Sie einmal je Ladeziel
        zu integrieren statt einmal je Ladeziel *und* Etappe ist der
        Unterschied zwischen Sekunden und Sekundenbruchteilen.
        """
        legs = []
        for j in range(node + 1, self.n):
            if not self.reachable_at_all(node, j):
                # Die Spitze wächst monoton mit j - was hier zu weit ist,
                # bleibt es auch für alle folgenden Knoten.
                break
            min_target = self.min_charge(node, j, target_soc)
            if min_target > 100.0 + _EPS:
                continue
            legs.append((j, min_target, self.net_soc(node, j),
                            self.drive_time(node, j)))
        if not legs:
            return

        if node == 0:
            # Am Start wird nicht geladen - man fährt mit dem los, was da ist.
            departures = [(soc, 0.0)]
        else:
            option = self.options[node - 1]
            detour = option.detour_minutes
            table = self.table(node)
            bonus_raw = (redundancy_bonus(option.point_count or 1,
                                         self.charge_park_bonus_min)
                        + operator_bonus(option.operator,
                                          self.preferred_operators))

            lower_limit = soc + MIN_CHARGE_SWING
            # Das Raster **und** die exakt nötigen Ladestände der Etappen: Der
            # gerade noch tragende Ladehub ist der schnellste Halt überhaupt
            # und liegt fast nie auf dem Raster.
            targets = set(_grid(lower_limit, SOC_GRID))
            targets.update(z for _, z, _, _ in legs if z >= lower_limit)
            departures = []
            for charge_target in sorted(targets):
                if charge_target > 100.0 + _EPS:
                    break
                charge_time = table.timestamp(soc, charge_target)
                if charge_time == math.inf:
                    break
                # Die Gutschrift darf Umweg **und** Fixkosten des Halts
                # aufwiegen, aber nie die Ladezeit. Damit bleibt jede Kante
                # positiv - Dijkstra braucht das - und ein Halt kostet
                # mindestens so viel, wie das Laden dauert.
                #
                # Vorher stand hier `min(bonus_roh, umweg)`, und das war der
                # Fehler: Ein Ladepark **direkt an der Route** hat keinen
                # Umweg, also bekam er auch keine Gutschrift. Ausgerechnet
                # dort, wo die grossen Parks stehen - an der Autobahn -,
                # wirkte die Bevorzugung damit gar nicht. Auf einer
                # Frankreich-Route wurde sie bei sechs von sieben
                # Ionity-Standorten vollständig weggeschnitten.
                #
                # Die Fixkosten mit hereinzunehmen ist dabei kein
                # Zugeständnis, sondern die richtige Bezugsgrösse: Die
                # Gutschrift sagt "dieser Halt ist weniger lästig als ein
                # anderer" - und lästig ist am Halt das Anhalten, nicht das
                # Laden.
                bonus = min(bonus_raw, detour + self.stop_fixed_cost_min)
                # Die Fixkosten des Halts stehen hier und nicht bei der
                # Ladezeit: Sie fallen einmal je Stopp an, unabhängig davon,
                # wie viel geladen wird. Genau das ist der Unterschied, der
                # wenige lange Halte gegen viele kurze gewinnen lässt.
                # Was diese Ladung kostet, in gleichwertigen Minuten. Der
                # Energiebedarf einer Etappe hängt nicht vom Ladestand ab,
                # die *Kosten* eines Halts aber sehr wohl von der Lademenge -
                # deshalb steht das hier je Ladeziel und nicht je Stopp.
                kwh = (charge_target - soc) / 100.0 * self.fz.battery_net_kwh
                cost_min = (kwh * self.price_for(option)
                              * self.cost_weight)
                departures.append((charge_target, self.stop_fixed_cost_min
                                  + detour + charge_time - bonus + cost_min))
            if not departures:
                return

        vals = [a for a, _ in departures]
        for j, min_target, net, drive in legs:
            # Zwei Schranken: genug Ladung für die Etappe, und mehr Ladung, als
            # der Zielknoten schon abgearbeitet hat. Die zweite ist nur ein
            # schneller Vorfilter - verworfen wird in `suchen` exakt.
            barrier = max(min_target, done[j] + net + LABEL_GRID)
            for idx in range(bisect_left(vals, barrier - _EPS), len(vals)):
                departure, surcharge = departures[idx]
                yield (j, _round_down(departure - net),
                       cost + surcharge + drive, departure)

    @staticmethod
    def _path_trace_back(labels, lid) -> tuple[list[int], list[float]]:
        """Stoppfolge und die dort gewählten Abfahrts-SoC.

        Jedes Label merkt sich, mit welchem Ladestand es den *Vorgänger*
        verlassen hat. Rückwärts gelesen ergibt das genau die Lademengen des
        gefundenen Weges - ohne sie ein zweites Mal rechnen zu müssen.
        """
        node: list[int] = []
        departures: list[float] = []
        latest = lid
        while latest >= 0:
            k, _soc, _cost, predecessor, departure = labels[latest]
            node.append(k)
            departures.append(departure)
            latest = predecessor
        node.reverse()
        departures.reverse()
        # knoten = [Start, Stopp .., Ziel]; abfahrten[i] gehört zu knoten[i-1],
        # die ersten beiden Einträge betreffen also den Start.
        return node[1:-1], departures[2:]

    # ---------- Schritt 4: Nachoptimierung ----------

    def reoptimize(self, stops: list[int], start_soc: float,
                       target_soc: float) -> list[float]:
        """Bei fester Stoppfolge die Lademengen neu verteilen.

        Die Suche in Schritt 3 arbeitet auf einem 5-Prozent-Raster, damit der
        Graph klein bleibt. Steht die Stoppfolge fest, ist das Problem nur noch
        eindimensional - dann lohnt das feine Raster. Der Effekt ist der aus
        Abschnitt 2.4: Ladehübe wandern in den steilen Teil der Kurve, weil
        dort dieselbe Kilowattstunde weniger Zeit kostet.

        Rückgabe: die Abfahrts-SoC je Stopp.
        """
        if not stops:
            return []

        node_sequence = [0] + stops + [self.target_index]
        cells = _fine_grid()

        # stufen[t] bildet den Abfahrts-SoC am Stopp t auf (Ladezeit bis
        # hierher, Abfahrts-SoC am Stopp davor) ab. Der Rückverweis macht das
        # Auflösen am Ende eindeutig.
        levels: list[dict[float, tuple[float, float | None]]] = []

        arrival = start_soc - self.net_soc(0, node_sequence[1])
        if arrival + _EPS < self.reserve:
            return []
        level = self._fill_level(node_sequence[1], node_sequence[2], target_soc,
                                    cells, [(arrival, 0.0, None)])
        if not level:
            return []
        levels.append(level)

        for t in range(1, len(stops)):
            node = node_sequence[t + 1]
            net = self.net_soc(node_sequence[t], node)
            peak = self.peak_soc(node_sequence[t], node)
            inputs = []
            for d_prior, (time_prior, _) in levels[-1].items():
                if d_prior - peak + _EPS < self.reserve:
                    continue
                inputs.append((d_prior - net, time_prior, d_prior))
            level = self._fill_level(node, node_sequence[t + 2], target_soc,
                                        cells, inputs)
            if not level:
                return []
            levels.append(level)

        # Die Bedingung "am Ziel mindestens der Ziel-SoC" steckt bereits in
        # `mindestladung` für die letzte Etappe - hier bleibt nur das Minimum.
        tail = levels[-1]
        d = min(tail, key=lambda val: tail[val][0])

        departures = [0.0] * len(stops)
        for t in range(len(stops) - 1, -1, -1):
            departures[t] = d
            d = levels[t][d][1]
        return departures

    def _fill_level(self, node: int, upcoming: int, target_soc: float,
                       cells: list[float],
                       inputs: list[tuple[float, float, float | None]]):
        """Eine DP-Stufe: alle sinnvollen Abfahrts-SoC an diesem Stopp.

        `eingaenge` sind Tripel (Ankunfts-SoC, Ladezeit bis hierher,
        Abfahrts-SoC am Stopp davor).
        """
        tab = self.table(node)
        min_target = self.min_charge(node, upcoming, target_soc)
        level: dict[float, tuple[float, float | None]] = {}
        for arrival, time_prior, origin in inputs:
            if arrival + _EPS < self.reserve:
                continue
            # Der Mindestladehub gilt auch hier. Ohne ihn schleift die
            # Nachoptimierung die Ladehübe auf das gerade noch Nötige herunter
            # und erzeugt Stopps von einer Minute - der Dijkstra hatte sie
            # verboten, das DP führte sie wieder ein. Zulässig bleibt es: Der
            # Weg aus Schritt 3 erfüllt die Schranke bereits, es gibt hier
            # also immer eine Lösung.
            lower_limit = max(arrival + MIN_CHARGE_SWING, min_target)
            for d in cells:
                if d + _EPS < lower_limit:
                    continue
                timestamp = tab.timestamp(arrival, d)
                if timestamp == math.inf:
                    break
                # Die Kosten müssen hier genauso zählen wie in der Suche.
                # Ohne sie minimierte die Nachoptimierung reine Zeit - und
                # weil sie *nach* dem Dijkstra läuft und dessen Lademengen
                # überschreibt, verschob sie Energie von der billigen Säule
                # zur teuren, sobald das ein paar Sekunden sparte. Sie machte
                # damit still zunichte, was Schritt 3 an Kostenoptimierung
                # gerade geleistet hatte.
                cost_min = ((d - arrival) / 100.0 * self.fz.battery_net_kwh
                              * self.price_for(self.options[node - 1])
                              * self.cost_weight)
                total = time_prior + timestamp + cost_min
                present = level.get(d)
                if present is None or total < present[0] - _EPS:
                    level[d] = (total, origin)
        return level

    # ---------- Schritt 5 und Zusammenbau ----------

    def plan_build(self, plan: ChargePlan, stops: list[int],
                   departures: list[float], start_soc: float) -> ChargePlan:
        plan.feasible = True
        plan.drive_time_minutes = self.profile.total_minutes

        soc = start_soc
        clock = 0.0
        earlier = 0
        for pos, node in enumerate(stops):
            option = self.options[node - 1]
            clock += self.drive_time(earlier, node)
            arrival = soc - self.net_soc(earlier, node)
            clock += option.detour_minutes

            departure = departures[pos] if pos < len(departures) else arrival
            departure = max(departure, arrival)
            charge_time = self.table(node).timestamp(arrival, departure)
            if charge_time == math.inf:
                charge_time = 0.0
                departure = arrival

            # Ankunft ist, wann man da ist - die Fixkosten laufen danach,
            # sonst behauptete der Plan eine Ankunft, die schon das Einparken
            # enthält.
            arrival_minute = clock
            clock += self.stop_fixed_cost_min + charge_time

            plan.stops.append(Stop(
                option=option, arrival_soc=arrival, departure_soc=departure,
                charge_time_minutes=charge_time, detour_minutes=option.detour_minutes,
                kwh_charged=(departure - arrival) / 100.0 * self.fz.battery_net_kwh,
                cost_eur=((departure - arrival) / 100.0 * self.fz.battery_net_kwh
                            * self.price_for(option)),
                arrival_minute=arrival_minute, departure_minute=clock,
                detour_alt=self._alternative(node, arrival)))

            plan.charge_time_minutes += charge_time
            plan.detour_time_minutes += option.detour_minutes
            plan.holding_cost_minutes += self.stop_fixed_cost_min
            plan.cost_eur += plan.stops[-1].cost_eur
            soc = departure
            earlier = node

        plan.soc_at_target = soc - self.net_soc(earlier, self.target_index)
        plan.total_minutes = (plan.drive_time_minutes + plan.charge_time_minutes
                               + plan.detour_time_minutes
                               + plan.holding_cost_minutes)
        return plan

    def _alternative(self, node: int, arrival_soc: float) -> dict | None:
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
        lower_limit = max(2.0, self.reserve / 2.0)
        best = None
        for j in range(node + 1, self.target_index):
            option = self.options[j - 1]
            if option.locked:
                continue
            rest = arrival_soc - self.peak_soc(node, j)
            if rest + _EPS < lower_limit:
                # Die Spitze wächst monoton mit j - was von hier aus nicht mehr
                # reicht, reicht auch für alles Weitere nicht.
                break
            cost = (self.drive_time(node, j) + option.detour_minutes
                      - redundancy_bonus(option.point_count or 1))
            if best is None or cost < best[0]:
                best = (cost, option,
                          arrival_soc - self.net_soc(node, j))
        if best is None:
            return None
        _, option, rest_soc = best
        return {**option.as_dict(), "arrival_soc": round(rest_soc, 1)}

    # ---------- Diagnose ----------

    def describe_gap(self, start_soc: float,
                           candidates: list[ChargeOption],
                           km_offset: float = 0.0) -> str:
        """Warum ging es nicht? Die Antwort ist fast immer eine Lücke.

        Ein blosses "nicht machbar" hilft niemandem. Wer weiss, dass zwischen
        km 210 und km 480 kein erreichbarer Schnelllader im Korridor liegt,
        kann den Radius aufziehen, die Mindestleistung senken oder langsamer
        fahren - und sieht sofort, dass nicht die Software das Problem ist,
        sondern womöglich die noch leere Ladepunkt-Tabelle.

        `km_versatz` rechnet die Kilometer auf die **ganze** Fahrt um. Bei
        einer Umplanung unterwegs rechnet der Optimierer auf der Reststrecke,
        die bei null beginnt; die Stopps werden hinterher zurueckgerechnet,
        dieser Satz aber nicht. Er nannte deshalb Kilometer, die es auf der
        Strecke gar nicht gibt - in einem Probelauf stand bei km 137 die
        Meldung "zwischen km 0 und km 44". Wer am Steuer eine Kilometerangabe
        liest, sucht sie auf seiner Route, nicht auf einer gedachten.
        """
        if not candidates:
            return ("Ohne Ladestopp nicht machbar, und im Korridor liegt kein "
                    "passender Ladepunkt. Sind die Ladesäulen importiert?")

        # Erreichbarkeit ohne Rücksicht auf Zeit: von jedem erreichten Knoten
        # aus mit voller Batterie weiter.
        reached = {0}
        farthest = 0
        opened = [0]
        while opened:
            i = opened.pop()
            soc = start_soc if i == 0 else 100.0
            for j in range(i + 1, self.n):
                if not self.reachable_at_all(i, j):
                    break
                if j in reached:
                    continue
                if soc - self.peak_soc(i, j) + _EPS < self.reserve:
                    continue
                reached.add(j)
                if j != self.target_index:
                    opened.append(j)
                    farthest = max(farthest, j)

        until_km = self.km[farthest] + km_offset
        next_km = None
        for j in range(farthest + 1, self.target_index):
            next_km = self.km[j] + km_offset
            break
        if next_km is None:
            return (f"Ab km {until_km:.0f} liegt kein weiterer Ladepunkt im "
                    f"Korridor - das Ziel ist von dort nicht erreichbar.")
        return (f"Zwischen km {until_km:.0f} und km {next_km:.0f} liegt kein "
                f"erreichbarer Ladepunkt. Grösserer Radius, geringere "
                f"Mindestleistung oder langsamer fahren.")


# ---------------------------------------------------------------------------
# Kleinkram
# ---------------------------------------------------------------------------

def _round_down(soc: float) -> float:
    """Ankunfts-SoC auf das Labelraster **abrunden**.

    Abrunden statt Runden ist Absicht: Es hält die Pareto-Front endlich, ohne
    je mehr Ladung zu behaupten, als tatsächlich da ist. Ein Plan, der sich um
    ein halbes Prozent zu optimistisch verrechnet, ist schlechter als einer,
    der ein halbes Prozent verschenkt.
    """
    return math.floor(max(0.0, soc) / LABEL_GRID) * LABEL_GRID


def _grid(lower_limit: float, step: float):
    """Die Ladeziele ab `untergrenze`: erst der exakt nötige Wert, dann das Raster.

    Der exakte Wert muss dabei sein - er ist der schnellste Halt, der die
    nächste Etappe gerade noch trägt, und liegt fast nie auf dem Raster.
    """
    lower_limit = max(0.0, min(100.0, lower_limit))
    yield lower_limit
    val = math.ceil((lower_limit + _EPS) / step) * step
    while val <= 100.0 + _EPS:
        yield min(100.0, val)
        val += step


def _fine_grid() -> list[float]:
    vals = [i * SOC_GRID_FINE for i in range(int(100.0 / SOC_GRID_FINE) + 1)]
    if vals[-1] < 100.0:
        vals.append(100.0)
    return vals
