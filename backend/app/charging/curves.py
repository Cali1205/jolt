"""Charge curves: how much power flows at which state of charge.

"150 kW charging power" is a peak figure that usually applies between 20 and
40 % SoC. At 70 % it may only be 60 kW, at 85 % just 35. Ignoring the
difference leads to planning too few and too long stops.

This gives the rule that saves time: two short charges from 10 to 55 % are
often faster than one long charge from 10 to 90 % - the last thirty
percentage points cost more time than the first sixty.
"""

# The charging step of the numerical integration. 1 percentage point is fine
# enough: on 60 kWh that is 0.6 kWh per step, and within one such step the
# power changes by only a few percent.
STEP_SOC = 1.0


def power_at(curve: list[tuple[float, float]], soc: float,
                 max_charger_kw: float = 1e9, max_vehicle_kw: float = 1e9,
                 temperature_factor: float = 1.0) -> float:
    """Charging power in kW at this state of charge.

    Between the support points the value is interpolated linearly; outside
    them the nearest outer value applies. In the end the result is capped by
    the charger and the vehicle - the curve alone only says what the car
    *could* do.
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
    """Charging time for one charge swing, integrated numerically over the curve.

    Deliberately not a closed formula: the curve is piecewise linear and has
    kinks exactly where the battery management derates. An integral over
    1-percent steps is more accurate than any approximation and costs nothing.
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
    """Derating of the charging power for a cold battery.

    An approximation, but a necessary one: anyone who drives to a fast
    charger in winter without preconditioning often sees 50 kW instead of
    150 - that is the difference between 18 and 50 minutes of stop time and
    therefore larger than any error in the route forecast.
    """
    if batterie_c >= 20.0:
        return 1.0
    if batterie_c <= -5.0:
        return 0.25
    # Between -5 and 20 °C linear from 25 % to 100 %.
    return 0.25 + (batterie_c + 5.0) / 25.0 * 0.75


def as_pairs(charge_curve_points) -> list[tuple[float, float]]:
    """Convert ORM objects into the form this module works with."""
    return sorted((p.soc_percent, p.kw) for p in charge_curve_points)


# ---------------------------------------------------------------------------
# Templates
#
# CAUTION: approximate values, not manufacturer data. They are good enough to
# get started right away, and are adapted to the user's own car through the
# correction factor (energie/calibration.py). Anyone with exact values
# should enter them.
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
