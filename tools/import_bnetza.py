#!/usr/bin/env python3
"""Ladesäulenregister der Bundesnetzagentur einlesen.

Bewusst ein Skript und kein Endpunkt: Die Datei ist über 50 MB gross, und ein
HTTP-Aufruf, der minutenlang blockiert, ist der falsche Ort dafür. So lässt es
sich auch in einen Cronjob hängen - der Import ist idempotent.

Die Datei zuerst von der Ladesäulenkarte der Bundesnetzagentur herunterladen
("Ladesäulenregister", CSV):
https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenkarte/start.html

    ./tools/import_bnetza.py ladesaeulenregister.csv

Im laufenden Container:

    docker exec -i jolt-app python tools/import_bnetza.py /pfad/zur/datei.csv
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
