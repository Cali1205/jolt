"""Die Modellparameter aus einer gefahrenen Strecke zurückrechnen.

`calibration.py` lernt **eine** Zahl je Fahrzeug: Prognose gegen
Wirklichkeit, alles in einen Korrekturfaktor. Das ist bewusst so und für
seinen Zweck richtig - aber es kann eine Sorte Fehler grundsätzlich nicht
beheben. Ein Faktor verschiebt die Verbrauchskurve; er dreht sie nicht. Wenn
das Modell bei 90 km/h stimmt und bei 130 daneben liegt, macht ein Skalar es
an einer der beiden Stellen schlechter, egal wie er gewählt wird. Genau diese
Frage - "wie ändert sich der Verbrauch mit dem Tempo, und wie mit der
Steigung?" - ist aber die, an der ein Ladeplan hängt.

**Warum das überhaupt geht.** Das Modell in `model.py` ist in seinen
Parametern linear. Durch die Streckenlänge geteilt wird daraus eine Bilanz
von Kräften, in der jeder Summand ein Produkt aus einem gesuchten Parameter
und einer gemessenen Grösse ist:

    E/s = F_roll + c_w·A·(½ρ⟨v²⟩) + (1/η)·mg·(auf/s)
                 + η_rek·mg·(ab/s) + P_neben·(1/v) + k_beschl·(E_kin/s)

Damit ist die Parameterschätzung eine gewöhnliche Ausgleichsrechnung.

**Warum je Kilometer und nicht je Zeitfenster.** Rechnet man in absoluten
Grössen, wächst jeder Regressor mit der Fensterlänge, und Roll- und
Luftwiderstand korrelieren zu 0,97 - nicht aus Physik, sondern weil ein
längeres Fenster von allem mehr hat. Die Parameter sind dann einzeln nicht
mehr trennbar. Normiert stehen sich ein konstantes Glied, ein v²-Glied und
ein 1/v-Glied gegenüber; das sind unterscheidbare Formen.

**Warum Fenster und nicht Einzelsegmente.** Ein Messpunktabstand von zwölf
Sekunden sind rund 350 m. Der Höhenunterschied darauf liegt in derselben
Grössenordnung wie das Rauschen der SRTM-Höhendaten, der Energiezuwachs in
der Grössenordnung der Zählerauflösung. Über 90 s / 1,5 km mitteln sich
beide heraus, während die Streuung in Tempo und Steigung erhalten bleibt -
und die trägt die Information.

**Was diese Rechnung nicht kann.** Masse und Höhenmassstab multiplizieren
denselben Term (m·g·Δh). Wer die Masse zu niedrig ansetzt oder wessen
Höhendaten die Anstiege glattbügeln, bekommt dieselbe Antwort: zu grosse
Steigungskoeffizienten. Die beiden Ursachen sind aus einer Fahrt heraus
nicht unterscheidbar - deshalb prüft `Ergebnis.warnungen`, ob η_rek über 1
liegt, was physikalisch unmöglich ist und genau auf diesen Fall zeigt.

Rein, ohne Datenbank und ohne Netz - wie `model.py`, damit
tools/check_identification.py sie direkt durchrechnen kann.
"""
import math
from dataclasses import dataclass, field

from ..geo import haversine_m

G = 9.80665
R_AIR = 287.058
P0 = 101325.0

# Ein Fenster muss beide Schwellen reissen, sonst trägt es zu wenig Signal.
TIMEFRAME_S = 90.0
TIMEFRAME_M = 1500.0
# Grösserer Messpunktabstand heisst Lücke: Was dazwischen geschah, ist
# unbekannt, und ein Fenster darüber hinweg wäre erfunden.
MAX_SPACING_S = 60.0
# Darüber ist es ein GPS-Ausreisser und kein Auto.
MAX_SPEED_KMH = 190.0
MIN_SEGMENT_M = 5.0
# Zufluss über dieser Leistung im Fahren ist keine Rekuperation mehr,
# sondern eine Säule - der Abschnitt gehört nicht in eine Verbrauchsmessung.
MAX_REGEN_KW = 60.0
# Glättungsbreite des Höhenprofils. SRTM streut um einige Meter; wer diese
# Streuung als Steigung liest, findet auf ebener Autobahn Hunderte
# Höhenmeter. Derselbe Grund wie in live/recording.py.
SMOOTHING_M = 600.0

