"""Back-calculate the model parameters from a driven route.

`calibration.py` learns **one** number per vehicle: forecast against reality,
all folded into a single correction factor. That is deliberate and right for
its purpose - but there is one kind of error it fundamentally cannot fix. A
factor shifts the consumption curve; it does not rotate it. If the model is
right at 90 km/h and off at 130, a scalar makes it worse at one of the two
places, no matter how it is chosen. Yet exactly this question - "how does
consumption change with speed, and how with gradient?" - is the one a
charging plan hinges on.

**Why this is possible at all.** The model in `model.py` is linear in its
parameters. Divided by the distance, it becomes a balance of forces in which
every summand is a product of a sought parameter and a measured quantity:

    E/s = F_roll + c_w·A·(½ρ⟨v²⟩) + (1/η)·mg·(up/s)
                 + η_regen·mg·(down/s) + P_aux·(1/v) + k_accel·(E_kin/s)

That makes the parameter estimation an ordinary least-squares fit.

**Why per kilometre and not per time window.** If you calculate in absolute
quantities, every regressor grows with the window length, and rolling and air
resistance correlate at 0.97 - not because of physics, but because a longer
window has more of everything. The parameters can then no longer be separated
individually. Normalised, a constant term, a v² term and a 1/v term face each
other; those are distinguishable shapes.

**Why windows and not single segments.** A measurement point spacing of twelve
seconds is about 350 m. The elevation difference over that is of the same
order as the noise of the SRTM elevation data, the energy increment of the
same order as the meter resolution. Over 90 s / 1.5 km both average out,
while the spread in speed and gradient is preserved - and that carries the
information.

**What this calculation cannot do.** Mass and elevation scale multiply the
same term (m·g·Δh). Someone who assumes the mass too low, or whose elevation
data smooth out the climbs, gets the same answer: gradient coefficients that
are too large. The two causes cannot be told apart from a single trip - which
is why `Result.warnings` checks whether η_regen is above 1, which is
physically impossible and points to exactly this case.

Pure, without database and without network - like `model.py`, so that
tools/check_identification.py can run it directly.
"""
import math
from dataclasses import dataclass, field

from ..geo import haversine_m

G = 9.80665
R_AIR = 287.058
P0 = 101325.0

# A window must exceed both thresholds, otherwise it carries too little
# signal.
TIMEFRAME_S = 90.0
TIMEFRAME_M = 1500.0
# A larger measurement point spacing means a gap: what happened in between
# is unknown, and a window across it would be made up.
MAX_SPACING_S = 60.0
# Above this it is a GPS outlier and not a car.
MAX_SPEED_KMH = 190.0
MIN_SEGMENT_M = 5.0
# Inflow above this power while driving is no longer regeneration but a
# charging pillar - the section does not belong in a consumption measurement.
MAX_REGEN_KW = 60.0
# Smoothing width of the elevation profile. SRTM scatters by a few metres;
# reading that scatter as gradient yields hundreds of metres of climb on a
# flat motorway. Same reason as in live/recording.py.
SMOOTHING_M = 600.0

COLUMNS = ("f_roll", "cw_a", "uphill", "downhill", "p_neben", "beschl")


@dataclass
class Sample:
    """What the identification needs from a point - nothing more.

    Deliberately not `models.LivePoint`: the calculation should run without a
    database, and a recorded trip from another source has the same quantities
    under other names.
    """
    time_s: float
    lat: float
    lon: float
    elevation_m: float
    # Cumulative counters of the vehicle in Wh. Their difference is the net
    # energy: what the battery delivered, minus what regeneration brought
    # back.
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
    # All force quantities are **effective** values: the energy is measured at
    # the battery, so the drivetrain efficiency is already included. Anyone
    # comparing them with the raw values from `VehicleValues` has to divide
    # those by eta_drive - `tools/consumption_analysis.py` does exactly that.
    f_roll_n: float = 0.0
    cw_a_m2: float = 0.0
    # Reciprocal of the drivetrain efficiency as it takes effect uphill.
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
    """As in model.py - repeated here so that the module stands on its own."""
    elevation = max(-500.0, min(elevation_m, 9000.0))
    return (P0 * (1.0 - 2.25577e-5 * elevation) ** 5.25588) / (R_AIR * (273.15 + temp_c))


