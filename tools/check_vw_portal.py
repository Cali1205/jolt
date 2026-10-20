#!/usr/bin/env python3
"""Checks the VW EU Data Act portal module - `app/cloud/`.

The real portal needs a VW account and a car that reports, so none of this
talks to it. The data points come from a recorded shape
(tools/fixtures/vw_portal_points.json, synthetic VIN), the login runs
against a fake identity service and portal behind the real host names.

Without network, without Postgres:

    ./tools/check_vw_portal.py
"""
import json
import os
import sys
from datetime import datetime, timezone

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
from examine import Check, application_provide  # noqa: E402

application_provide("vw-portal")
for _name in ("VW_PORTAL_USER", "VW_PORTAL_PASSWORD", "VW_PORTAL_VIN",
              "VW_PORTAL_VEHICLE", "VW_PORTAL_INTERVAL_MIN"):
    os.environ.pop(_name, None)

from app.cloud import snapshot  # noqa: E402

verify = Check()

with open(os.path.join(TOOLS, "fixtures", "vw_portal_points.json"),
          encoding="utf-8") as _file:
    DATASET = json.load(_file)


def part_snapshot():
    verify.section("Datenpunkte -> Schnappschuss")
    points = snapshot.decode(DATASET["Data"])
    snap = snapshot.from_points(points)
    expected = {
        "soc_pct": 57, "range_km": 192, "odometer_km": 62067,
        "energy_kwh": 36.65, "capacity_kwh": 72.6,
        "batt_temp_min_c": 15.0, "batt_temp_max_c": 15.5,
        "charge_power_kw": 0, "charge_state": "CHARGE_STATE_NOT_READY_FOR_CHARGING",
        "target_soc_pct": 80, "locked": True,
        "climatisation_state": "CLIMATISATION_STATE_OFF",
    }
    verify(snap is not None, "aus dem Datensatz entsteht ein Schnappschuss")
    if snap is None:
        return
    for name, value in expected.items():
        got = snap.values.get(name)
        verify(got == value and type(got) in (type(value), int, float)
               and isinstance(got, bool) == isinstance(value, bool),
               f"{name} = {value!r}", f"ist {got!r}")
    verify(set(snap.values) == set(expected),
           "nur bekannte Werte werden behalten (parking_brake, Türen fallen weg)",
           f"zusätzlich: {sorted(set(snap.values) - set(expected))}")
    verify(snap.captured_at == datetime(2026, 10, 20, 0, 17, 14, tzinfo=timezone.utc),
           "Stand ist car_captured_time, wann das Auto gemeldet hat",
           str(snap.captured_at))
    verify(snapshot.lookup(points, ("battery_level_HV.value",)).value == "57.0",
           "von zwei Einträgen derselben Kennung gewinnt der neuere, auch wenn er "
           "im Datensatz vorne steht")

    without_time = [p for p in points if p.name != snapshot.CAPTURED_AT]
    snap = snapshot.from_points(without_time)
    verify(snap is not None and snap.captured_at
           == datetime(2026, 10, 20, 0, 17, 13, tzinfo=timezone.utc),
           "ohne car_captured_time gilt der jüngste Zeitstempel eines Punktes",
           str(snap and snap.captured_at))

    verify(snapshot.from_points(snapshot.decode([
        {"key": "x", "dataFieldName": "parking_brake", "value": "true",
         "timestampUtc": "2026-10-20T00:17:13Z"}])) is None,
        "nur unbekannte Punkte: kein Schnappschuss")
    broken = snapshot.decode([
        {"key": "a", "dataFieldName": "battery_level_HV.value", "value": "n/a",
         "timestampUtc": "2026-10-20T00:17:13Z"},
        {"key": "b", "dataFieldName": "mileage.value", "value": "NaN",
         "timestampUtc": "2026-10-20T00:17:13Z"},
        {"key": "c", "dataFieldName": "locked", "value": "1",
         "timestampUtc": "2026-10-20T00:17:13Z"},
        {"key": "d", "dataFieldName": "max_temperature", "value": "16",
         "timestampUtc": "2026-10-20T00:17:13Z"}])
    snap = snapshot.from_points(broken)
    verify(snap is not None and snap.values == {"batt_temp_max_c": 16},
           "ein unlesbarer Wert fällt weg, statt den Rest mitzureissen",
           str(snap and snap.values))

    older = snapshot.decode([
        {"key": "a", "dataFieldName": "battery_level_HV.value", "value": "40",
         "timestampUtc": "2026-10-20T01:00:00Z"}])
    newer = snapshot.decode([
        {"key": snapshot.KEY_ENYAQ_SOC, "dataFieldName": "value", "value": "41",
         "timestampUtc": "2026-10-20T00:30:00Z"}])
    merged = snapshot.merge(snapshot.merge([], older, 1), newer, 2)
    snap = snapshot.from_points(merged)
    verify(snap.values["soc_pct"] == 41,
           "über mehrere Datensätze zählt der zuletzt gelieferte Wert, nicht die "
           "Reihenfolge der Kandidaten und nicht der Zeitstempel des Punktes",
           str(snap.values))


PARTS = [part_snapshot]


def main() -> int:
    for part in PARTS:
        part()
    return verify.balance(
        "Nicht geprüft: das echte VW-Portal. Dafür tools/vw_portal_probe.py "
        "mit eigenen Zugangsdaten.")


if __name__ == "__main__":
    sys.exit(main())
