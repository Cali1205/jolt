#!/usr/bin/env python3
"""Checks the measured charging curve and how the planner picks its curve.

The fixture is a van whose DC charges follow a known curve (the shape a real
ID.Buzz showed over fourteen fast charges): a dozen charges at fast chargers
with some scatter, two at a 50 kW charger, two at a wall box. From their
power tracks the envelope has to come back out, the planner has to charge
with it, and with too little data or the setting "profile" it must not.

No network, no Postgres:

    ./tools/check_measured_curve.py
"""
import os
import random
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from examine import Check, application_provide  # noqa: E402

application_provide("kurve")

from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402
from app.charging import curve_source, curves, measured  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.energy.model import haversine_m  # noqa: E402
from app.main import app  # noqa: E402

import requests  # noqa: E402
from app.energy import weather  # noqa: E402


def _no_network(*args, **kwargs):
    raise requests.ConnectionError("network disabled in this check")


weather.requests.get = _no_network

verify = Check()

TRUE_CURVE = [(5, 175), (10, 182), (15, 181), (20, 184), (25, 173), (30, 146),
              (35, 133), (40, 122), (45, 111), (50, 103), (55, 97), (60, 91),
              (65, 89), (70, 88), (75, 88), (80, 86), (85, 70), (90, 50),
              (95, 30), (100, 10)]
KWH_PER_PCT = 0.764
BUZZ = next(t for t in curves.TEMPLATES if "Buzz" in t["name"])
T0 = datetime(2026, 3, 1, 8, 0)


def simulate(soc_start, soc_end, cap_kw, seed, start=T0, sample_s=60):
    """A charge's power track as the VW history stores it, and the true
    charge level at each sample. The car is simulated second by second; the
    power scatters from minute to minute and ramps up over the first one."""
    rng = random.Random(seed)
    soc, t, track, true_soc = soc_start, start, [], []
    second, scatter = 0, 1.0
    while soc < soc_end:
        if second % sample_s == 0:
            scatter = rng.uniform(0.93, 1.0)
        ramp = min(1.0, 0.4 + 0.6 * second / sample_s)
        kw = min(curves.power_at(TRUE_CURVE, soc) * scatter * ramp, cap_kw)
        if second % sample_s == 0:
            track.append([t.isoformat(), round(kw, 1)])
            true_soc.append(soc)
        soc += kw / 3600 / KWH_PER_PCT
        t += timedelta(seconds=1)
        second += 1
    track.append([t.isoformat(), round(kw, 1)])
    true_soc.append(soc)
    return track, true_soc


def charge(soc_start, soc_end, cap_kw=400.0, seed=0, charge_type="DC",
           start=T0, source="vw_history", sample_s=60):
    track, true_soc = simulate(soc_start, soc_end, cap_kw, seed, start, sample_s)
    record = {"started_at": track[0][0], "ended_at": track[-1][0],
              "power_track": track}
    return SimpleNamespace(
        soc_start=soc_start, soc_end=true_soc[-1], true_soc=true_soc,
        charge_type=charge_type,
        started_at=datetime.fromisoformat(track[0][0]),
        ended_at=datetime.fromisoformat(track[-1][0]),
        energy_kwh=round((soc_end - soc_start) * KWH_PER_PCT, 1),
        max_power_kw=max(kw for _, kw in track),
        details={"by_source": {source: [record]}})


def fleet():
    """Twelve fast DC charges, two at a 50 kW charger, two on AC."""
    rng = random.Random(7)
    found = []
    for i in range(12):
        found.append(charge(rng.uniform(5, 20), rng.uniform(60, 84),
                            seed=i, start=T0 + timedelta(days=i)))
    found.append(charge(15, 70, cap_kw=50.0, seed=20,
                        start=T0 + timedelta(days=20)))
    found.append(charge(30, 80, cap_kw=50.0, seed=21,
                        start=T0 + timedelta(days=21)))
    found.append(charge(20, 80, cap_kw=11.0, seed=22, charge_type="AC",
                        start=T0 + timedelta(days=22)))
    found.append(charge(40, 90, cap_kw=11.0, seed=23, charge_type="AC",
                        start=T0 + timedelta(days=23)))
    return found


def dc_points(charges):
    return [measured.charge_points(c) for c in charges if c.charge_type == "DC"]


# ---------------------------------------------------------------------------

