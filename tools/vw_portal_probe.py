#!/usr/bin/env python3
"""Try the VW EU Data Act portal with the credentials from the environment.

Logs in with VW_PORTAL_USER and VW_PORTAL_PASSWORD, lists the account's
vehicles (VIN shortened to its last four characters), fetches the newest
datasets and prints what jolt would store. Writes nothing; needs no database.

    docker compose run --rm --no-deps jolt-app python tools/vw_portal_probe.py

Exit code 0 when a snapshot came back, 1 when the portal refused or had
nothing, 2 when the variables are missing. See docs/vw-portal.md.
"""
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
# Locally the package lives under backend/app; in the Docker image it sits
# directly next to tools/ as app/ - both layouts must work.
for _candidate in (os.path.join(HERE, "..", "backend"), os.path.join(HERE, "..")):
    if os.path.isdir(os.path.join(_candidate, "app")):
        sys.path.insert(0, _candidate)
        break

from app.cloud import mask  # noqa: E402
from app.cloud.vw_portal import PortalError, VwPortal  # noqa: E402


def main() -> int:
    user = os.environ.get("VW_PORTAL_USER", "").strip()
    password = os.environ.get("VW_PORTAL_PASSWORD", "")
    if not user or not password:
        print("VW_PORTAL_USER und VW_PORTAL_PASSWORD fehlen in .env "
              "(VW_PORTAL_USER and VW_PORTAL_PASSWORD are not set).")
        return 2

    portal = VwPortal(user, password)
    try:
        vins = portal.vehicles()
        print("Anmeldung ok (login ok). Fahrzeuge im Portal (vehicles): "
              + (", ".join(mask(v) for v in vins) or "keine (none)"))
        vin = os.environ.get("VW_PORTAL_VIN", "").strip()
        if not vin:
            if len(vins) != 1:
                print("VW_PORTAL_VIN setzen: das Konto hat nicht genau ein "
                      "Fahrzeug (set VW_PORTAL_VIN).")
                return 1
            vin = vins[0]
        snap = portal.latest(vin)
    except PortalError as failure:
        print(f"Fehler (error): {failure}")
        return 1
    except Exception as failure:      # noqa: BLE001
        print(f"Unerwarteter Fehler (unexpected error): {type(failure).__name__}")
        return 1

    print(f"Fahrzeug (vehicle) {mask(vin)}: {len(portal.points(vin))} Rohpunkte (raw points)")
    if snap is None:
        print("Noch kein Datensatz mit bekannten Werten. Ist die fortlaufende "
              "Datenanfrage im Portal eingeschaltet? (No dataset with known "
              "values yet - is the continuous data request enabled?)")
        return 1
    age_min = (datetime.now(timezone.utc) - snap.captured_at).total_seconds() / 60
    print(f"Stand (captured at): {snap.captured_at.isoformat()} "
          f"- vor {age_min:.0f} min ({age_min:.0f} min ago)")
    for name, value in snap.values.items():
        print(f"  {name:22s} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
