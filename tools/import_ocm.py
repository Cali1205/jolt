#!/usr/bin/env python3
"""Ladepunkte von Open Charge Map holen.

Ergänzung zum amtlichen deutschen Register - vor allem für Fahrten über die
Grenze. Braucht einen kostenlosen Schlüssel in OCM_API_KEY:
https://openchargemap.org/site/profile/applications

    ./tools/import_ocm.py                 # Deutschland, 2000 Einträge
    ./tools/import_ocm.py AT,CH 5000 50   # Länder, Anzahl je Land, Mindestleistung kW
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

from app.database import SessionLocal, migrate  # noqa: E402
from app.charging.chargers_import import from_ocm  # noqa: E402


def main() -> int:
    keyname = os.environ.get("OCM_API_KEY", "")
    if not keyname:
        print("OCM_API_KEY ist nicht gesetzt.")
        print(__doc__)
        return 2

    countries = (sys.argv[1] if len(sys.argv) > 1 else "DE").split(",")
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
    min_kw = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0

    migrate()
    db = SessionLocal()
    try:
        counter = from_ocm(db, keyname, countries=[l.strip() for l in countries],
                          max_results=count, min_kw=min_kw)
    finally:
        db.close()

    print(f"Fertig: {counter['neu']} neu, {counter['aktualisiert']} aktualisiert, "
          f"{counter['skipped']} übersprungen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
