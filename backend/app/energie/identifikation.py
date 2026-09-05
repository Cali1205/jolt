"""Die Modellparameter aus einer gefahrenen Strecke zurückrechnen.

`kalibrierung.py` lernt **eine** Zahl je Fahrzeug: Prognose gegen
Wirklichkeit, alles in einen Korrekturfaktor. Das ist bewusst so und für
seinen Zweck richtig - aber es kann eine Sorte Fehler grundsätzlich nicht
beheben. Ein Faktor verschiebt die Verbrauchskurve; er dreht sie nicht. Wenn
das Modell bei 90 km/h stimmt und bei 130 daneben liegt, macht ein Skalar es
an einer der beiden Stellen schlechter, egal wie er gewählt wird. Genau diese
Frage - "wie ändert sich der Verbrauch mit dem Tempo, und wie mit der
Steigung?" - ist aber die, an der ein Ladeplan hängt.

**Warum das überhaupt geht.** Das Modell in `modell.py` ist in seinen
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

Rein, ohne Datenbank und ohne Netz - wie `modell.py`, damit
tools/check_identifikation.py sie direkt durchrechnen kann.
"""
import math
from dataclasses import dataclass, field

from ..geo import haversine_m

G = 9.80665
R_LUFT = 287.058
P0 = 101325.0

# Ein Fenster muss beide Schwellen reissen, sonst trägt es zu wenig Signal.
FENSTER_S = 90.0
FENSTER_M = 1500.0
# Grösserer Messpunktabstand heisst Lücke: Was dazwischen geschah, ist
# unbekannt, und ein Fenster darüber hinweg wäre erfunden.
MAX_ABSTAND_S = 60.0
# Darüber ist es ein GPS-Ausreisser und kein Auto.
MAX_TEMPO_KMH = 190.0
MIN_SEGMENT_M = 5.0
# Zufluss über dieser Leistung im Fahren ist keine Rekuperation mehr,
# sondern eine Säule - der Abschnitt gehört nicht in eine Verbrauchsmessung.
MAX_REKU_KW = 60.0
# Glättungsbreite des Höhenprofils. SRTM streut um einige Meter; wer diese
# Streuung als Steigung liest, findet auf ebener Autobahn Hunderte
# Höhenmeter. Derselbe Grund wie in live/aufzeichnung.py.
GLAETTUNG_M = 600.0

SPALTEN = ("f_roll", "cw_a", "auf", "ab", "p_neben", "beschl")


@dataclass
class Messpunkt:
    """Was die Identifikation von einem Punkt braucht - mehr nicht.

    Bewusst nicht `models.LivePunkt`: Die Rechnung soll ohne Datenbank
    laufen, und eine aufgezeichnete Fahrt aus einer anderen Quelle hat
    dieselben Grössen unter anderen Namen.
    """
    zeit_s: float
    lat: float
    lon: float
    hoehe_m: float
    # Kumulierte Zähler des Fahrzeugs in Wh. Ihre Differenz ist die
    # Nettoenergie: Was die Batterie abgab, abzüglich dessen, was die
    # Rekuperation zurückbrachte.
    entladen_wh: float | None = None
    geladen_wh: float | None = None


@dataclass
class Fenster:
    von_s: float
    bis_s: float
    dauer_s: float
    strecke_m: float
    tempo_kmh: float
    auf_m: float
    ab_m: float
    netto_hoehe_m: float
    steigung_pct: float
    energie_wh: float
    aero_j: float
    kin_j: float

    @property
    def wh_km(self) -> float:
        return self.energie_wh / (self.strecke_m / 1000.0)


@dataclass
class Ergebnis:
    # Alle Kraftgrössen sind **wirksame** Werte: Gemessen wird die Energie an
    # der Batterie, der Antriebswirkungsgrad steckt also schon darin. Wer sie
    # mit den Rohwerten aus `Fahrzeugwerte` vergleicht, muss dort durch
    # eta_antrieb teilen - `tools/verbrauch_analyse.py` tut genau das.
    f_roll_n: float = 0.0
    cw_a_m2: float = 0.0
    # Kehrwert des Antriebswirkungsgrads, wie er am Berg wirksam wird.
    auf_faktor: float = 0.0
    eta_rekup: float = 0.0
    p_neben_w: float = 0.0
    beschl_anteil: float = 0.0
    fehler: dict = field(default_factory=dict)
    r2: float = 0.0
    kondition: float = 0.0
    n_fenster: int = 0
    strecke_km: float = 0.0
    masse_kg: float = 0.0
    warnungen: list = field(default_factory=list)

    def als_dict(self) -> dict:
        return {"f_roll_n": self.f_roll_n, "cw_a_m2": self.cw_a_m2,
                "auf_faktor": self.auf_faktor, "eta_rekup": self.eta_rekup,
                "p_neben_w": self.p_neben_w,
                "beschl_anteil": self.beschl_anteil,
                "r2": self.r2, "kondition": self.kondition,
                "n_fenster": self.n_fenster, "strecke_km": self.strecke_km,
                "masse_kg": self.masse_kg, "warnungen": list(self.warnungen)}


