#!/usr/bin/env python3
"""Fetch charging points from Open Charge Map along an already computed trip.

The country import (`import_ocm.py`) does not reliably page through OCM's
result pages for very large countries - for France, for example, a large
part of the charging points remained unreachable no matter how high the
limit was set. This script instead queries several smaller radii along the
actual route geometry - the same kind of request that OCM answers reliably
in practice.

Prerequisite: the trip must have been computed in the app beforehand (under
"Planen" on "Route rechnen"). The ID is in the response of GET /api/trips
or in the URL when you open the trip in the UI.

    ./tools/import_ocm_route.py 42            # trip 42, 30 km radius, all power levels
    ./tools/import_ocm_route.py 42 25 50      # radius 25 km, from 50 kW
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Locally the package lives under backend/app; in the Docker image (where
# this script runs via `docker exec`) it sits directly next to tools/ as app/ -
# both layouts must work.
for _candidate in (os.path.join(HERE, "..", "backend"), os.path.join(HERE, "..")):
    if os.path.isdir(os.path.join(_candidate, "app")):
        sys.path.insert(0, _candidate)
        break

from app import models  # noqa: E402
from app.database import SessionLocal, migrate  # noqa: E402
from app.charging.chargers_import import from_ocm_route  # noqa: E402


def main() -> int:
    keyname = os.environ.get("OCM_API_KEY", "")
    if not keyname:
        print("OCM_API_KEY ist nicht gesetzt.")
        print(__doc__)
        return 2
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    trip_id = int(sys.argv[1])
    radius_km = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    min_kw = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0

    migrate()
    db = SessionLocal()
    try:
        trip = db.get(models.Trip, trip_id)
        if not trip:
            print(f"Fahrt {trip_id} nicht gefunden - erst in der App berechnen.")
            return 2
        if not trip.geometry or len(trip.geometry) < 2:
            print(f"Fahrt {trip_id} hat keine brauchbare Geometrie.")
            return 2

        try:
            counter = from_ocm_route(db, keyname, trip.geometry,
                                    radius_km=radius_km, min_kw=min_kw)
        except ValueError as failure:
            print(f"Abbruch: {failure}")
            return 1
    finally:
        db.close()

    print(f"Fertig: {counter['neu']} neu, {counter['aktualisiert']} aktualisiert, "
          f"{counter['skipped']} übersprungen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
