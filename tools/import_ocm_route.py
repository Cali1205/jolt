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

HIER = os.path.dirname(os.path.abspath(__file__))
# Lokal liegt das Paket unter backend/app; im Docker-Image (wo dieses Skript
# per `docker exec` läuft) liegt es direkt neben tools/ als app/ - beide
# Layouts müssen funktionieren.
for _kandidat in (os.path.join(HIER, "..", "backend"), os.path.join(HIER, "..")):
    if os.path.isdir(os.path.join(_kandidat, "app")):
        sys.path.insert(0, _kandidat)
        break

from app import models  # noqa: E402
from app.database import SessionLocal, migrate  # noqa: E402
from app.laden.saeulen_import import aus_ocm_route  # noqa: E402


def main() -> int:
    schluessel = os.environ.get("OCM_API_KEY", "")
    if not schluessel:
        print("OCM_API_KEY ist nicht gesetzt.")
        print(__doc__)
        return 2
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    fahrt_id = int(sys.argv[1])
    radius_km = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    min_kw = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0

    migrate()
    db = SessionLocal()
    try:
        fahrt = db.get(models.Fahrt, fahrt_id)
        if not fahrt:
            print(f"Fahrt {fahrt_id} nicht gefunden - erst in der App berechnen.")
            return 2
        if not fahrt.geometrie or len(fahrt.geometrie) < 2:
            print(f"Fahrt {fahrt_id} hat keine brauchbare Geometrie.")
            return 2

        try:
            zaehler = aus_ocm_route(db, schluessel, fahrt.geometrie,
                                    radius_km=radius_km, min_kw=min_kw)
        except ValueError as fehler:
            print(f"Abbruch: {fehler}")
            return 1
    finally:
        db.close()

    print(f"Fertig: {zaehler['neu']} neu, {zaehler['aktualisiert']} aktualisiert, "
          f"{zaehler['uebersprungen']} übersprungen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
