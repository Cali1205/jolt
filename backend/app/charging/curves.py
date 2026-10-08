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
STEP_SOC = 1.0


def power_at(curve: list[tuple[float, float]], soc: float,
                 max_charger_kw: float = 1e9, max_vehicle_kw: float = 1e9,
                 temperature_factor: float = 1.0) -> float:
    """Ladeleistung in kW bei diesem Ladestand.

    Zwischen den Stützstellen wird linear interpoliert; ausserhalb gilt der
    jeweils äussere Wert. Begrenzt wird am Ende durch die Säule und das
    Fahrzeug - die Kurve allein sagt nur, was das Auto *könnte*.
    """
    if not curve:
        return min(max_charger_kw, max_vehicle_kw)

    sortiert = sorted(curve)
    if soc <= sortiert[0][0]:
        kw = sortiert[0][1]
    elif soc >= sortiert[-1][0]:
        kw = sortiert[-1][1]
    else:
        kw = sortiert[-1][1]
        for (s1, k1), (s2, k2) in zip(sortiert, sortiert[1:]):
            if s1 <= soc <= s2:
                share = (soc - s1) / (s2 - s1) if s2 > s1 else 0.0
                kw = k1 + (k2 - k1) * share
                break

    return max(0.0, min(kw * temperature_factor, max_charger_kw, max_vehicle_kw))


def charge_time_minutes(curve: list[tuple[float, float]], from_soc: float,
                     until_soc: float, battery_net_kwh: float,
                     max_charger_kw: float = 1e9, max_vehicle_kw: float = 1e9,
                     temperature_factor: float = 1.0) -> float:
    """Ladezeit für einen Ladehub, numerisch über die Kurve integriert.

    Bewusst keine geschlossene Formel: Die Kurve ist stückweise linear und hat
    Knicke genau dort, wo das Batteriemanagement abregelt. Ein Integral über
    1-Prozent-Schritte ist genauer als jede Näherung und kostet nichts.
    """
    if until_soc <= from_soc or battery_net_kwh <= 0:
        return 0.0

    mins = 0.0
    soc = from_soc
    while soc < until_soc:
        level = min(STEP_SOC, until_soc - soc)
        kw = power_at(curve, soc + level / 2.0, max_charger_kw,
                          max_vehicle_kw, temperature_factor)
        if kw <= 0.1:
            return float("inf")
        kwh = battery_net_kwh * level / 100.0
        mins += kwh / kw * 60.0
        soc += level
    return round(mins, 1)


def temperature_factor(batterie_c: float) -> float:
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


def as_pairs(charge_curve_points) -> list[tuple[float, float]]:
    """ORM-Objekte in die Form bringen, mit der dieses Modul rechnet."""
    return sorted((p.soc_percent, p.kw) for p in charge_curve_points)


# ---------------------------------------------------------------------------
# Vorlagen
#
# ACHTUNG: Näherungswerte, keine Herstellerangaben. Sie sind gut genug, um
# sofort loszulegen, und werden über den Korrekturfaktor (energie/calibration.py)
# an das eigene Auto herangeführt. Wer genaue Werte hat, trägt sie ein.
# ---------------------------------------------------------------------------

TEMPLATES: list[dict] = [
    {"name": "Allgemeines E-Auto (60 kWh)",
     "battery_gross_kwh": 64.0, "battery_net_kwh": 60.0,
     "curb_mass_kg": 1800.0, "payload_kg": 150.0,
     "c_w": 0.28, "frontal_area_m2": 2.30, "c_rr": 0.010,
     "eta_drive": 0.88, "eta_regen": 0.70, "p_aux_w": 350.0,
     "heat_pump": True, "reserve_soc": 10.0, "target_soc": 20.0,
     "max_charge_power_kw": 120.0, "connector_type": "CCS",
     "charge_curve": [(0, 110), (10, 120), (30, 120), (50, 90),
                   (70, 60), (80, 45), (90, 28), (100, 8)]},

    {"name": "Kompakt-SUV (77 kWh)",
     "battery_gross_kwh": 82.0, "battery_net_kwh": 77.0,
     "curb_mass_kg": 2120.0, "payload_kg": 150.0,
     "c_w": 0.28, "frontal_area_m2": 2.56, "c_rr": 0.011,
     "eta_drive": 0.87, "eta_regen": 0.70, "p_aux_w": 400.0,
     "heat_pump": True, "reserve_soc": 10.0, "target_soc": 20.0,
     "max_charge_power_kw": 135.0, "connector_type": "CCS",
     "charge_curve": [(0, 120), (10, 135), (35, 130), (50, 100),
                   (65, 75), (80, 50), (90, 30), (100, 8)]},

    {"name": "Aerodynamische Limousine (75 kWh)",
     "battery_gross_kwh": 79.0, "battery_net_kwh": 75.0,
     "curb_mass_kg": 1830.0, "payload_kg": 150.0,
     "c_w": 0.22, "frontal_area_m2": 2.22, "c_rr": 0.009,
     "eta_drive": 0.90, "eta_regen": 0.73, "p_aux_w": 300.0,
     "heat_pump": True, "reserve_soc": 10.0, "target_soc": 20.0,
     "max_charge_power_kw": 250.0, "connector_type": "CCS",
     "charge_curve": [(0, 180), (10, 250), (25, 200), (40, 150),
                   (55, 110), (70, 75), (85, 45), (95, 20), (100, 8)]},

    {"name": "800-Volt-Crossover (77 kWh)",
     "battery_gross_kwh": 77.4, "battery_net_kwh": 74.0,
     "curb_mass_kg": 2100.0, "payload_kg": 150.0,
     "c_w": 0.29, "frontal_area_m2": 2.65, "c_rr": 0.011,
     "eta_drive": 0.89, "eta_regen": 0.72, "p_aux_w": 400.0,
     "heat_pump": True, "reserve_soc": 10.0, "target_soc": 20.0,
     "max_charge_power_kw": 235.0, "connector_type": "CCS",
     "charge_curve": [(0, 180), (10, 230), (45, 220), (55, 150),
                   (70, 100), (80, 60), (90, 35), (100, 10)]},

    {"name": "VW ID.Buzz Pro (82 kWh)",
     "battery_gross_kwh": 82.0, "battery_net_kwh": 77.0,
     "curb_mass_kg": 2550.0, "payload_kg": 150.0,
     "c_w": 0.29, "frontal_area_m2": 2.90, "c_rr": 0.011,
     "eta_drive": 0.87, "eta_regen": 0.68, "p_aux_w": 450.0,
     "heat_pump": True, "reserve_soc": 10.0, "target_soc": 20.0,
     "max_charge_power_kw": 170.0, "connector_type": "CCS",
     "charge_curve": [(0, 140), (10, 170), (30, 165), (50, 120),
                   (65, 90), (80, 55), (90, 30), (100, 8)]},

    {"name": "Kleinwagen (52 kWh, nur AC-nah)",
     "battery_gross_kwh": 55.0, "battery_net_kwh": 52.0,
     "curb_mass_kg": 1580.0, "payload_kg": 120.0,
     "c_w": 0.29, "frontal_area_m2": 2.30, "c_rr": 0.010,
     "eta_drive": 0.86, "eta_regen": 0.68, "p_aux_w": 300.0,
     "heat_pump": False, "reserve_soc": 12.0, "target_soc": 20.0,
     "max_charge_power_kw": 46.0, "connector_type": "CCS",
     "charge_curve": [(0, 44), (20, 46), (50, 44), (70, 35),
                   (80, 25), (90, 15), (100, 5)]},
]
