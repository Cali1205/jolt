#!/usr/bin/env python3
"""Den Verbrauch einer gefahrenen Sitzung nach Tempo und Steigung auswerten.

    docker exec jolt-app python3 tools/consumption_analysis.py [sitzung_id]

Ohne Angabe wird die jüngste Sitzung genommen.

**Wozu das neben `calibration.py` steht.** Die Kalibrierung lernt einen
Korrekturfaktor - eine Zahl, die die Verbrauchskurve verschiebt. Dieses
Werkzeug beantwortet die andere Frage: *Stimmt die Form der Kurve?* Ein
Modell, das bei 90 km/h passt und bei 130 um ein Viertel danebenliegt, lässt
sich durch keinen Faktor retten, und genau daran hängt jeder Ladeplan auf der
Autobahn.

Es schreibt nichts. Was es findet, ist ein Befund und keine Einstellung -
ob daraus ein geänderter c_w-Wert im Fahrzeugprofil wird, entscheidet ein
Mensch, der die Warnungen gelesen hat.

**Die Höhe kommt aus Kartendaten**, nicht aus dem GPS. Dessen Höhenangabe
streut um zehn bis zwanzig Meter; wer solche Differenzen aufsummiert, findet
für eine Fahrt durch die Ebene mehrere hundert Höhenmeter und schreibt sie
dem Steigungsterm zu. Dieselbe Begründung wie in live/recording.py.
"""
import os
import sys

# Suchpfad wie in examine.py - aber *ohne* dessen Wegwerf-Datenbank und ohne
# das Löschen von ORS_API_KEY: Dieses Werkzeug soll die echte Datenbank
# lesen und echte Höhen holen.
_here = os.path.dirname(os.path.abspath(__file__))
for _candidate in (os.path.join(_here, "..", "backend"), os.path.join(_here, "..")):
    if os.path.isdir(os.path.join(_candidate, "app")):
        if _candidate not in sys.path:
            sys.path.insert(0, _candidate)
        break

from sqlalchemy import select                        # noqa: E402

from app import models, routing                      # noqa: E402
from app.database import SessionLocal                # noqa: E402
from app.energy import identification as ident      # noqa: E402
from app.energy.identification import Sample     # noqa: E402
from app.energy.model import VehicleValues, G      # noqa: E402

TEMP_C = 15.0


def fetch_elevations(points):
    """Geländehöhe je Messpunkt - Karte zuerst, GPS als Rückfall."""
    geometry = [[p.lon, p.lat] for p in points]
    try:
        with_elevation = routing.provider().elevations(geometry)
    except Exception as failure:                      # noqa: BLE001
        print(f"  Höhenabfrage fehlgeschlagen: {failure}")
        with_elevation = None
    if with_elevation and len(with_elevation) == len(points):
        print(f"  Höhe aus Kartendaten für {len(points)} Punkte.")
        return [float(p[2]) for p in with_elevation]

    gps = [(p.raw_values or {}).get("elevation_m") for p in points]
    if any(h is not None for h in gps):
        print("  ACHTUNG: keine Kartenhöhe - GPS-Höhe als Rückfall. Der "
              "Steigungsterm ist damit nur grob.")
        tail = 0.0
        origin_of = []
        for h in gps:
            tail = float(h) if h is not None else tail
            origin_of.append(tail)
        return origin_of
    print("  ACHTUNG: gar keine Höhendaten - es wird eben gerechnet. Alles, "
          "was die Steigung gekostet hat, landet in den übrigen Parametern.")
    return [0.0] * len(points)


def session_charging(db, session_id):
    if session_id:
        s = db.get(models.LiveSession, session_id)
        if not s:
            raise SystemExit(f"Sitzung {session_id} gibt es nicht.")
        return s
    s = db.execute(select(models.LiveSession)
                   .order_by(models.LiveSession.id.desc())
                   .limit(1)).scalars().first()
    if not s:
        raise SystemExit("Keine Sitzung in der Datenbank.")
    return s


def samples(points, elevations):
    """LivePunkt -> Messpunkt. Die Zähler stehen in `rohwerte` in kWh."""
    t0 = points[0].timestamp
    origin_of = []
    for p, h in zip(points, elevations):
        raw = p.raw_values if isinstance(p.raw_values, dict) else {}
        ent, chg = raw.get("discharge_kwh"), raw.get("charged_kwh")
        origin_of.append(Sample(
            time_s=(p.timestamp - t0).total_seconds(),
            lat=p.lat, lon=p.lon, elevation_m=h,
            discharge_wh=None if ent is None else float(ent) * 1000.0,
            charged_wh=None if chg is None else float(chg) * 1000.0))
    return origin_of


def model_wh_km(vals, speed_kmh, gradient_pct=0.0):
    """Was `model.py` heute für dieselbe Lage vorhersagt."""
    from app.energy.model import hvac_power_w, air_density
    v = speed_kmh / 3.6
    rho = air_density(TEMP_C, 400.0)
    force = (vals.c_rr * vals.mass_kg * G
             + 0.5 * rho * vals.c_w * vals.frontal_area_m2 * v * v)
    force /= vals.eta_drive
    slope = gradient_pct / 100.0
    climb = vals.mass_kg * G * slope
    force += climb / vals.eta_drive if slope >= 0 else climb * vals.eta_regen
    force += (vals.p_aux_w
              + hvac_power_w(TEMP_C, vals.heat_pump)) / v
    return force / 3.6


