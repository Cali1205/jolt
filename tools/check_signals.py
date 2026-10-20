#!/usr/bin/env python3
"""Checks the signal catalog (backend/app/signals.py) against everything
that speaks it: the docs, the sources, stored data and the signals API.

Without network, without Postgres:

    ./tools/check_signals.py            check
    ./tools/check_signals.py --write    regenerate the table in docs/data-model.md
"""
import json
import os
import re
import sys

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
from examine import Check, application_provide  # noqa: E402

application_provide("signals")

from app import signals  # noqa: E402

verify = Check()
ROOT = os.path.join(TOOLS, "..")
DOC = os.path.join(ROOT, "docs", "data-model.md")
TABLE = re.compile(r"(<!-- signals:start -->\r?\n)(.*?)(\r?\n<!-- signals:end -->)", re.S)


def _doc() -> str:
    with open(DOC, encoding="utf-8", newline="") as file:
        return file.read()


def write_doc() -> None:
    text = _doc()
    newline = "\r\n" if "\r\n" in text else "\n"
    table = signals.markdown_table().replace("\n", newline)
    with open(DOC, "w", encoding="utf-8", newline="") as file:
        file.write(TABLE.sub(lambda m: m.group(1) + table + m.group(3), text))


def part_catalog():
    verify.section("Katalog")
    names = [s.name for s in signals.SIGNALS]
    verify(len(names) == len(set(names)), "jeder Signalname kommt einmal vor")
    verify(all(re.fullmatch(r"[a-z][a-z0-9_]*", n) for n in names),
           "Namen sind klein, mit Unterstrich")
    verify([s.name for s in signals.SIGNALS if s.tier == "required"] == ["soc_pct"],
           "Pflicht ist allein der Ladestand")
    verify(all(s.tier in signals.TIERS and s.kind in ("number", "bool", "enum", "text")
               for s in signals.SIGNALS), "Stufe und Art aus der festen Liste")
    verify(all(bool(s.values) == (s.kind == "enum") for s in signals.SIGNALS),
           "genau die Aufzählungen nennen ihre Werte")
    verify(all(s.label and s.meaning for s in signals.SIGNALS),
           "jedes Signal hat Bezeichnung und Bedeutung")
    for name in ("odometer_km", "speed_kmh", "outside_temp_c", "batt_temp_c",
                 "charge_power_kw", "charge_state", "plugged"):
        verify(signals.BY_NAME[name].tier == "recommended", f"{name} ist empfohlen")
    verify(signals.CHARGE_STATES == ("charging", "ready", "not_ready", "purpose_reached",
                                     "conservation", "error"),
           "Ladezustände wie in der Erkennung", str(signals.CHARGE_STATES))


def part_doc():
    verify.section("docs/data-model.md")
    match = TABLE.search(_doc())
    table = match.group(2).replace("\r\n", "\n") if match else ""
    verify(table == signals.markdown_table(),
           "die Tabelle entspricht signals.py (sonst: check_signals.py --write)")


def _platform_names() -> dict[str, list[str]]:
    """Row names per OBD platform profile in frontend/platforms/."""
    folder = os.path.join(ROOT, "frontend", "platforms")
    found = {}
    for file in sorted(os.listdir(folder)):
        with open(os.path.join(folder, file), encoding="utf-8") as handle:
            found[file[:-3]] = re.findall(r'\bname:\s*"([a-z0-9_]+)"', handle.read())
    return found


def part_sources():
    verify.section("Quellen sprechen den Katalog")
    from app import cloud
    from app.cloud import observations, snapshot, stellantis
    verify(set(cloud.SOURCES) == {"vw_portal", "stellantis"}
           and set(cloud.SOURCES) <= signals.SOURCE_LABELS.keys(),
           "jede Cloud-Quelle hat eine Bezeichnung", str(sorted(cloud.SOURCES)))
    for name, provides in (("VW-Portal", snapshot.PROVIDES),
                           ("Stellantis", stellantis.PROVIDES)):
        verify(provides and not signals.unknown(provides),
               f"{name}: PROVIDES nur Katalognamen", str(signals.unknown(provides)))
    verify(snapshot.PROVIDES == {name for name, _, _ in snapshot.FIELDS},
           "VW-Portal: PROVIDES ist, was FIELDS liefert")
    history = {field for field, _ in observations.HISTORY_FIELDS.values()}
    verify(not signals.unknown(history), "VW-Export: HISTORY_FIELDS nur Katalognamen",
           str(signals.unknown(history)))
    verify(not signals.unknown(observations.LIVE_RAW.values()),
           "Dongle: LIVE_RAW zeigt auf Katalognamen")
    verify(not set(observations.DERIVED) & signals.BY_NAME.keys(),
           "abgeleitete Beobachtungen sind keine Signale")
    for platform, names in _platform_names().items():
        provides = sorted({observations.LIVE_RAW.get(n, n) for n in names}
                          & signals.BY_NAME.keys() | {"soc_pct"})
        verify("soc_raw" in names, f"Plattform {platform}: soc_raw vorhanden; "
               f"liefert {', '.join(provides)}")
    with open(os.path.join(ROOT, "frontend", "vehicle.js"), encoding="utf-8") as handle:
        block = re.search(r"const CHARGE_STATES = \{(.*?)\};", handle.read(), re.S)
    keys = tuple(re.findall(r"(\w+):", block.group(1))) if block else ()
    verify(keys == signals.CHARGE_STATES,
           "die Fahrzeugansicht beschriftet genau die Ladezustände des Katalogs", str(keys))