def part_charge_level():
    verify.section("Ladestand während des Ladens, aus der Energie geschätzt")
    track = [[(T0 + timedelta(minutes=m)).isoformat(), 100.0] for m in range(61)]
    flat = SimpleNamespace(soc_start=20.0, soc_end=80.0,
                           details={"by_source": {"vw_history": [{"power_track": track}]}})
    points = measured.charge_points(flat)
    verify(len(points) == 61 and points[0][0] == 20.0 and abs(points[-1][0] - 80.0) < 1e-9,
           "beginnt beim Start- und endet beim End-Ladestand", str(points[:1] + points[-1:]))
    verify(abs(points[30][0] - 50.0) < 1e-9,
           "bei gleicher Leistung nach der halben Zeit genau in der Mitte",
           f"{points[30][0]:.3f}")

    gap = [[T0.isoformat(), 100.0], [(T0 + timedelta(hours=1)).isoformat(), 100.0],
           [(T0 + timedelta(hours=1, minutes=5)).isoformat(), 100.0]]
    gapped = SimpleNamespace(soc_start=0.0, soc_end=30.0,
                             details={"by_source": {"vw_history": [{"power_track": gap}]}})
    verify(abs(measured.charge_points(gapped)[1][0] - 20.0) < 1e-9,
           "eine Stunde Funkstille zählt wie zehn Minuten",
           f"{measured.charge_points(gapped)[1][0]:.2f}")

    simulated = charge(10, 80, seed=3)
    estimated = [s for s, _ in measured.charge_points(simulated)]
    worst = max(abs(a - b) for a, b in zip(simulated.true_soc, estimated))
    verify(worst < 1.0, "auf einer simulierten Ladung höchstens 1 % neben dem echten Ladestand",
           f"{worst:.2f} %")

    both = charge(10, 50, seed=4)
    portal = [[t, kw / 2] for t, kw in both.details["by_source"]["vw_history"][0]["power_track"]]
    both.details["by_source"]["vw_portal"] = [{"power_track": portal}]
    verify(measured.charge_points(both)[5][1]
           == both.details["by_source"]["vw_history"][0]["power_track"][5][1],
           "der Verlauf aus dem VW-Export geht dem aus dem Portal vor")
    verify(measured.charge_points(SimpleNamespace(soc_start=50.0, soc_end=50.0,
                                                  details={})) == [],
           "ohne Ladestandsgewinn gibt es keine Punkte")


def part_envelope():
    verify.section("Hüllkurve über die DC-Ladungen")
    curve = measured.envelope(dc_points(fleet()))
    verify(curve is not None, "aus zwölf schnellen und zwei langsamen DC-Ladungen entsteht eine Kurve")
    if curve is None:
        return
    off = [(soc, kw, curves.power_at(TRUE_CURVE, soc)) for soc, kw in curve
           if 10 <= soc <= 80
           and abs(kw - curves.power_at(TRUE_CURVE, soc)) > 0.1 * curves.power_at(TRUE_CURVE, soc)]
    verify(not off, "zwischen 10 und 80 % höchstens 10 % neben der echten Kurve", str(off))
    socs = [soc for soc, _ in curve]
    verify(socs[0] <= 10 and socs[-1] >= 80, "und sie reicht von 10 bis 80 %", str(socs))
    verify(all(kw > 60 for soc, kw in curve if soc <= 80),
           "die Ladungen an 50 kW und an der Wallbox drücken sie nicht herunter",
           str(curve))

    lonely = dc_points(fleet()) + [measured.charge_points(charge(80, 97, seed=30))]
    top = max(soc for soc, _ in measured.envelope(lonely))
    verify(top < 90, "ein Bereich, den nur eine einzige Ladung erreicht hat, bleibt leer",
           f"höchster Punkt {top} %")

    dense = [measured.charge_points(charge(5, 85, seed=50 + i, sample_s=10))
             for i in range(2)]
    verify(measured.envelope(dense) is None,
           "zwei DC-Ladungen sind zu wenig, auch wenn sie dicht und lang aufgezeichnet sind")
    narrow = [measured.charge_points(charge(55, 80, seed=40 + i)) for i in range(6)]
    verify(measured.envelope(narrow) is None,
           "sechs Ladungen nur zwischen 55 und 80 % decken zu wenig ab")


def part_merge():
    verify.section("Zusammengesetzt: gemessen, sonst Profil")
    profile = BUZZ["charge_curve"]
    measured_part = [(15.0, 181.0), (20.0, 184.0), (50.0, 103.0), (80.0, 86.0)]
    merged = measured.merge(measured_part, profile)
    verify(merged[0][0] == 0 and merged[-1][0] == 100,
           "reicht immer von 0 bis 100 %", str(merged))
    verify((10, 170) in merged and (90, 30) in merged,
           "unter und über dem Gemessenen gilt das Profil", str(merged))
    verify(not any(15 < soc < 80 and (soc, kw) in profile for soc, kw in merged),
           "dazwischen kein Profilpunkt", str(merged))
    verify(measured.merge(None, profile) == sorted(profile),
           "ohne Messung ist es das Profil")
    alone = measured.merge(measured_part, [])
    verify(alone[0] == (0.0, 181.0) and alone[-1] == (100.0, 86.0),
           "ohne Profil wird an den Rändern das Gemessene fortgesetzt", str(alone))