def luftdichte(temp_c: float, hoehe_m: float) -> float:
    """Wie in modell.py - hier wiederholt, damit das Modul für sich steht."""
    hoehe = max(-500.0, min(hoehe_m, 9000.0))
    return (P0 * (1.0 - 2.25577e-5 * hoehe) ** 5.25588) / (R_LUFT * (273.15 + temp_c))


def hoehe_glaetten(punkte: list[Messpunkt],
                   fenster_m: float = GLAETTUNG_M) -> list[float]:
    """Gleitendes Mittel über die *Strecke*, nicht über den Index.

    Über den Index gemittelt würde ein Stau, in dem hundert Punkte auf
    derselben Stelle liegen, das Höhenprofil dort plattdrücken und an der
    Ausfahrt eine Stufe erzeugen.
    """
    if len(punkte) < 3:
        return [p.hoehe_m for p in punkte]
    kum = [0.0]
    for a, b in zip(punkte, punkte[1:]):
        kum.append(kum[-1] + haversine_m(a.lat, a.lon, b.lat, b.lon))
    aus, links = [], 0
    for i in range(len(punkte)):
        while kum[i] - kum[links] > fenster_m / 2:
            links += 1
        rechts = i
        while rechts + 1 < len(punkte) and kum[rechts + 1] - kum[i] < fenster_m / 2:
            rechts += 1
        aus.append(sum(punkte[j].hoehe_m for j in range(links, rechts + 1))
                   / (rechts - links + 1))
    return aus


def fenster_bilden(punkte: list[Messpunkt], masse_kg: float,
                   temp_c: float = 15.0,
                   glaettung_m: float = GLAETTUNG_M) -> list[Fenster]:
    """Messpunkte zu auswertbaren Fenstern verdichten."""
    if len(punkte) < 3:
        return []
    hoehen = hoehe_glaetten(punkte, glaettung_m)

    brauchbar = []
    for i, (a, b) in enumerate(zip(punkte, punkte[1:])):
        dt = b.zeit_s - a.zeit_s
        s = haversine_m(a.lat, a.lon, b.lat, b.lon)
        if not 0 < dt <= MAX_ABSTAND_S or s < MIN_SEGMENT_M:
            brauchbar.append(None)
            continue
        if s / dt * 3.6 > MAX_TEMPO_KMH:
            brauchbar.append(None)
            continue
        if (a.entladen_wh is None or b.entladen_wh is None
                or a.geladen_wh is None or b.geladen_wh is None):
            brauchbar.append(None)
            continue
        de = b.entladen_wh - a.entladen_wh
        dg = b.geladen_wh - a.geladen_wh
        if de < 0 or dg < 0 or dg / dt * 3.6 > MAX_REKU_KW * 1000:
            brauchbar.append(None)
            continue
        brauchbar.append({"i": i, "dt": dt, "s": s, "de": de, "dg": dg,
                          "h1": hoehen[i], "h2": hoehen[i + 1],
                          "a": a, "b": b})

    aus: list[Fenster] = []
    puffer: list[dict] = []

    def abschliessen():
        if not puffer:
            return
        t = sum(x["dt"] for x in puffer)
        s = sum(x["s"] for x in puffer)
        if t < FENSTER_S or s < FENSTER_M:
            return
        energie = sum(x["de"] - x["dg"] for x in puffer)
        # Aero segmentweise: ⟨v²⟩ ist nicht ⟨v⟩², und die Tempostreuung
        # innerhalb des Fensters ist gerade das, was den Term trägt.
        aero = sum(0.5 * luftdichte(temp_c, (x["h1"] + x["h2"]) / 2)
                   * (x["s"] / x["dt"]) ** 2 * x["s"] for x in puffer)
        # Steigung nach Vorzeichen getrennt aufsummieren, nicht als
        # Nettodifferenz: Ein Fenster mit +50 m Anstieg und -80 m Gefälle hat
        # netto -30 m, aber der Anstieg hat Energie gekostet und das Gefälle
        # nur einen Teil davon zurückgegeben. Netto gerechnet landet diese
        # Differenz in den übrigen Parametern.
        auf = sum(max(0.0, x["h2"] - x["h1"]) for x in puffer)
        ab = sum(min(0.0, x["h2"] - x["h1"]) for x in puffer)
        # Beschleunigungsarbeit. Ohne diesen Term landet der Stadtverkehr im
        # Nebenverbraucher-Glied: Beide sind bei niedrigem Tempo gross.
        kin = 0.0
        for x, nx in zip(puffer, puffer[1:]):
            v1, v2 = x["s"] / x["dt"], nx["s"] / nx["dt"]
            kin += max(0.0, 0.5 * masse_kg * (v2 * v2 - v1 * v1))
        netto = puffer[-1]["h2"] - puffer[0]["h1"]
        aus.append(Fenster(
            von_s=puffer[0]["a"].zeit_s, bis_s=puffer[-1]["b"].zeit_s,
            dauer_s=t, strecke_m=s, tempo_kmh=s / t * 3.6,
            auf_m=auf, ab_m=ab, netto_hoehe_m=netto,
            steigung_pct=100.0 * netto / s, energie_wh=energie,
            aero_j=aero, kin_j=kin))

    for eintrag in brauchbar:
        if eintrag is None:
            abschliessen()
            puffer = []
            continue
        puffer.append(eintrag)
        if (sum(x["dt"] for x in puffer) >= FENSTER_S
                and sum(x["s"] for x in puffer) >= FENSTER_M):
            abschliessen()
            puffer = []
    abschliessen()
    return aus


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