COLUMNS = ("f_roll", "cw_a", "uphill", "downhill", "p_neben", "beschl")


@dataclass
class Sample:
    """Was die Identifikation von einem Punkt braucht - mehr nicht.

    Bewusst nicht `models.LivePunkt`: Die Rechnung soll ohne Datenbank
    laufen, und eine aufgezeichnete Fahrt aus einer anderen Quelle hat
    dieselben Grössen unter anderen Namen.
    """
    time_s: float
    lat: float
    lon: float
    elevation_m: float
    # Kumulierte Zähler des Fahrzeugs in Wh. Ihre Differenz ist die
    # Nettoenergie: Was die Batterie abgab, abzüglich dessen, was die
    # Rekuperation zurückbrachte.
    discharge_wh: float | None = None
    charged_wh: float | None = None


@dataclass
class Timeframe:
    from_s: float
    until_s: float
    duration_s: float
    distance_m: float
    speed_kmh: float
    on_m: float
    from_m: float
    net_elevation_m: float
    gradient_pct: float
    energy_wh: float
    aero_j: float
    kin_j: float

    @property
    def wh_km(self) -> float:
        return self.energy_wh / (self.distance_m / 1000.0)


@dataclass
class Result:
    # Alle Kraftgrössen sind **wirksame** Werte: Gemessen wird die Energie an
    # der Batterie, der Antriebswirkungsgrad steckt also schon darin. Wer sie
    # mit den Rohwerten aus `Fahrzeugwerte` vergleicht, muss dort durch
    # eta_antrieb teilen - `tools/consumption_analysis.py` tut genau das.
    f_roll_n: float = 0.0
    cw_a_m2: float = 0.0
    # Kehrwert des Antriebswirkungsgrads, wie er am Berg wirksam wird.
    uphill_factor: float = 0.0
    eta_regen: float = 0.0
    p_aux_w: float = 0.0
    accel_share: float = 0.0
    failure: dict = field(default_factory=dict)
    r2: float = 0.0
    condition_number: float = 0.0
    n_timeframe: int = 0
    distance_km: float = 0.0
    mass_kg: float = 0.0
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"f_roll_n": self.f_roll_n, "cw_a_m2": self.cw_a_m2,
                "uphill_factor": self.uphill_factor, "eta_regen": self.eta_regen,
                "p_aux_w": self.p_aux_w,
                "accel_share": self.accel_share,
                "r2": self.r2, "condition_number": self.condition_number,
                "n_timeframe": self.n_timeframe, "distance_km": self.distance_km,
                "mass_kg": self.mass_kg, "warnings": list(self.warnings)}


def air_density(temp_c: float, elevation_m: float) -> float:
    """Wie in model.py - hier wiederholt, damit das Modul für sich steht."""
    elevation = max(-500.0, min(elevation_m, 9000.0))
    return (P0 * (1.0 - 2.25577e-5 * elevation) ** 5.25588) / (R_AIR * (273.15 + temp_c))


def elevation_smooth(points: list[Sample],
                   timeframe_m: float = SMOOTHING_M) -> list[float]:
    """Gleitendes Mittel über die *Strecke*, nicht über den Index.

    Über den Index gemittelt würde ein Stau, in dem hundert Punkte auf
    derselben Stelle liegen, das Höhenprofil dort plattdrücken und an der
    Ausfahrt eine Stufe erzeugen.
    """
    if len(points) < 3:
        return [p.elevation_m for p in points]
    cum = [0.0]
    for a, b in zip(points, points[1:]):
        cum.append(cum[-1] + haversine_m(a.lat, a.lon, b.lat, b.lon))
    origin_of, left_side = [], 0
    for i in range(len(points)):
        while cum[i] - cum[left_side] > timeframe_m / 2:
            left_side += 1
        right = i
        while right + 1 < len(points) and cum[right + 1] - cum[i] < timeframe_m / 2:
            right += 1
        origin_of.append(sum(points[j].elevation_m for j in range(left_side, right + 1))
                   / (right - left_side + 1))
    return origin_of


