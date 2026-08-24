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

HIER = os.path.dirname(os.path.abspath(__file__))
# Lokal liegt das Paket unter backend/app; im Docker-Image (wo dieses Skript
# per `docker exec` läuft) liegt es direkt neben tools/ als app/ - beide
# Layouts müssen funktionieren.
for _kandidat in (os.path.join(HIER, "..", "backend"), os.path.join(HIER, "..")):
    if os.path.isdir(os.path.join(_kandidat, "app")):
        sys.path.insert(0, _kandidat)
        break

from app.database import SessionLocal, migrate  # noqa: E402
from app.laden.saeulen_import import aus_ocm  # noqa: E402


def main() -> int:
    schluessel = os.environ.get("OCM_API_KEY", "")
    if not schluessel:
        print("OCM_API_KEY ist nicht gesetzt.")
        print(__doc__)
        return 2

    laender = (sys.argv[1] if len(sys.argv) > 1 else "DE").split(",")
    anzahl = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
    min_kw = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0

    migrate()
    db = SessionLocal()
    try:
        zaehler = aus_ocm(db, schluessel, laender=[l.strip() for l in laender],
                          max_ergebnisse=anzahl, min_kw=min_kw)
    finally:
        db.close()

    print(f"Fertig: {zaehler['neu']} neu, {zaehler['aktualisiert']} aktualisiert, "
          f"{zaehler['uebersprungen']} übersprungen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