def _alembic(target: str, down: bool = False) -> None:
    from alembic import command
    from alembic.config import Config
    backend = os.path.join(ROOT, "backend")
    cfg = Config(os.path.join(backend, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(backend, "alembic"))
    (command.downgrade if down else command.upgrade)(cfg, target)


OLD_RAW = {"k-soc": ["battery_level_HV.value", "57.0", None],
           "k-state": ["charging_state_report.current_charge_state",
                       "CHARGE_STATE_NOT_READY_FOR_CHARGING", None],
           "k-clima": ["climatisation_state", "CLIMATISATION_STATE_OFF", None],
           "k-odo": ["mileage.value", "62067", None]}
OLD_VALUES = {"soc_pct": 57, "odometer_km": 62067, "charge_power_kw": 0,
              "charge_state": "CHARGE_STATE_NOT_READY_FOR_CHARGING",
              "climatisation_state": "CLIMATISATION_STATE_OFF", "locked": True}


def part_old_rows():
    verify.section("Alte VW-Schnappschüsse")
    import sqlalchemy as sa
    from app.database import engine
    _alembic("0023")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO vehicles (id, name, battery_gross_kwh, battery_net_kwh, curb_mass_kg,"
            " payload_kg, c_w, frontal_area_m2, c_rr, eta_drive, eta_regen, p_aux_w,"
            " heat_pump, reserve_soc, target_soc, max_charge_power_kw, platform,"
            " connector_type, charge_curve_source, preferred_operators,"
            " electricity_price_eur_kwh, correction_factor, created_at)"
            " VALUES (1, 'Buzz', 86, 79, 2400, 150, 0.29, 3.2, 0.01, 0.88, 0.7, 350,"
            " 1, 10, 20, 170, 'meb', 'CCS', 'auto', '[]', 0.59, 1.0, '2026-10-01')"))
        for n, (raw, state) in enumerate([(OLD_RAW, "CHARGE_STATE_NOT_READY_FOR_CHARGING"),
                                          (None, "chargePurposeReachedAndConservation"),
                                          (None, "CHARGE_STATE_INVALID")]):
            connection.execute(
                sa.text("INSERT INTO vehicle_snapshots (vehicle_id, source, captured_at,"
                        " received_at, \"values\", raw) VALUES (1, 'vw_portal', :t, :t, :v, :r)"),
                {"t": f"2026-10-20 0{n}:17:14", "v": json.dumps({**OLD_VALUES, "charge_state": state}),
                 "r": json.dumps(raw) if raw else None})
    _alembic("head")
    from app import models
    from app.cloud import observations
    from app.database import SessionLocal
    db = SessionLocal()
    rows = db.query(models.VehicleSnapshot).order_by(models.VehicleSnapshot.captured_at).all()
    stored = [r.values for r in rows]
    verify([v.get("charge_state") for v in stored] == ["not_ready", "conservation", None],
           "Ladezustand nach der Migration im Katalog; unbekannt fällt weg",
           str([v.get("charge_state") for v in stored]))
    verify(all(v.get("climate_state") == "off" and "climatisation_state" not in v
               for v in stored), "climatisation_state heißt climate_state, klein",
           str(stored[0]))
    verify(all(not signals.unknown(v) for v in stored),
           "gespeicherte Werte sind nur noch Katalognamen",
           str([signals.unknown(v) for v in stored]))
    verify(rows[0].raw == OLD_RAW, "die Rohpunkte bleiben unverändert")
    obs = observations.from_snapshots(rows)
    first = {o.field: o.value for o in obs if o.t.hour == 0}
    verify(first == {"soc_pct": 57.0, "odometer_km": 62067.0,
                     "charge_state": "not_ready", "climate_state": "off"},
           "eine alte Meldung mit Rohpunkten liest sich aus ihren eigenen Punkten",
           str(first))
    second = {o.field: o.value for o in obs if o.t.hour == 1}
    verify(second.get("charge_state") == "conservation" and second.get("soc_pct") == 57.0
           and second.get("locked") is True,
           "eine alte Meldung ohne Rohpunkte liest sich aus values", str(second))
    db.close()
    _alembic("0023", down=True)
    with engine.connect() as connection:
        back = [json.loads(v) if isinstance(v, str) else v for (v,) in connection.execute(
            sa.text("SELECT \"values\" FROM vehicle_snapshots ORDER BY captured_at"))]
    verify(back[0].get("charge_state") == "CHARGE_STATE_NOT_READY_FOR_CHARGING"
           and back[0].get("climatisation_state") == "CLIMATISATION_STATE_OFF",
           "zurück auf 0023: wieder VWs Schreibweise", str(back[0]))
    _alembic("head")


