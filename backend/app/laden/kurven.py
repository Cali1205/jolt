"""Ladekurven: wie viel Leistung fliesst bei welchem Ladestand.

"150 kW Ladeleistung" ist eine Spitzenangabe, die meist zwischen 20 und 40 %
SoC gilt. Bei 70 % sind es vielleicht noch 60 kW, bei 85 % noch 35. Wer den
Unterschied ignoriert, plant zu wenige und zu lange Stopps.

Daraus folgt die Regel, die den Zeitgewinn bringt: Zweimal kurz von 10 auf
55 % ist oft schneller als einmal lang von 10 auf 90 % - die letzten dreissig
Prozentpunkte kosten mehr Zeit als die ersten sechzig.
"""

# Der Ladeschritt der numerischen Integration. 1 Prozentpunkt ist fein genug:
# Auf 60 kWh sind das 0,6 kWh je Schritt, und innerhalb eines solchen Schritts
# ändert sich die Leistung um wenige Prozent.
SCHRITT_SOC = 1.0


def leistung_bei(kurve: list[tuple[float, float]], soc: float,
                 max_saeule_kw: float = 1e9, max_fahrzeug_kw: float = 1e9,
                 temperatur_faktor: float = 1.0) -> float:
    """Ladeleistung in kW bei diesem Ladestand.

    Zwischen den Stützstellen wird linear interpoliert; ausserhalb gilt der
    jeweils äussere Wert. Begrenzt wird am Ende durch die Säule und das
    Fahrzeug - die Kurve allein sagt nur, was das Auto *könnte*.
    """
    if not kurve:
        return min(max_saeule_kw, max_fahrzeug_kw)

    sortiert = sorted(kurve)
    if soc <= sortiert[0][0]:
        kw = sortiert[0][1]
    elif soc >= sortiert[-1][0]:
        kw = sortiert[-1][1]
    else:
        kw = sortiert[-1][1]
        for (s1, k1), (s2, k2) in zip(sortiert, sortiert[1:]):
            if s1 <= soc <= s2:
                anteil = (soc - s1) / (s2 - s1) if s2 > s1 else 0.0
                kw = k1 + (k2 - k1) * anteil
                break

    return max(0.0, min(kw * temperatur_faktor, max_saeule_kw, max_fahrzeug_kw))


def ladezeit_minuten(kurve: list[tuple[float, float]], von_soc: float,
                     bis_soc: float, akku_netto_kwh: float,
                     max_saeule_kw: float = 1e9, max_fahrzeug_kw: float = 1e9,
                     temperatur_faktor: float = 1.0) -> float:
    """Ladezeit für einen Ladehub, numerisch über die Kurve integriert.

    Bewusst keine geschlossene Formel: Die Kurve ist stückweise linear und hat
    Knicke genau dort, wo das Batteriemanagement abregelt. Ein Integral über
    1-Prozent-Schritte ist genauer als jede Näherung und kostet nichts.
    """
    if bis_soc <= von_soc or akku_netto_kwh <= 0:
        return 0.0

    minuten = 0.0
    soc = von_soc
    while soc < bis_soc:
        stufe = min(SCHRITT_SOC, bis_soc - soc)
        kw = leistung_bei(kurve, soc + stufe / 2.0, max_saeule_kw,
                          max_fahrzeug_kw, temperatur_faktor)
        if kw <= 0.1:
            return float("inf")
        kwh = akku_netto_kwh * stufe / 100.0
        minuten += kwh / kw * 60.0
        soc += stufe
    return round(minuten, 1)


def temperatur_faktor(batterie_c: float) -> float:
    """Abschlag auf die Ladeleistung bei kalter Batterie.

    Eine Näherung, aber eine notwendige: Wer im Winter ohne Vorkonditionierung
    an den Schnelllader fährt, sieht statt 150 kW oft 50 - das ist der
    Unterschied zwischen 18 und 50 Minuten Standzeit und damit grösser als
    jeder Fehler in der Streckenprognose.
    """
    if batterie_c >= 20.0:
        return 1.0
    if batterie_c <= -5.0:
        return 0.25
    # Zwischen -5 und 20 °C linear von 25 % auf 100 %.
    return 0.25 + (batterie_c + 5.0) / 25.0 * 0.75


def als_paare(ladekurvenpunkte) -> list[tuple[float, float]]:
    """ORM-Objekte in die Form bringen, mit der dieses Modul rechnet."""
    return sorted((p.soc_prozent, p.kw) for p in ladekurvenpunkte)


# ---------------------------------------------------------------------------
# Vorlagen
#
# ACHTUNG: Näherungswerte, keine Herstellerangaben. Sie sind gut genug, um
# sofort loszulegen, und werden über den Korrekturfaktor (energie/kalibrierung.py)
# an das eigene Auto herangeführt. Wer genaue Werte hat, trägt sie ein.
# ---------------------------------------------------------------------------

