#!/usr/bin/env python3
"""Checks the consumption model against cases whose result is known beforehand.

No test framework, no network connection, no database - `python3
tools/check_model.py` is enough. It does not check exact numbers (those
depend on parameters that are allowed to change) but the ratios that must
hold physically. Violating exactly these is what leaves you standing at the
wrong charger on the road.

    ./tools/check_model.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from examine import Check, application_provide  # noqa: E402

# This script does not touch a database - it only computes physics.
application_provide("modell", db_name=False)

from app.energy.model import (VehicleValues, Environment, hvac_power_w,  # noqa: E402
                                air_density, compute_profile)
from app.charging.curves import charge_time_minutes, power_at  # noqa: E402

verify = Check()


def current_distance(km: float, elevation_end_m: float = 0.0, points_per_km: int = 1):
    """A synthetic route: straight north, rising linearly.

    One degree of latitude is about 111.32 km - that lets us construct a
    distance of any length exactly, without needing a routing service.
    """
    count = max(2, int(km * points_per_km))
    points = []
    for i in range(count + 1):
        t = i / count
        points.append([10.0, 50.0 + (km * t) / 111.32, elevation_end_m * t])
    return points, [0.0] * count


def pass_distance(km: float, summit_m: float, points_per_km: int = 1):
    """Up and back down: same elevation at the start and at the end."""
    count = max(2, int(km * points_per_km))
    if count % 2:
        count += 1
    points = []
    for i in range(count + 1):
        t = i / count
        elevation = summit_m * (2 * t if t <= 0.5 else 2 * (1 - t))
        points.append([10.0, 50.0 + (km * t) / 111.32, elevation])
    return points, [0.0] * count


def velocity(kmh: float, count: int):
    return [kmh / 3.6] * count


def cycle(fz, km, kmh, elevation=0.0, temp_c=20.0, start_soc=100.0, wind_ms=0.0,
         wind_degree=0.0):
    points, _ = current_distance(km, elevation)
    environment = Environment(temp_c=temp_c, wind_speed_ms=wind_ms,
                        wind_direction_degree=wind_degree)
    return compute_profile(fz, points, velocity(kmh, len(points) - 1), start_soc,
                          lambda lat, lon: environment)


def main() -> int:
    fz = VehicleValues()      # 1950 kg, c_w 0.28, 2.3 m², 60 kWh net

    verify.section("Luftdichte")
    verify(abs(air_density(15.0, 0.0) - 1.225) < 0.01,
           "bei 15 °C auf Meereshöhe rund 1,225 kg/m³",
           f"ist {air_density(15.0, 0.0):.3f}")
    verify(air_density(-5.0, 0.0) > air_density(20.0, 0.0) * 1.07,
           "kalte Luft ist mindestens 7 % dichter als warme")
    verify(air_density(15.0, 1500.0) < air_density(15.0, 0.0) * 0.87,
           "auf 1500 m mindestens 13 % dünner")

    print("\nHeizung und Klima")
    verify(hvac_power_w(20.0) < 300.0, "bei 20 °C fast nichts")
    verify(2000.0 < hvac_power_w(-5.0, heat_pump=True) < 4500.0,
           "bei -5 °C mit Wärmepumpe zwischen 2 und 4,5 kW",
           f"ist {hvac_power_w(-5.0, True):.0f} W")
    verify(hvac_power_w(-5.0, False) > hvac_power_w(-5.0, True) * 1.5,
           "ohne Wärmepumpe deutlich mehr")

    print("\nGrundfall: 100 km eben, 20 °C, 120 km/h")
    just = cycle(fz, 100.0, 120.0)
    verify(15.0 < just.consumption_kwh_100km < 26.0,
           "Verbrauch im plausiblen Bereich 15-26 kWh/100 km",
           f"ist {just.consumption_kwh_100km}")
    verify(abs(just.distance_km - 100.0) < 1.0,
           "Streckenlänge stimmt", f"ist {just.distance_km}")
    verify(abs(just.mins - 50.0) < 1.0,
           "Fahrzeit rund 50 Minuten", f"ist {just.mins}")

    print("\nTempo geht quadratisch ein")
    slow = cycle(fz, 100.0, 110.0)
    fast = cycle(fz, 100.0, 130.0)
    verify(fast.kwh_total > slow.kwh_total * 1.10,
           "130 km/h braucht über 10 % mehr als 110 km/h",
           f"{slow.kwh_total} -> {fast.kwh_total} kWh")
    # With pure air drag it would be (130/110)^2 = 1.40. Rolling resistance and
    # auxiliary load dampen that, so the increase must lie below it.
    verify(fast.kwh_total < slow.kwh_total * 1.40,
           "aber weniger als der reine v²-Faktor von 1,40")

    print("\nSteigung")
    mountain = cycle(fz, 100.0, 120.0, elevation=800.0)
    swing_kwh = fz.mass_kg * 9.80665 * 800.0 / 3.6e6 / fz.eta_drive
    gain = mountain.kwh_total - just.kwh_total
    verify(gain > 0, "800 Höhenmeter kosten Energie",
           f"+{gain:.2f} kWh")
    verify(abs(gain - swing_kwh) < swing_kwh * 0.25,
           "und zwar ungefähr die Hubarbeit geteilt durch den Wirkungsgrad",
           f"erwartet ~{swing_kwh:.2f} kWh, gemessen {gain:.2f} kWh")

    print("\nPass: hinauf und wieder hinunter")
    up = cycle(fz, 50.0, 100.0, elevation=600.0)
    down = cycle(fz, 50.0, 100.0, elevation=-600.0)
    verify(down.kwh_total < up.kwh_total,
           "bergab weniger als bergauf")

    # 1200 m over 25 km is just under 5 % grade. Only at this order of magnitude
    # does the downhill force exceed the driving resistance, so that there is
    # anything to recuperate going downhill at all. On gentle slopes the car keeps
    # pulling - there a pass really is a zero-sum game, and the model rightly says
    # so.
    pass_points, _ = pass_distance(50.0, 1200.0)
    environment = Environment(temp_c=20.0)
    over_the_pass = compute_profile(fz, pass_points,
                                    velocity(70.0, len(pass_points) - 1), 100.0,
                                    lambda lat, lon: environment)
    through_the_level = cycle(fz, 50.0, 70.0)
    verify(over_the_pass.kwh_total > through_the_level.kwh_total * 1.2,
           "über den Pass kostet über 20 % mehr als die Ebene, obwohl man "
           "wieder auf Ausgangshöhe ankommt - Rekuperation holt nur ~70 % zurück",
           f"{over_the_pass.kwh_total:.2f} kWh gegen "
           f"{through_the_level.kwh_total:.2f} kWh")
    verify(abs(over_the_pass.points[-1].elevation_m) < 1.0,
           "und der Pass endet wirklich wieder auf null Metern")

    print("\nKälte")
    warm = cycle(fz, 200.0, 120.0, temp_c=20.0)
    cold = cycle(fz, 200.0, 120.0, temp_c=-5.0)
    verify(cold.kwh_total > warm.kwh_total * 1.12,
           "bei -5 °C mindestens 12 % mehr als bei 20 °C",
           f"{warm.kwh_total} -> {cold.kwh_total} kWh")

    print("\nWind")
    against = cycle(fz, 100.0, 120.0, wind_ms=10.0, wind_degree=0.0)    # from the north
    back = cycle(fz, 100.0, 120.0, wind_ms=10.0, wind_degree=180.0)
    verify(against.kwh_total > just.kwh_total > back.kwh_total,
           "Gegenwind kostet, Rückenwind spart - die Route führt nach Norden",
           f"{against.kwh_total} / {just.kwh_total} / {back.kwh_total} kWh")

    print("\nReserve-Marke")
    far = cycle(fz, 500.0, 120.0, start_soc=80.0)
    verify(far.reserve_at_km is not None,
           "60-kWh-Auto schafft 500 km bei 80 % nicht ohne Nachladen")
    expected_km = (80.0 - fz.reserve_soc) / 100.0 * fz.battery_net_kwh \
        / just.consumption_kwh_100km * 100.0
    verify(abs((far.reserve_at_km or 0) - expected_km) < expected_km * 0.15,
           "Reserve wird ungefähr dort erreicht, wo die Überschlagsrechnung sagt",
           f"erwartet ~{expected_km:.0f} km, gerechnet {far.reserve_at_km} km")

    short = cycle(fz, 80.0, 120.0, start_soc=80.0)
    verify(short.reserve_at_km is None,
           "80 km bei 80 % greifen die Reserve nicht an")

    print("\nAnhänger: eigene Masse, eigene Luftwiderstandsfläche")
    rig = VehicleValues(**{**fz.__dict__, "mass_kg": fz.mass_kg + 1300.0,
                               "cwa_extra_m2": 1.1})
    with_trailer = cycle(rig, 100.0, 100.0)
    without_trailer = cycle(fz, 100.0, 100.0)
    verify(with_trailer.kwh_total > without_trailer.kwh_total * 1.30,
           "ein Wohnwagen kostet bei 100 km/h mindestens ein Drittel mehr",
           f"{without_trailer.kwh_total} -> {with_trailer.kwh_total} kWh")
    only_mass = VehicleValues(**{**fz.__dict__, "mass_kg": fz.mass_kg + 1300.0})
    only_area = VehicleValues(**{**fz.__dict__, "cwa_extra_m2": 1.1})
    verify(cycle(only_area, 100.0, 130.0).kwh_total
           - cycle(only_area, 100.0, 100.0).kwh_total
           > (cycle(only_mass, 100.0, 130.0).kwh_total
              - cycle(only_mass, 100.0, 100.0).kwh_total) * 1.5,
           "die Fläche wirkt mit v², die Masse nicht - schneller fahren "
           "bestraft den Luftwiderstand des Anhängers, nicht sein Gewicht")
    mountain_without = cycle(fz, 100.0, 100.0, elevation=600.0)
    mountain_with = cycle(only_mass, 100.0, 100.0, elevation=600.0)
    verify(mountain_with.kwh_total - cycle(only_mass, 100.0, 100.0).kwh_total
           > (mountain_without.kwh_total - without_trailer.kwh_total) * 1.4,
           "und umgekehrt: Die Masse kostet am Berg, die Fläche kaum")

    class FzStub:
        curb_mass_kg, payload_kg, mass_kg = 1800.0, 150.0, 1950.0
        c_w, frontal_area_m2, c_rr = 0.28, 2.3, 0.010
        eta_drive, eta_regen, p_aux_w = 0.88, 0.70, 350.0
        heat_pump, battery_net_kwh, reserve_soc = True, 60.0, 10.0
        correction_factor = 1.0
        max_speed_kmh = None

    class TripStub:
        def __init__(self, **kw):
            self.vehicle = FzStub()
            self.payload_kg = None
            self.air_drag_factor = 1.0
            self.__dict__.update(kw)

    w = VehicleValues.from_trip(TripStub(trailer_kg=1300.0,
                                          trailer_cwa_m2=1.1))
    verify(abs(w.mass_kg - 3250.0) < 1e-9 and abs(w.cwa_extra_m2 - 1.1) < 1e-9,
           "from_trip addiert die Masse des Anhängers und merkt sich seine Fläche",
           f"{w.mass_kg} kg, {w.cwa_extra_m2} m²")
    verify(abs(w.c_w - 0.28) < 1e-9,
           "der cw-Wert des Autos bleibt unberührt - der Anhänger ist keine "
           "Verbiegung des Fahrzeugs")
    verify(VehicleValues.from_trip(TripStub()).cwa_extra_m2 == 0.0,
           "ohne Anhänger ändert sich nichts")

    print("\nHöchstgeschwindigkeit")
    capped = VehicleValues(**{**fz.__dict__, "speed_max_ms": 100.0 / 3.6})
    a = cycle(capped, 100.0, 130.0)
    b = cycle(fz, 100.0, 100.0)
    verify(abs(a.kwh_total - b.kwh_total) < 0.05,
           "bei 130 km/h Annahme und 100 km/h Grenze wird mit 100 gerechnet",
           f"{a.kwh_total} gegen {b.kwh_total} kWh")
    verify(all(p.speed_kmh <= 100.1 for p in a.points),
           "und das Profil führt nirgends mehr als 100 km/h")
    verify(abs(a.mins - b.mins) < 0.2,
           "die Fahrzeit folgt der Grenze", f"{a.mins} gegen {b.mins} min")
    verify(a.minutes_without_cap < a.mins * 0.80,
           "ohne Grenze wäre sie deutlich kürzer - daraus lässt sich ablesen, "
           "wie sehr die Grenze die Fahrt verlängert",
           f"{a.minutes_without_cap} gegen {a.mins} min")
    under = cycle(capped, 100.0, 80.0)
    verify(abs(under.kwh_total - cycle(fz, 100.0, 80.0).kwh_total) < 0.01,
           "wer ohnehin langsamer fährt, merkt von der Grenze nichts")
    free_points, _ = current_distance(100.0, 0.0)
    free = compute_profile(capped, free_points,
                          velocity(130.0, len(free_points) - 1), 100.0,
                          lambda lat, lon: Environment(temp_c=20.0),
                          speed_cap=False)
    verify(abs(free.kwh_total - cycle(fz, 100.0, 130.0).kwh_total) < 0.05,
           "mit speed_cap=False bleibt gefahrenes Tempo gefahrenes Tempo - "
           "eine Aufzeichnung wird nicht nachträglich zurechtgestutzt")

    w = VehicleValues.from_trip(TripStub(speed_max_kmh=100.0))
    verify(abs(w.speed_max_ms * 3.6 - 100.0) < 1e-6,
           "die Grenze einer Fahrt kommt in den Fahrzeugwerten an")
    FzStub.max_speed_kmh = 145.0
    w = VehicleValues.from_trip(TripStub())
    verify(abs(w.speed_max_ms * 3.6 - 145.0) < 1e-6,
           "die Grenze des Fahrzeugs gilt auch ohne Grenze der Fahrt")
    w = VehicleValues.from_trip(TripStub(speed_max_kmh=100.0))
    verify(abs(w.speed_max_ms * 3.6 - 100.0) < 1e-6,
           "sind beide gesetzt, gilt die kleinere (Gespann am Auto mit 145)")
    w = VehicleValues.from_trip(TripStub(speed_max_kmh=180.0))
    verify(abs(w.speed_max_ms * 3.6 - 145.0) < 1e-6,
           "und auch umgekehrt: Die Fahrt kann das Auto nicht schneller machen")
    FzStub.max_speed_kmh = None

    print("\nLadekurve")
    curve = [(0, 110), (10, 120), (30, 120), (50, 90), (70, 60), (80, 45),
             (90, 28), (100, 8)]
    verify(abs(power_at(curve, 20.0) - 120.0) < 1.0,
           "bei 20 % SoC volle 120 kW", f"ist {power_at(curve, 20.0):.1f}")
    verify(abs(power_at(curve, 60.0) - 75.0) < 1.0,
           "bei 60 % interpoliert auf 75 kW",
           f"ist {power_at(curve, 60.0):.1f}")
    verify(power_at(curve, 20.0, max_charger_kw=50.0) == 50.0,
           "eine 50-kW-Säule begrenzt die Kurve")

    bottom = charge_time_minutes(curve, 10.0, 55.0, 60.0)
    upper = charge_time_minutes(curve, 55.0, 100.0, 60.0)
    verify(upper > bottom * 2,
           "die oberen 45 Prozentpunkte dauern über doppelt so lang wie die "
           "unteren - der Grund für zwei kurze statt einem langen Stopp",
           f"{bottom:.0f} min gegen {upper:.0f} min")
    verify(charge_time_minutes(curve, 50.0, 50.0, 60.0) == 0.0,
           "kein Ladehub, keine Zeit")

    return verify.balance()


if __name__ == "__main__":
    sys.exit(main())
