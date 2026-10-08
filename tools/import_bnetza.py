#!/usr/bin/env python3
"""Import the charging station register of the Bundesnetzagentur.

Deliberately a script and not an endpoint: the file is over 50 MB, and an
HTTP call that blocks for minutes is the wrong place for it. This also lets
it be hooked into a cron job - the import is idempotent.

First download the file from the charging station map of the
Bundesnetzagentur ("Ladesäulenregister", CSV):
https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenkarte/start.html

    ./tools/import_bnetza.py ladesaeulenregister.csv

In the running container:

    docker exec -i jolt-app python tools/import_bnetza.py /path/to/file.csv
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

from app.database import SessionLocal, migrate  # noqa: E402
from app.charging.chargers_import import from_bnetza_csv  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    fs_path = sys.argv[1]
    if not os.path.isfile(fs_path):
        print(f"Datei nicht gefunden: {fs_path}")
        return 2

    migrate()
    with open(fs_path, "rb") as file:
        contents = file.read()

    print(f"Lese {len(contents) / 1e6:.1f} MB aus {fs_path} ...")
    db = SessionLocal()
    try:
        counter = from_bnetza_csv(db, contents)
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