VORLAGEN: list[dict] = [
    {"name": "Allgemeines E-Auto (60 kWh)",
     "akku_brutto_kwh": 64.0, "akku_netto_kwh": 60.0,
     "leermasse_kg": 1800.0, "zuladung_kg": 150.0,
     "c_w": 0.28, "stirnflaeche_m2": 2.30, "c_rr": 0.010,
     "eta_antrieb": 0.88, "eta_rekup": 0.70, "p_neben_w": 350.0,
     "waermepumpe": True, "reserve_soc": 10.0, "ziel_soc": 20.0,
     "max_ladeleistung_kw": 120.0, "steckertyp": "CCS",
     "ladekurve": [(0, 110), (10, 120), (30, 120), (50, 90),
                   (70, 60), (80, 45), (90, 28), (100, 8)]},

    {"name": "Kompakt-SUV (77 kWh)",
     "akku_brutto_kwh": 82.0, "akku_netto_kwh": 77.0,
     "leermasse_kg": 2120.0, "zuladung_kg": 150.0,
     "c_w": 0.28, "stirnflaeche_m2": 2.56, "c_rr": 0.011,
     "eta_antrieb": 0.87, "eta_rekup": 0.70, "p_neben_w": 400.0,
     "waermepumpe": True, "reserve_soc": 10.0, "ziel_soc": 20.0,
     "max_ladeleistung_kw": 135.0, "steckertyp": "CCS",
     "ladekurve": [(0, 120), (10, 135), (35, 130), (50, 100),
                   (65, 75), (80, 50), (90, 30), (100, 8)]},

    {"name": "Aerodynamische Limousine (75 kWh)",
     "akku_brutto_kwh": 79.0, "akku_netto_kwh": 75.0,
     "leermasse_kg": 1830.0, "zuladung_kg": 150.0,
     "c_w": 0.22, "stirnflaeche_m2": 2.22, "c_rr": 0.009,
     "eta_antrieb": 0.90, "eta_rekup": 0.73, "p_neben_w": 300.0,
     "waermepumpe": True, "reserve_soc": 10.0, "ziel_soc": 20.0,
     "max_ladeleistung_kw": 250.0, "steckertyp": "CCS",
     "ladekurve": [(0, 180), (10, 250), (25, 200), (40, 150),
                   (55, 110), (70, 75), (85, 45), (95, 20), (100, 8)]},

    {"name": "800-Volt-Crossover (77 kWh)",
     "akku_brutto_kwh": 77.4, "akku_netto_kwh": 74.0,
     "leermasse_kg": 2100.0, "zuladung_kg": 150.0,
     "c_w": 0.29, "stirnflaeche_m2": 2.65, "c_rr": 0.011,
     "eta_antrieb": 0.89, "eta_rekup": 0.72, "p_neben_w": 400.0,
     "waermepumpe": True, "reserve_soc": 10.0, "ziel_soc": 20.0,
     "max_ladeleistung_kw": 235.0, "steckertyp": "CCS",
     "ladekurve": [(0, 180), (10, 230), (45, 220), (55, 150),
                   (70, 100), (80, 60), (90, 35), (100, 10)]},

    {"name": "VW ID.Buzz Pro (82 kWh)",
     "akku_brutto_kwh": 82.0, "akku_netto_kwh": 77.0,
     "leermasse_kg": 2550.0, "zuladung_kg": 150.0,
     "c_w": 0.29, "stirnflaeche_m2": 2.90, "c_rr": 0.011,
     "eta_antrieb": 0.87, "eta_rekup": 0.68, "p_neben_w": 450.0,
     "waermepumpe": True, "reserve_soc": 10.0, "ziel_soc": 20.0,
     "max_ladeleistung_kw": 170.0, "steckertyp": "CCS",
     "ladekurve": [(0, 140), (10, 170), (30, 165), (50, 120),
                   (65, 90), (80, 55), (90, 30), (100, 8)]},

    {"name": "Kleinwagen (52 kWh, nur AC-nah)",
     "akku_brutto_kwh": 55.0, "akku_netto_kwh": 52.0,
     "leermasse_kg": 1580.0, "zuladung_kg": 120.0,
     "c_w": 0.29, "stirnflaeche_m2": 2.30, "c_rr": 0.010,
     "eta_antrieb": 0.86, "eta_rekup": 0.68, "p_neben_w": 300.0,
     "waermepumpe": False, "reserve_soc": 12.0, "ziel_soc": 20.0,
     "max_ladeleistung_kw": 46.0, "steckertyp": "CCS",
     "ladekurve": [(0, 44), (20, 46), (50, 44), (70, 35),
                   (80, 25), (90, 15), (100, 5)]},
]
