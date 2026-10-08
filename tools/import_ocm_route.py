#!/usr/bin/env python3
"""Ladepunkte von Open Charge Map entlang einer bereits gerechneten Fahrt holen.

Der Länder-Import (`import_ocm.py`) blättert bei sehr grossen Ländern nicht
zuverlässig durch OCMs Ergebnisseiten - für Frankreich etwa blieb ein
grosser Teil der Ladepunkte unerreichbar, egal wie hoch das Limit stand.
Dieses Skript fragt stattdessen mehrere kleinere Umkreise entlang der
tatsächlichen Streckengeometrie ab - dieselbe Art Anfrage, die OCM in der
Praxis zuverlässig beantwortet.

Voraussetzung: die Fahrt muss vorher in der App berechnet worden sein (unter
"Planen" auf "Route rechnen"). Die ID steht in der Antwort von GET /api/fahrten
oder in der URL, wenn man die Fahrt in der Oberfläche öffnet.

    ./tools/import_ocm_route.py 42            # Fahrt 42, 30 km Umkreis, alle Leistungen
    ./tools/import_ocm_route.py 42 25 50      # Umkreis 25 km, ab 50 kW
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Lokal liegt das Paket unter backend/app; im Docker-Image (wo dieses Skript
# per `docker exec` läuft) liegt es direkt neben tools/ als app/ - beide
# Layouts müssen funktionieren.
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
