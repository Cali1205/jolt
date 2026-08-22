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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "backend"))

from app.database import SessionLocal, migrate  # noqa: E402
from app.laden.saeulen_import import aus_bnetza_csv  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    pfad = sys.argv[1]
    if not os.path.isfile(pfad):
        print(f"Datei nicht gefunden: {pfad}")
        return 2

    migrate()
    with open(pfad, "rb") as datei:
        inhalt = datei.read()

    print(f"Lese {len(inhalt) / 1e6:.1f} MB aus {pfad} ...")
    db = SessionLocal()
    try:
        zaehler = aus_bnetza_csv(db, inhalt)
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
