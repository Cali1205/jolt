#!/usr/bin/env python3
"""Import the vehicle history from the VW EU Data Act portal.

The portal offers a one-off export of everything the car reported (a zip
with one JSON document, tens of megabytes for a year). jolt keeps only what
it finds in it: the drives and the charges, with their kilometre, charge
level and power tracks - not the raw data points. Finished jolt recordings
are added to the logbook in the same run, so a drive both saw is one entry.

Running it again with the same file, or with a newer export that overlaps
the old one, changes nothing that is already there.

    ./tools/import_vw_history.py export.zip [--vehicle ID] [--dry-run]

In the container:

    docker compose run --rm --no-deps -v "$PWD/export.zip:/tmp/export.zip:ro" \\
        jolt-app python tools/import_vw_history.py /tmp/export.zip
"""
import argparse
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
# Locally the package lives under backend/app; in the Docker image (where
# this script runs via `docker exec`) it sits directly next to tools/ as app/ -
# both layouts must work.
for _candidate in (os.path.join(HERE, "..", "backend"), os.path.join(HERE, "..")):
    if os.path.isdir(os.path.join(_candidate, "app")):
        sys.path.insert(0, _candidate)
        break

from app import models  # noqa: E402
from app.cloud import logbook, mask, observations, sessions  # noqa: E402
from app.database import SessionLocal, migrate  # noqa: E402


def read_text(path: str) -> str:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            member = next((m for m in archive.infolist()
                           if m.filename.lower().endswith(".json")), None)
            if member is None:
                raise ValueError("keine JSON-Datei im Archiv")
            return archive.read(member).decode("utf-8")
    with open(path, encoding="utf-8") as file:
        return file.read()


def data_points(text: str):
    """The entries of the `Data` list one by one, so the 500 000 of them
    never exist as dicts at the same time."""
    match = re.search(r'"Data"\s*:\s*\[', text)
    if match is None:
        yield from json.loads(text).get("Data") or []
        return
    decoder = json.JSONDecoder()
    pos = match.end()
    while True:
        while pos < len(text) and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(text) or text[pos] == "]":
            return
        item, pos = decoder.raw_decode(text, pos)
        if isinstance(item, dict):
            yield item


def vin_of(text: str) -> str | None:
    match = re.search(r'"vin"\s*:\s*"([^"]+)"', text[:2000])
    return match.group(1) if match else None


def pick_vehicle(db, wanted: int | None) -> models.Vehicle:
    if wanted is not None:
        vehicle = db.get(models.Vehicle, wanted)
        if vehicle is None:
            raise ValueError(f"Fahrzeug {wanted} gibt es in jolt nicht")
        return vehicle
    vehicles = db.query(models.Vehicle).all()
    if len(vehicles) != 1:
        raise ValueError(f"jolt hat {len(vehicles)} Fahrzeuge - bitte --vehicle angeben")
    return vehicles[0]


def counts_line(counts: dict) -> str:
    return (f"{counts['new']} neu, {counts['updated']} aktualisiert, "
            f"{counts['unchanged']} unverändert")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("file", help="Export des VW-Portals (.zip oder .json)")
    parser.add_argument("--vehicle", type=int, help="jolt-Fahrzeug-ID")
    parser.add_argument("--dry-run", action="store_true",
                        help="nur auswerten, nichts speichern")
    args = parser.parse_args()
    if not os.path.isfile(args.file):
        print(f"Datei nicht gefunden: {args.file}")
        return 2

    migrate()
    text = read_text(args.file)
    vin = vin_of(text)
    print(f"Lese {len(text) / 1e6:.1f} MB"
          + (f" (Fahrzeug {mask(vin)})" if vin else "") + " ...")
    obs = observations.from_history(data_points(text))
    del text
    if not obs:
        print("Abbruch: keine verwertbaren Datenpunkte.")
        return 1

    db = SessionLocal()
    try:
        vehicle = pick_vehicle(db, args.vehicle)
        drives = sessions.detect_drives(obs, dense=True)
        charges = sessions.detect_charges(obs, logbook.kwh_per_pct(vehicle))
        drive_counts = logbook.store_drives(
            db, vehicle.id, logbook.HISTORY,
            [logbook.drive_record(d) for d in drives])
        charge_counts = logbook.store_charges(
            db, vehicle.id, logbook.HISTORY,
            [logbook.charge_record(c) for c in charges])
        jolt_counts = logbook.store_all_sessions(db, vehicle.id)
        both = sum(1 for row in db.query(models.VehicleDrive)
                   .filter_by(vehicle_id=vehicle.id)
                   if {logbook.HISTORY, logbook.JOLT} <= set(row.sources))

        first = min(o.t for o in obs)
        last = max(o.t for o in obs)
        km = sum(d.odo_end - d.odo_start for d in drives)
        kwh = sum(c.energy_kwh for c in charges)
        by_type = {kind: [c for c in charges if c.charge_type == kind]
                   for kind in ("AC", "DC", None)}
        print(f"Fahrzeug:      {vehicle.name} (id {vehicle.id})")
        print(f"Zeitraum:      {first:%Y-%m-%d} bis {last:%Y-%m-%d}, "
              f"{len(obs)} Datenpunkte verwertet")
        print(f"Fahrten:       {len(drives)}, {km:.0f} km "
              f"({counts_line(drive_counts)})")
        print(f"Ladevorgänge:  {len(charges)}, {kwh:.0f} kWh "
              f"({counts_line(charge_counts)})")
        for kind, items in by_type.items():
            if items:
                print(f"  {kind or 'unbekannt'}: {len(items)}, "
                      f"{sum(c.energy_kwh for c in items):.0f} kWh")
        print(f"jolt-Fahrten:  {counts_line(jolt_counts)}; "
              f"{both} Fahrten aus VW-Daten und jolt zusammengeführt")
        if args.dry_run:
            db.rollback()
            print("Probelauf: nichts gespeichert.")
        else:
            db.commit()
            print("Gespeichert.")
    except ValueError as failure:
        db.rollback()
        print(f"Abbruch: {failure}")
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