def build_timeframe(points: list[Sample], mass_kg: float,
                   temp_c: float = 15.0,
                   smoothing_m: float = SMOOTHING_M) -> list[Timeframe]:
    """Messpunkte zu auswertbaren Fenstern verdichten."""
    if len(points) < 3:
        return []
    elevations = elevation_smooth(points, smoothing_m)

    usable = []
    for i, (a, b) in enumerate(zip(points, points[1:])):
        dt = b.time_s - a.time_s
        s = haversine_m(a.lat, a.lon, b.lat, b.lon)
        if not 0 < dt <= MAX_SPACING_S or s < MIN_SEGMENT_M:
            usable.append(None)
            continue
        if s / dt * 3.6 > MAX_SPEED_KMH:
            usable.append(None)
            continue
        if (a.discharge_wh is None or b.discharge_wh is None
                or a.charged_wh is None or b.charged_wh is None):
            usable.append(None)
            continue
        de = b.discharge_wh - a.discharge_wh
        dg = b.charged_wh - a.charged_wh
        if de < 0 or dg < 0 or dg / dt * 3.6 > MAX_REGEN_KW * 1000:
            usable.append(None)
            continue
        usable.append({"i": i, "dt": dt, "s": s, "de": de, "dg": dg,
                          "h1": elevations[i], "h2": elevations[i + 1],
                          "a": a, "b": b})

    origin_of: list[Timeframe] = []
    buffer: list[dict] = []

    def complete():
        if not buffer:
            return
        t = sum(x["dt"] for x in buffer)
        s = sum(x["s"] for x in buffer)
        if t < TIMEFRAME_S or s < TIMEFRAME_M:
            return
        energy = sum(x["de"] - x["dg"] for x in buffer)
        # Aero segmentweise: ⟨v²⟩ ist nicht ⟨v⟩², und die Tempostreuung
        # innerhalb des Fensters ist gerade das, was den Term trägt.
        aero = sum(0.5 * air_density(temp_c, (x["h1"] + x["h2"]) / 2)
                   * (x["s"] / x["dt"]) ** 2 * x["s"] for x in buffer)
        # Steigung nach Vorzeichen getrennt aufsummieren, nicht als
        # Nettodifferenz: Ein Fenster mit +50 m Anstieg und -80 m Gefälle hat
        # netto -30 m, aber der Anstieg hat Energie gekostet und das Gefälle
        # nur einen Teil davon zurückgegeben. Netto gerechnet landet diese
        # Differenz in den übrigen Parametern.
        uphill = sum(max(0.0, x["h2"] - x["h1"]) for x in buffer)
        downhill = sum(min(0.0, x["h2"] - x["h1"]) for x in buffer)
        # Beschleunigungsarbeit. Ohne diesen Term landet der Stadtverkehr im
        # Nebenverbraucher-Glied: Beide sind bei niedrigem Tempo gross.
        kin = 0.0
        for x, nx in zip(buffer, buffer[1:]):
            v1, v2 = x["s"] / x["dt"], nx["s"] / nx["dt"]
            kin += max(0.0, 0.5 * mass_kg * (v2 * v2 - v1 * v1))
        net = buffer[-1]["h2"] - buffer[0]["h1"]
        origin_of.append(Timeframe(
            from_s=buffer[0]["a"].time_s, until_s=buffer[-1]["b"].time_s,
            duration_s=t, distance_m=s, speed_kmh=s / t * 3.6,
            on_m=uphill, from_m=downhill, net_elevation_m=net,
            gradient_pct=100.0 * net / s, energy_wh=energy,
            aero_j=aero, kin_j=kin))

    for entry in usable:
        if entry is None:
            complete()
            buffer = []
            continue
        buffer.append(entry)
        if (sum(x["dt"] for x in buffer) >= TIMEFRAME_S
                and sum(x["s"] for x in buffer) >= TIMEFRAME_M):
            complete()
            buffer = []
    complete()
    return origin_of


# ---------- Ausgleichsrechnung ----------