def _eigenwerte(a: list[list[float]]) -> list[float]:
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


def _zeilen(fenster: list[Fenster], masse_kg: float) -> tuple[list, list, list]:
    """Kraftbilanz je Meter. Jeder Summand hat die Einheit Newton."""
    X, y, gew = [], [], []
    for f in fenster:
        s = f.strecke_m
        X.append([
            1.0,                                # F_roll
            f.aero_j / s,                       # c_w·A
            masse_kg * G * f.auf_m / s,         # 1/η
            masse_kg * G * f.ab_m / s,          # η_rek
            f.dauer_s / s,                      # P_neben
            f.kin_j / s,                        # Beschleunigungsanteil
        ])
        y.append(f.energie_wh * 3600.0 / s)
        # Gewichtet mit der Strecke: Ein Fenster über 4 km trägt mehr
        # Information als eines über 1,5 km, und sein spezifischer Verbrauch
        # streut entsprechend weniger.
        gew.append(s)
    return X, y, gew


def identifizieren(fenster: list[Fenster], masse_kg: float,
                   lam: float = 0.02) -> Ergebnis:
    """Die Parameter aus den Fenstern schätzen.

    `lam` ist eine schwache Ridge-Dämpfung auf den spaltennormierten Daten.
    Sie kostet etwas Erwartungstreue und kauft dafür, dass eine Fahrt ohne
    Steigung oder ohne Tempowechsel nicht in eine fast singuläre Matrix
    läuft und wilde Parameter ausspuckt.
    """
    k = len(SPALTEN)
    erg = Ergebnis(masse_kg=masse_kg, n_fenster=len(fenster))
    if len(fenster) < 3 * k:
        erg.warnungen.append(
            f"Zu wenige Fenster ({len(fenster)}) für {k} Parameter - "
            "mindestens das Dreifache wäre nötig.")
        if len(fenster) <= k:
            return erg

    X, y, gew = _zeilen(fenster, masse_kg)
    n = len(X)
    erg.strecke_km = sum(gew) / 1000.0
    sg = sum(gew)

    skala = [math.sqrt(sum(g * X[i][j] ** 2 for i, g in enumerate(gew)) / sg)
             or 1.0 for j in range(k)]
    Xs = [[X[i][j] / skala[j] for j in range(k)] for i in range(n)]
    XtX = [[sum(gew[i] * Xs[i][a] * Xs[i][b] for i in range(n))
            for b in range(k)] for a in range(k)]
    ew = _eigenwerte(XtX)
    erg.kondition = math.sqrt(ew[-1] / ew[0]) if ew[0] > 1e-12 else float("inf")
    for j in range(k):
        XtX[j][j] += lam * sg
    Xty = [sum(gew[i] * Xs[i][a] * y[i] for i in range(n)) for a in range(k)]
    try:
        inv = _inverse(XtX)
    except ValueError as fehler:
        erg.warnungen.append(str(fehler))
        return erg
    beta = [sum(inv[a][b] * Xty[b] for b in range(k)) / skala[a]
            for a in range(k)]

    vor = [sum(b * x for b, x in zip(beta, zeile)) for zeile in X]
    my = sum(g * v for g, v in zip(gew, y)) / sg
    ss_tot = sum(g * (v - my) ** 2 for g, v in zip(gew, y))
    ss_res = sum(g * (v - p) ** 2 for g, v, p in zip(gew, y, vor))
    erg.r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    # Residuenvarianz bei Gewichtung Var(y_i) = σ²/w_i: die gewichtete
    # Quadratsumme durch die Freiheitsgrade - *nicht* zusätzlich durch die
    # Gewichtssumme, sonst kommen die Standardfehler um sqrt(Σw) zu klein.
    sigma2 = ss_res / (n - k) if n > k else float("inf")

    (erg.f_roll_n, erg.cw_a_m2, erg.auf_faktor, erg.eta_rekup,
     erg.p_neben_w, erg.beschl_anteil) = beta
    for j, name in enumerate(SPALTEN):
        erg.fehler[name] = math.sqrt(max(0.0, sigma2 * inv[j][j])) / skala[j]

    _pruefen(erg)
    return erg