def main():
    session_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    db = SessionLocal()
    try:
        session = session_charging(db, session_id)
        vals = VehicleValues.from_trip(session.trip)
        points = [p for p in session.points if p.timestamp and p.lat and p.lon]
        print(f"=== Sitzung {session.id} (Fahrt {session.trip_id}), "
              f"{len(points)} Messpunkte ===")
        print(f"  Fahrzeug: {session.trip.vehicle.name}, "
              f"{vals.mass_kg:.0f} kg, c_w·A = "
              f"{vals.c_w * vals.frontal_area_m2:.3f} m²")
        if len(points) < 50:
            raise SystemExit("  Zu wenige Messpunkte für eine Auswertung.")

        elevations = fetch_elevations(points)
        mp = samples(points, elevations)
        timeframe = ident.build_timeframe(mp, vals.mass_kg, temp_c=TEMP_C)
        if len(timeframe) < 10:
            raise SystemExit(
                f"  Nur {len(timeframe)} auswertbare Fenster - zu wenig. "
                "Meist liegt es an Lücken oder fehlenden Fahrzeugdaten.")

        km = sum(f.distance_m for f in timeframe) / 1000.0
        print(f"\n=== {len(timeframe)} Fenster, {km:.0f} km auswertbar "
              f"({100*km/max(0.001, _driven_km(points)):.0f} % der Fahrt) ===")

        res = ident.identifizieren(timeframe, vals.mass_kg)
        print(f"  R² = {res.r2:.3f}   Konditionszahl {res.condition_number:.0f}\n")
        # Verglichen werden *wirksame* Werte, also mit eingerechnetem
        # Wirkungsgrad - anders ist die Messung an der Batterie nicht zu
        # deuten. Deshalb steht in der Modellspalte überall das /eta.
        print(f"  {'Anteil (wirksam)':20s} {'gemessen':>20s} {'im Modell':>11s}")
        for title, val, failure, preset in (
                ("Rollwiderstand [N]", res.f_roll_n, res.failure["f_roll"],
                 vals.c_rr * vals.mass_kg * G / vals.eta_drive),
                ("Luft c_w·A/eta [m²]", res.cw_a_m2, res.failure["cw_a"],
                 vals.c_w * vals.frontal_area_m2 / vals.eta_drive),
                ("Bergauf 1/eta", res.uphill_factor, res.failure["uphill"],
                 1 / vals.eta_drive),
                ("Bergab eta_rekup", res.eta_regen, res.failure["downhill"],
                 vals.eta_regen),
                ("Nebenverbraucher [W]", res.p_aux_w, res.failure["p_neben"],
                 vals.p_aux_w),
                ("Beschleunigen", res.accel_share, res.failure["beschl"],
                 0.0)):
            print(f"  {title:20s} {val:11.3f} ±{failure:7.3f} {preset:11.3f}")

        if res.warnings:
            print("\n  Was diese Fahrt *nicht* hergibt:")
            for w in res.warnings:
                print(f"    - {w}")

        print("\n=== Verbrauch nach Tempo (eben) ===")
        print(f"  {'Tempo':>8s} {'gemessen':>10s} {'Modell':>9s} {'Abw.':>7s}")
        for v in (60, 80, 100, 110, 120, 130):
            a = ident.consumption_wh_km(res, v, temp_c=TEMP_C)
            b = model_wh_km(vals, v)
            print(f"  {v:5d} km/h {a:8.0f} {b:9.0f} {100*(a/b-1):+6.0f} %")

        print("\n=== Verbrauch nach Steigung (bei 110 km/h) ===")
        print(f"  {'Steigung':>9s} {'gemessen':>10s} {'Modell':>9s}")
        for s in (-3, -2, -1, 0, 1, 2, 3):
            print(f"  {s:+7d} % {ident.consumption_wh_km(res, 110, s, TEMP_C):8.0f} "
                  f"{model_wh_km(vals, 110, s):9.0f}")

        print("\n=== Was Langsamerfahren bringt (eben, gemessene Kurve) ===")
        basis = ident.consumption_wh_km(res, 130, temp_c=TEMP_C)
        for destination in (120, 110, 100, 90):
            w = ident.consumption_wh_km(res, destination, temp_c=TEMP_C)
            timestamp = (100.0 / destination - 100.0 / 130) * 60.0
            print(f"  130 -> {destination:3d} km/h: {100*(w/basis-1):+5.0f} % Verbrauch, "
                  f"{100*(basis/w-1):+5.0f} % Reichweite, "
                  f"{timestamp:+4.0f} min je 100 km")

        print("\n=== Gemessene Fenster nach Tempoband ===")
        print("  (roh, also mit der Steigung darin - der Vergleich zur "
              "bereinigten\n   Kurve oben zeigt, wie stark das Profil "
              "hineinspielt)")
        bins: dict = {}
        for f in timeframe:
            bins.setdefault(int(f.speed_kmh // 20) * 20, []).append(f)
        for lo in sorted(bins):
            g = bins[lo]
            distance = sum(f.distance_m for f in g)
            print(f"  {lo:3d}-{lo+19:3d} km/h  {len(g):3d} Fenster "
                  f"{distance/1000:6.1f} km  "
                  f"{sum(f.energy_wh for f in g)/(distance/1000):6.0f} Wh/km  "
                  f"(Schnittsteigung {100*sum(f.net_elevation_m for f in g)/distance:+.2f} %)")
    finally:
        db.close()


def _driven_km(points):
    from app.geo import haversine_m
    return sum(haversine_m(a.lat, a.lon, b.lat, b.lon)
               for a, b in zip(points, points[1:])) / 1000.0


if __name__ == "__main__":
    main()