def _inverse(a: list[list[float]]) -> list[list[float]]:
    """Gauss-Jordan mit Teilpivotisierung. Bei sechs Unbekannten genügt das."""
    n = len(a)
    m = [list(z) + [1.0 if i == j else 0.0 for j in range(n)]
         for i, z in enumerate(a)]
    for k in range(n):
        piv = max(range(k, n), key=lambda r: abs(m[r][k]))
        if abs(m[piv][k]) < 1e-14:
            raise ValueError("Regressoren linear abhängig")
        m[k], m[piv] = m[piv], m[k]
        p = m[k][k]
        m[k] = [v / p for v in m[k]]
        for r in range(n):
            if r != k and m[r][k]:
                fk = m[r][k]
                m[r] = [v - fk * u for v, u in zip(m[r], m[k])]
    return [z[n:] for z in m]


def _eigenvalues(a: list[list[float]]) -> list[float]:
    """Jacobi-Rotation - nur für die Konditionszahl, nicht für die Lösung."""
    n = len(a)
    m = [list(z) for z in a]
    for _ in range(200):
        gr, p, q = 0.0, 0, 1
        for i in range(n):
            for j in range(i + 1, n):
                if abs(m[i][j]) > gr:
                    gr, p, q = abs(m[i][j]), i, j
        if gr < 1e-12:
            break
        th = 0.5 * math.atan2(2 * m[p][q], m[p][p] - m[q][q])
        c, s = math.cos(th), math.sin(th)
        for i in range(n):
            mp, mq = m[i][p], m[i][q]
            m[i][p], m[i][q] = c * mp + s * mq, -s * mp + c * mq
        for i in range(n):
            mp, mq = m[p][i], m[q][i]
            m[p][i], m[q][i] = c * mp + s * mq, -s * mp + c * mq
    return sorted(abs(m[i][i]) for i in range(n))


def _rows(timeframe: list[Timeframe], mass_kg: float) -> tuple[list, list, list]:
    """Kraftbilanz je Meter. Jeder Summand hat die Einheit Newton."""
    X, y, gew = [], [], []
    for f in timeframe:
        s = f.distance_m
        X.append([
            1.0,                                # F_roll
            f.aero_j / s,                       # c_w·A
            mass_kg * G * f.on_m / s,         # 1/η
            mass_kg * G * f.from_m / s,          # η_rek
            f.duration_s / s,                      # P_neben
            f.kin_j / s,                        # Beschleunigungsanteil
        ])
        y.append(f.energy_wh * 3600.0 / s)
        # Gewichtet mit der Strecke: Ein Fenster über 4 km trägt mehr
        # Information als eines über 1,5 km, und sein spezifischer Verbrauch
        # streut entsprechend weniger.
        gew.append(s)
    return X, y, gew


def identifizieren(timeframe: list[Timeframe], mass_kg: float,
                   lam: float = 0.02) -> Result:
    """Die Parameter aus den Fenstern schätzen.

    `lam` ist eine schwache Ridge-Dämpfung auf den spaltennormierten Daten.
    Sie kostet etwas Erwartungstreue und kauft dafür, dass eine Fahrt ohne
    Steigung oder ohne Tempowechsel nicht in eine fast singuläre Matrix
    läuft und wilde Parameter ausspuckt.
    """
    k = len(COLUMNS)
    res = Result(mass_kg=mass_kg, n_timeframe=len(timeframe))
    if len(timeframe) < 3 * k:
        res.warnings.append(
            f"Zu wenige Fenster ({len(timeframe)}) für {k} Parameter - "
            "mindestens das Dreifache wäre nötig.")
        if len(timeframe) <= k:
            return res

    X, y, gew = _rows(timeframe, mass_kg)
    n = len(X)
    res.distance_km = sum(gew) / 1000.0
    sg = sum(gew)

    gauge = [math.sqrt(sum(g * X[i][j] ** 2 for i, g in enumerate(gew)) / sg)
             or 1.0 for j in range(k)]
    Xs = [[X[i][j] / gauge[j] for j in range(k)] for i in range(n)]
    XtX = [[sum(gew[i] * Xs[i][a] * Xs[i][b] for i in range(n))
            for b in range(k)] for a in range(k)]
    ew = _eigenvalues(XtX)
    res.condition_number = math.sqrt(ew[-1] / ew[0]) if ew[0] > 1e-12 else float("inf")
    for j in range(k):
        XtX[j][j] += lam * sg
    Xty = [sum(gew[i] * Xs[i][a] * y[i] for i in range(n)) for a in range(k)]
    try:
        inv = _inverse(XtX)
    except ValueError as failure:
        res.warnings.append(str(failure))
        return res
    beta = [sum(inv[a][b] * Xty[b] for b in range(k)) / gauge[a]
            for a in range(k)]

    prior = [sum(b * x for b, x in zip(beta, row)) for row in X]
    my = sum(g * v for g, v in zip(gew, y)) / sg
    ss_tot = sum(g * (v - my) ** 2 for g, v in zip(gew, y))
    ss_res = sum(g * (v - p) ** 2 for g, v, p in zip(gew, y, prior))
    res.r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    # Residuenvarianz bei Gewichtung Var(y_i) = σ²/w_i: die gewichtete
    # Quadratsumme durch die Freiheitsgrade - *nicht* zusätzlich durch die
    # Gewichtssumme, sonst kommen die Standardfehler um sqrt(Σw) zu klein.
    sigma2 = ss_res / (n - k) if n > k else float("inf")

    (res.f_roll_n, res.cw_a_m2, res.uphill_factor, res.eta_regen,
     res.p_aux_w, res.accel_share) = beta
    for j, name in enumerate(COLUMNS):
        res.failure[name] = math.sqrt(max(0.0, sigma2 * inv[j][j])) / gauge[j]

    _examine(res)
    return res