def new_vehicle(db, source="auto") -> models.Vehicle:
    vehicle = models.Vehicle(**{k: v for k, v in BUZZ.items() if k != "charge_curve"},
                             charge_curve_source=source)
    vehicle.name = f"Prüfbus {random.random():.6f}"
    db.add(vehicle)
    db.flush()
    for soc, kw in BUZZ["charge_curve"]:
        vehicle.charge_curve.append(models.ChargeCurvePoint(soc_percent=soc, kw=kw))
    db.flush()
    return vehicle


def store(db, vehicle_id, found):
    for c in found:
        db.add(models.VehicleCharge(
            vehicle_id=vehicle_id, started_at=c.started_at, ended_at=c.ended_at,
            soc_start=c.soc_start, soc_end=c.soc_end, energy_kwh=c.energy_kwh,
            max_power_kw=c.max_power_kw, charge_type=c.charge_type,
            sources=list(c.details["by_source"]), details=c.details))
    db.flush()


def part_resolver():
    verify.section("Welche Kurve die Planung bekommt")
    db = SessionLocal()
    try:
        vehicle = new_vehicle(db)
        found = fleet()
        store(db, vehicle.id, found[:2])
        curve, info = curve_source.effective_curve(db, vehicle)
        verify(info["source"] == "profile" and curve == curves.as_pairs(vehicle.charge_curve),
               "mit zwei DC-Ladungen das Profil", str(info))

        store(db, vehicle.id, found[2:])
        curve, info = curve_source.effective_curve(db, vehicle)
        verify(info["source"] == "measured" and info["charges"] == 14,
               "kommen Ladungen dazu, die gemessene Kurve aus allen vierzehn DC-Ladungen",
               str({k: info[k] for k in ("source", "charges", "bins")}))
        verify(curve[0][0] == 0 and curve[-1][0] == 100 and curve != info["profile"],
               "von 0 bis 100 % und nicht das Profil", str(curve))

        row = (db.query(models.VehicleCharge)
               .filter_by(vehicle_id=vehicle.id, charge_type="DC")
               .order_by(models.VehicleCharge.id.desc()).first())
        row.soc_end = row.soc_end + 5
        db.flush()
        _, again = curve_source.effective_curve(db, vehicle)
        verify(again["measured"] != info["measured"],
               "eine nachträglich geänderte Ladung wird neu eingerechnet")

        vehicle.charge_curve_source = "profile"
        curve, info = curve_source.effective_curve(db, vehicle)
        verify(info["source"] == "profile" and curve == info["profile"]
               and info["measured"],
               "mit der Einstellung „immer Profil“ das Profil, die Messung bleibt sichtbar",
               str(info["source"]))
    finally:
        db.rollback()
        db.close()


def prepare_trip(client, vehicle_id) -> int:
    route = client.post("/api/route", json={
        "vehicle_id": vehicle_id,
        "start": {"lat": 53.5511, "lon": 9.9937, "text": "Hamburg"},
        "destination": {"lat": 48.1351, "lon": 11.5820, "text": "München"},
        "start_soc": 80.0}).json()["variants"][0]
    geo = route["geometry"]
    db = SessionLocal()
    try:
        db.query(models.ChargePoint).delete()
        km, upcoming, i = 0.0, 30.0, 0
        for n in range(1, len(geo)):
            km += haversine_m(geo[n - 1][1], geo[n - 1][0], geo[n][1], geo[n][0]) / 1000.0
            if km < upcoming:
                continue
            db.add(models.ChargePoint(
                source="ocm", foreign_id=f"kurve-{i}", name=f"Lader km {km:.0f}",
                operator="Prüfbetrieb", lat=geo[n][1] + 0.0036, lon=geo[n][0],
                city=f"Ort {i}", country="DE", connectors=[], max_kw=150.0,
                point_count=4, connector_types="CCS"))
            i += 1
            upcoming = km + 30.0
        db.commit()
    finally:
        db.close()
    return route["trip_id"]