def elevation_smooth(points: list[Sample],
                   timeframe_m: float = SMOOTHING_M) -> list[float]:
    """Moving average over the *distance*, not over the index.

    Averaged over the index, a traffic jam in which a hundred points lie at the
    same spot would flatten the elevation profile there and create a step at
    the exit.
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
    """Condense measurement points into evaluable windows."""
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
        # Aero per segment: ⟨v²⟩ is not ⟨v⟩², and the speed spread within
        # the window is precisely what carries the term.
        aero = sum(0.5 * air_density(temp_c, (x["h1"] + x["h2"]) / 2)
                   * (x["s"] / x["dt"]) ** 2 * x["s"] for x in buffer)
        # Sum the gradient separately by sign, not as a net difference: a
        # window with +50 m climb and -80 m descent has -30 m net, but the
        # climb cost energy and the descent gave back only part of it. Counted
        # net, this difference ends up in the remaining parameters.
        uphill = sum(max(0.0, x["h2"] - x["h1"]) for x in buffer)
        downhill = sum(min(0.0, x["h2"] - x["h1"]) for x in buffer)
        # Acceleration work. Without this term city traffic ends up in the
        # auxiliary-consumer term: both are large at low speed.
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


# ---------- Least-squares fit ----------

def _inverse(a: list[list[float]]) -> list[list[float]]:
    """Gauss-Jordan with partial pivoting. Sufficient for six unknowns."""
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
    """Jacobi rotation - only for the condition number, not for the solution."""
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
    """Force balance per metre. Every summand has the unit newton."""
    X, y, gew = [], [], []
    for f in timeframe:
        s = f.distance_m
        X.append([
            1.0,                                # F_roll
            f.aero_j / s,                       # c_w·A
            mass_kg * G * f.on_m / s,         # 1/η
            mass_kg * G * f.from_m / s,          # η_regen
            f.duration_s / s,                      # P_aux
            f.kin_j / s,                        # acceleration share
        ])
        y.append(f.energy_wh * 3600.0 / s)
        # Weighted by distance: a window over 4 km carries more information
        # than one over 1.5 km, and its specific consumption scatters
        # correspondingly less.
        gew.append(s)
    return X, y, gew


def identifizieren(timeframe: list[Timeframe], mass_kg: float,
                   lam: float = 0.02) -> Result:
    """Estimate the parameters from the windows.

    `lam` is a weak ridge damping on the column-normalised data. It costs a
    little unbiasedness and buys that a trip without gradient or without
    speed changes does not run into a nearly singular matrix and spit out
    wild parameters.
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
    # Residual variance with weighting Var(y_i) = σ²/w_i: the weighted sum of
    # squares divided by the degrees of freedom - *not* additionally by the
    # sum of weights, otherwise the standard errors come out too small by
    # sqrt(Σw).
    sigma2 = ss_res / (n - k) if n > k else float("inf")

    (res.f_roll_n, res.cw_a_m2, res.uphill_factor, res.eta_regen,
     res.p_aux_w, res.accel_share) = beta
    for j, name in enumerate(COLUMNS):
        res.failure[name] = math.sqrt(max(0.0, sigma2 * inv[j][j])) / gauge[j]

    _examine(res)
    return res


def _examine(res: Result) -> None:
    """Check physical bounds and name what is uncertain.

    Not to beautify the numbers, but so that nobody adopts a parameter set
    into the charging plan that describes a trip and not a car.
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
    """Evaluate the identified consumption curve.

    The counterpart of `model.segment_wh`, but with the measured instead of
    the assumed parameters - intended for comparing the two.
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