def _examine(res: Result) -> None:
    """Physikalische Schranken prüfen und Unsicheres benennen.

    Nicht um die Zahlen zu beschönigen, sondern damit niemand einen
    Parametersatz in den Ladeplan übernimmt, der eine Fahrt beschreibt und
    kein Auto.
    """
    if res.eta_regen > 1.0:
        required = res.mass_kg * res.eta_regen
        res.warnings.append(
            f"η_rekup = {res.eta_regen:.2f} liegt über 1 - bergab käme mehr "
            f"zurück als an Lageenergie da war. Entweder ist die Masse zu "
            f"niedrig angesetzt (physikalisch wären ≥ {required:.0f} kg nötig) "
            f"oder die Höhendaten bügeln die Anstiege glatt. Beides wirkt "
            f"gleich und ist aus einer Fahrt nicht unterscheidbar.")
    if res.eta_regen < 0:
        res.warnings.append(
            f"η_rekup = {res.eta_regen:.2f} ist negativ - bergab würde "
            "Energie kosten. Die Höhendaten passen nicht zur Strecke.")
    if res.uphill_factor < 1.0:
        res.warnings.append(
            f"1/η = {res.uphill_factor:.2f} liegt unter 1 - bergauf wäre der "
            "Antrieb verlustfrei.")
    if res.condition_number > 100:
        res.warnings.append(
            f"Konditionszahl {res.condition_number:.0f}: Die Fahrt hat zu wenig "
            "Streuung in Tempo oder Steigung, um die Parameter einzeln zu "
            "trennen. Die Summe stimmt, die Aufteilung nicht.")
    for name in COLUMNS:
        val = getattr(res, {"f_roll": "f_roll_n", "cw_a": "cw_a_m2",
                             "uphill": "uphill_factor", "downhill": "eta_regen",
                             "p_neben": "p_aux_w",
                             "beschl": "accel_share"}[name])
        s = res.failure.get(name, 0.0)
        if s > 0 and abs(val) < 2 * s:
            res.warnings.append(
                f"{name}: {val:.3f} ± {s:.3f} - nicht von null zu "
                "unterscheiden, aus dieser Fahrt nicht bestimmbar.")


def consumption_wh_km(res: Result, speed_kmh: float,
                    gradient_pct: float = 0.0, temp_c: float = 15.0,
                    elevation_m: float = 400.0) -> float:
    """Die identifizierte Verbrauchskurve auswerten.

    Das Gegenstück zu `modell.segment_wh`, aber mit den gemessenen statt den
    angenommenen Parametern - gedacht für den Vergleich der beiden.
    """
    v = speed_kmh / 3.6
    if v <= 0:
        return float("nan")
    force = res.f_roll_n
    force += res.cw_a_m2 * 0.5 * air_density(temp_c, elevation_m) * v * v
    force += res.p_aux_w / v
    slope = gradient_pct / 100.0
    force += (res.uphill_factor if slope >= 0 else res.eta_regen) \
        * res.mass_kg * G * slope
    return force / 3.6      # N = J/m  ->  Wh/km
