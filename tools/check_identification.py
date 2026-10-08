#!/usr/bin/env python3
"""Checks the parameter identification on a trip whose truth is known.

The trick of this script: it generates a synthetic trip whose consumption
was computed with **known** parameters, and demands that the least-squares
fit return exactly those parameters. With a real trip this can never be
verified - there every deviation may be the car and not the estimator.

That this is necessary was shown by development: two sign and
normalisation errors produced parameter sets that looked plausible and were
wrong (η_rekup = 1.39 - going downhill would return more than was put in).
Both only showed up on a trip whose answer was known beforehand.

    ./tools/check_identification.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from examine import Check, application_provide  # noqa: E402

# Pure computation - no database, no network.
application_provide("identifikation", db_name=False)

from app.energy import identification as ident  # noqa: E402
from app.energy.identification import Sample  # noqa: E402

verify = Check()

# The truth that has to come back.
TRUTHY = {"f_roll": 200.0, "cw_a": 0.65, "uphill": 1.15, "downhill": 0.70,
        "p_neben": 800.0}
MASS = 2000.0
TEMP = 15.0


def trip_build(speeds, gradients, points_per_section=14, tick_s=12.0):
    """A trip made of segments, each with a fixed speed and a fixed gradient.

    The elevation is set *linearly* and not taken from noisy map data -
    here the estimator is to be checked, not the data quality.
    """
    points, t, distance_m, elevation, discharge, charged = [], 0.0, 0.0, 300.0, 0.0, 0.0
    lat0, lon0 = 48.0, 9.0
    # One degree of latitude is about 111.32 km - that turns metres into a
    # coordinate which haversine_m converts back into the same metres.
    degree_per_m = 1.0 / 111320.0

    for v_kmh, climb in zip(speeds, gradients):
        v = v_kmh / 3.6
        for _ in range(points_per_section):
            ds = v * tick_s
            dh = ds * climb / 100.0
            rho = ident.air_density(TEMP, elevation + dh / 2)
            force = (TRUTHY["f_roll"] + TRUTHY["cw_a"] * 0.5 * rho * v * v
                     + TRUTHY["p_neben"] / v)
            force += (TRUTHY["uphill"] if climb >= 0 else TRUTHY["downhill"]) \
                * MASS * ident.G * (climb / 100.0)
            wh = force * ds / 3600.0
            # Split into two counters, as the vehicle reports it:
            # negative demand is recuperation and accumulates into `charged`.
            if wh >= 0:
                discharge += wh
            else:
                charged += -wh
            distance_m += ds
            elevation += dh
            t += tick_s
            points.append(Sample(
                time_s=t, lat=lat0 + distance_m * degree_per_m, lon=lon0,
                elevation_m=elevation, discharge_wh=discharge, charged_wh=charged))
    return points


verify.section("Fenster bilden")

# Enough variation in speed *and* gradient, otherwise the parameters cannot
# be separated - exactly what the condition-number check claims, and here it
# is taken at its word.
speeds = [130, 90, 60, 110, 75, 130, 100, 45, 120, 85, 65, 135, 95, 55,
         125, 105, 70, 115, 80, 140, 50, 100, 90, 60]
gradients = [0, 2, -2, 1, -1, 0, 3, -3, 0, 1.5, -1.5, 0.5, 2.5, -2.5,
              -0.5, 1, -1, 0, 2, -2, 3, -3, 0.5, -0.5]
points = trip_build(speeds, gradients)

# Smoothing off: the synthetic elevation is exact, and smoothing would
# shrink the climbs - exactly the effect the module warns about.
timeframe = ident.build_timeframe(points, MASS, temp_c=TEMP, smoothing_m=0.0)
verify(len(timeframe) >= 20, f"aus {len(points)} Punkten entstehen genug Fenster",
       f"nur {len(timeframe)}")
verify(all(f.distance_m >= ident.TIMEFRAME_M for f in timeframe),
       "jedes Fenster reisst die Mindestlänge")
verify(all(f.duration_s >= ident.TIMEFRAME_S for f in timeframe),
       "jedes Fenster reisst die Mindestdauer")

# The windows must cover the bulk of the trip. What is left over at the
# edges is the remainder that no longer fills the minimum length - there must
# not be much more than one window per segment.
total_m = ident.haversine_m(points[0].lat, points[0].lon,
                             points[-1].lat, points[-1].lon)
covered = sum(f.distance_m for f in timeframe)
verify(covered > 0.8 * total_m,
       f"die Fenster decken {100*covered/total_m:.0f} % der Strecke ab",
       f"nur {100*covered/total_m:.0f} %")

verify.section("Parameter zurückgewinnen")

res = ident.identifizieren(timeframe, MASS, lam=0.0)
verify(res.r2 > 0.999, f"R² nahe 1 bei rauschfreien Daten (R²={res.r2:.4f})",
       f"R²={res.r2:.4f}")
verify(res.condition_number < 100,
       f"Kondition brauchbar ({res.condition_number:.0f})", f"{res.condition_number:.0f}")

for name, truthy, actual in (
        ("F_roll", TRUTHY["f_roll"], res.f_roll_n),
        ("c_w·A", TRUTHY["cw_a"], res.cw_a_m2),
        ("1/eta", TRUTHY["uphill"], res.uphill_factor),
        ("eta_rek", TRUTHY["downhill"], res.eta_regen),
        ("P_neben", TRUTHY["p_neben"], res.p_aux_w)):
    rel = abs(actual - truthy) / abs(truthy)
    verify(rel < 0.05, f"{name} auf 5 % genau zurückgewonnen "
                       f"({actual:.3f} gegen {truthy:.3f})",
           f"{actual:.3f} statt {truthy:.3f}, {100*rel:.1f} % daneben")

verify.section("Die Verbrauchskurve stimmt mit der Wahrheit überein")

for v, climb in ((130, 0.0), (100, 0.0), (80, 2.0), (110, -1.0)):
    vv = v / 3.6
    rho = ident.air_density(TEMP, 400.0)
    plan_value = (TRUTHY["f_roll"] + TRUTHY["cw_a"] * 0.5 * rho * vv * vv
            + TRUTHY["p_neben"] / vv
            + (TRUTHY["uphill"] if climb >= 0 else TRUTHY["downhill"])
            * MASS * ident.G * climb / 100.0) / 3.6
    actual = ident.consumption_wh_km(res, v, climb, temp_c=TEMP)
    verify(abs(actual - plan_value) < 0.03 * abs(plan_value),
           f"{v} km/h bei {climb:+.0f} %: {actual:.0f} Wh/km wie gerechnet",
           f"{actual:.1f} statt {plan_value:.1f}")

verify.section("Physikalisch Unmögliches wird gemeldet")

# The same trip, but evaluated with too low a mass. Exactly the case that
# occurred on the real trip of 4 Sept.: η_rekup slips above 1 because the
# gradient term is set too small.
to_light = ident.identifizieren(timeframe, MASS * 0.6, lam=0.0)
verify(to_light.eta_regen > 1.0,
       "zu leicht angesetzte Masse treibt η_rekup über 1",
       f"η_rekup={to_light.eta_regen:.2f}")
verify(any("η_rekup" in w for w in to_light.warnings),
       "und das wird als Warnung gemeldet",
       f"Warnungen: {to_light.warnings}")

# A trip without gradient variation: the sum must be right, the split must
# not claim to be.
just = ident.build_timeframe(
    trip_build([120, 118, 122, 119, 121] * 5, [0.0] * 25),
    MASS, temp_c=TEMP, smoothing_m=0.0)
res_just = ident.identifizieren(just, MASS)
verify(bool(res_just.warnings),
       "eine Fahrt ohne Streuung meldet, dass sie nichts trennen kann",
       "keine Warnung trotz fehlender Streuung")

verify.section("Lücken und Ladepausen fallen heraus")

with_gap = list(points)
for p in with_gap[120:]:
    p.time_s += 3600.0          # an hour of pause right in the middle
f_gap = ident.build_timeframe(with_gap, MASS, temp_c=TEMP, smoothing_m=0.0)
verify(all(not (f.from_s < points[120].time_s < f.until_s) for f in f_gap),
       "kein Fenster spannt über die Lücke hinweg")

sys.exit(verify.balance(
    "Nicht geprüft: echte Höhendaten, echte GPS-Streuung und die Frage, ob "
    "die Fenstergrössen für eine reale Fahrt gut gewählt sind."))