def _vehicle(db, name: str) -> int:
    from app import models
    vehicle = models.Vehicle(name=name, battery_gross_kwh=50.0, battery_net_kwh=46.0)
    db.add(vehicle)
    db.flush()
    return vehicle.id


def part_api():
    verify.section("GET /api/vehicles/{id}/signals")
    from datetime import datetime, timedelta
    from fastapi.testclient import TestClient
    from app import models
    from app.database import SessionLocal
    from app.main import app
    db = SessionLocal()
    live, cloud, empty = (_vehicle(db, "Nur Dongle"), _vehicle(db, "Nur Cloud"),
                          _vehicle(db, "Nichts"))
    trip = models.Trip(vehicle_id=live, recording=True, start_lat=48.1, start_lon=11.5,
                       target_lat=48.2, target_lon=11.6, start_soc=80, geometry=[],
                       energy_profile=[])
    db.add(trip)
    db.flush()
    session = models.LiveSession(trip_id=trip.id, running=False,
                                 started_at=datetime(2026, 10, 19, 8))
    db.add(session)
    db.flush()
    for i in range(3):
        db.add(models.LivePoint(session_id=session.id, lat=48.1, lon=11.5 + i / 100,
                                timestamp=datetime(2026, 10, 19, 8) + timedelta(minutes=i),
                                soc=80 - i, speed_kmh=50,
                                raw_values={"odometer_km": 1000 + i, "batterie_c": 21,
                                            "soc_raw": 160 - 2 * i}))
    db.add(models.VehicleSnapshot(vehicle_id=cloud, source="stellantis",
                                  captured_at=datetime(2026, 10, 20, 7),
                                  received_at=datetime(2026, 10, 20, 7),
                                  values={"soc_pct": 59, "odometer_km": 3196.5,
                                          "plugged": False, "lat": 47.274, "lon": -1.59},
                                  raw=None))
    db.commit()
    db.close()
    with TestClient(app) as client:
        answers = {v: client.get(f"/api/vehicles/{v}/signals").json()
                   for v in (live, cloud, empty)}
        missing = client.get("/api/vehicles/99999/signals").status_code

    def row(vehicle, name):
        return next(s for s in answers[vehicle]["signals"] if s["name"] == name)

    def sources(vehicle, name):
        return [s["id"] for s in row(vehicle, name)["sources"]]

    verify([s["name"] for s in answers[live]["signals"]] == [s.name for s in signals.SIGNALS],
           "jedes Katalogsignal steht in der Antwort, in Katalogreihenfolge")
    verify(sources(live, "soc_pct") == ["jolt"] and sources(live, "odometer_km") == ["jolt"]
           and row(live, "soc_pct")["last_at"] == "2026-10-19T08:02:00Z",
           "nur Dongle: Ladestand und Kilometerstand von jolt, zuletzt um 08:02",
           str(row(live, "soc_pct")))
    verify(sources(live, "batt_temp_c") == ["jolt"],
           "batterie_c des Dongles zählt als batt_temp_c")
    verify(not row(live, "charge_power_kw")["seen"] and not row(live, "plugged")["seen"]
           and answers[live]["missing_required"] == [],
           "nur Dongle: Ladeleistung und Stecker fehlen, Pflicht ist erfüllt",
           str(answers[live]["missing_required"]))
    verify(sources(cloud, "soc_pct") == ["stellantis"]
           and sources(cloud, "lat") == ["stellantis"] and row(cloud, "plugged")["seen"]
           and row(cloud, "soc_pct")["sources"][0]["label"] == "Stellantis",
           "nur Cloud: Ladestand, Position und Stecker von Stellantis",
           str(row(cloud, "soc_pct")))
    verify(not row(cloud, "speed_kmh")["seen"] and not row(cloud, "batt_temp_c")["seen"],
           "nur Cloud: Tempo und Batterietemperatur fehlen")
    verify(answers[empty]["missing_required"] == ["soc_pct"]
           and not any(s["seen"] for s in answers[empty]["signals"]),
           "ohne Daten: der Ladestand fehlt als Pflicht", str(answers[empty]["missing_required"]))
    verify(missing == 404, "unbekanntes Fahrzeug: 404", str(missing))


PARTS = [part_catalog, part_doc, part_sources, part_old_rows, part_api]


def main() -> int:
    if "--write" in sys.argv:
        write_doc()
        print("docs/data-model.md neu geschrieben.")
        return 0
    for part in PARTS:
        part()
    return verify.balance()


if __name__ == "__main__":
    sys.exit(main())