def part_planner():
    verify.section("Der Ladeplan rechnet mit der gemessenen Kurve")
    client = TestClient(app)
    payload = {**{k: v for k, v in BUZZ.items() if k != "name"},
               "name": "Prüfbus Planung",
               "charge_curve": [list(p) for p in BUZZ["charge_curve"]]}
    vehicle = client.post("/api/vehicles", json=payload).json()
    verify(vehicle.get("charge_curve_source") == "auto",
           "ein neues Fahrzeug plant mit der gemessenen Kurve, sobald es eine gibt",
           str(vehicle.get("charge_curve_source")))
    bad = client.put(f"/api/vehicles/{vehicle['id']}",
                     json={**payload, "charge_curve_source": "geraten"})
    verify(bad.status_code == 422, "eine unbekannte Einstellung wird abgewiesen",
           str(bad.status_code))
    client.put(f"/api/vehicles/{vehicle['id']}",
               json={**payload, "charge_curve_source": "profile"})
    old_client = client.put(f"/api/vehicles/{vehicle['id']}", json=payload).json()
    verify(old_client["charge_curve_source"] == "profile",
           "ein Client, der die Einstellung nicht kennt, setzt sie beim Speichern nicht zurück",
           old_client["charge_curve_source"])
    client.put(f"/api/vehicles/{vehicle['id']}",
               json={**payload, "charge_curve_source": "auto"})

    empty = client.get(f"/api/vehicles/{vehicle['id']}/charge-curve").json()
    verify(empty["source"] == "profile" and empty["charges"] == 0
           and empty["curve"] == empty["profile"],
           "ohne Ladungen im Fahrtenbuch zeigt der Endpunkt das Profil", str(empty))

    db = SessionLocal()
    try:
        store(db, vehicle["id"], fleet())
        db.commit()
    finally:
        db.close()
    shown = client.get(f"/api/vehicles/{vehicle['id']}/charge-curve").json()
    verify(shown["source"] == "measured" and shown["charges"] == 14 and shown["bins"] >= 10
           and shown["updated"] and shown["updated"].endswith("Z"),
           "mit den Ladungen meldet er die gemessene Kurve, auf wie vielen sie steht und von wann",
           str({k: shown[k] for k in ("source", "charges", "bins", "updated")}))

    def minutes(curve, until, charger_kw):
        return curves.charge_time_minutes(curve, 10, until, 77.0, charger_kw, 170.0)

    for until, charger_kw in ((80, 150.0), (50, 300.0)):
        print(f"        10 → {until} % an {charger_kw:.0f} kW: gemessen "
              f"{minutes(shown['curve'], until, charger_kw)} min, "
              f"Profil {minutes(shown['profile'], until, charger_kw)} min")
    verify(28.0 <= minutes(shown["curve"], 80, 150.0) <= 31.0,
           "10 → 80 % an einer 150-kW-Säule mit der gemessenen Kurve 28 bis 31 Minuten",
           f"{minutes(shown['curve'], 80, 150.0)} min")
    # The template is too fast between 30 and 50 % and too slow above 65;
    # over 10 -> 80 the two cancel almost exactly, over a short swing not.
    verify(minutes(shown["curve"], 50, 300.0) - minutes(shown["profile"], 50, 300.0) >= 1.0,
           "einen kurzen Hub 10 → 50 % schafft das Auto langsamer, als die Vorlage verspricht",
           f"{minutes(shown['curve'], 50, 300.0)} / {minutes(shown['profile'], 50, 300.0)} min")

    trip_id = prepare_trip(client, vehicle["id"])
    params = {"min_kw": 100, "radius_km": 10}
    with_measured = client.post(f"/api/trips/{trip_id}/charge-plan", params=params).json()
    client.put(f"/api/vehicles/{vehicle['id']}",
               json={**payload, "charge_curve_source": "profile"})
    with_profile = client.post(f"/api/trips/{trip_id}/charge-plan", params=params).json()
    verify(with_measured.get("feasible") and with_profile.get("feasible"),
           "beide Ladepläne sind machbar",
           f"{with_measured.get('reason')} / {with_profile.get('reason')}")
    verify(with_measured.get("charge_curve_source") == "measured"
           and with_profile.get("charge_curve_source") == "profile",
           "der Plan sagt, mit welcher Kurve er gerechnet hat",
           f"{with_measured.get('charge_curve_source')} / {with_profile.get('charge_curve_source')}")
    a, b = with_measured.get("charge_time_minutes"), with_profile.get("charge_time_minutes")
    print(f"        Hamburg → München: Ladezeit gemessen {a} min, Profil {b} min")
    verify(a != b and a and b and 0.5 < a / b < 2,
           "und kommt zu einer anderen, plausiblen Ladezeit", f"{a} / {b}")


def main() -> int:
    part_charge_level()
    part_envelope()
    part_merge()
    part_resolver()
    part_planner()
    return verify.balance()


if __name__ == "__main__":
    sys.exit(main())