def _pruefen(erg: Ergebnis) -> None:
    """Physikalische Schranken prüfen und Unsicheres benennen.

    Nicht um die Zahlen zu beschönigen, sondern damit niemand einen
    Parametersatz in den Ladeplan übernimmt, der eine Fahrt beschreibt und
    kein Auto.
    """
    if erg.eta_rekup > 1.0:
        noetig = erg.masse_kg * erg.eta_rekup
        erg.warnungen.append(
            f"η_rekup = {erg.eta_rekup:.2f} liegt über 1 - bergab käme mehr "
            f"zurück als an Lageenergie da war. Entweder ist die Masse zu "
            f"niedrig angesetzt (physikalisch wären ≥ {noetig:.0f} kg nötig) "
            f"oder die Höhendaten bügeln die Anstiege glatt. Beides wirkt "
            f"gleich und ist aus einer Fahrt nicht unterscheidbar.")
    if erg.eta_rekup < 0:
        erg.warnungen.append(
            f"η_rekup = {erg.eta_rekup:.2f} ist negativ - bergab würde "
            "Energie kosten. Die Höhendaten passen nicht zur Strecke.")
    if erg.auf_faktor < 1.0:
        erg.warnungen.append(
            f"1/η = {erg.auf_faktor:.2f} liegt unter 1 - bergauf wäre der "
            "Antrieb verlustfrei.")
    if erg.kondition > 100:
        erg.warnungen.append(
            f"Konditionszahl {erg.kondition:.0f}: Die Fahrt hat zu wenig "
            "Streuung in Tempo oder Steigung, um die Parameter einzeln zu "
            "trennen. Die Summe stimmt, die Aufteilung nicht.")
    for name in SPALTEN:
        wert = getattr(erg, {"f_roll": "f_roll_n", "cw_a": "cw_a_m2",
                             "auf": "auf_faktor", "ab": "eta_rekup",
                             "p_neben": "p_neben_w",
                             "beschl": "beschl_anteil"}[name])
        s = erg.fehler.get(name, 0.0)
        if s > 0 and abs(wert) < 2 * s:
            erg.warnungen.append(
                f"{name}: {wert:.3f} ± {s:.3f} - nicht von null zu "
                "unterscheiden, aus dieser Fahrt nicht bestimmbar.")


def verbrauch_wh_km(erg: Ergebnis, tempo_kmh: float,
                    steigung_pct: float = 0.0, temp_c: float = 15.0,
                    hoehe_m: float = 400.0) -> float:
    """Die identifizierte Verbrauchskurve auswerten.

    Das Gegenstück zu `modell.segment_wh`, aber mit den gemessenen statt den
    angenommenen Parametern - gedacht für den Vergleich der beiden.
    """
    v = tempo_kmh / 3.6
    if v <= 0:
        return float("nan")
    kraft = erg.f_roll_n
    kraft += erg.cw_a_m2 * 0.5 * luftdichte(temp_c, hoehe_m) * v * v
    kraft += erg.p_neben_w / v
    neigung = steigung_pct / 100.0
    kraft += (erg.auf_faktor if neigung >= 0 else erg.eta_rekup) \
        * erg.masse_kg * G * neigung
    return kraft / 3.6      # N = J/m  ->  Wh/km
