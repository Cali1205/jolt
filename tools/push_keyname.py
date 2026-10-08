#!/usr/bin/env python3
"""Ein VAPID-Schlüsselpaar für die Benachrichtigungen erzeugen.

Einmal ausführen, die drei Zeilen in die `.env` übernehmen, jolt neu starten.

    ./tools/push_keyname.py

Der **private** Schlüssel gehört ausschliesslich auf den Server. Wer ihn hat,
kann Benachrichtigungen im Namen dieser Installation verschicken - an die
Geräte, die sich hier angemeldet haben. Er gehört deshalb nicht ins Repo, nicht
in ein Backup, das andere lesen können, und nicht in eine Chatnachricht.

Der **öffentliche** Schlüssel ist dafür da, verteilt zu werden: Der Browser
braucht ihn, um ein Abo anzulegen.

Achtung beim Wechsel: Neue Schlüssel machen alle bestehenden Abos ungültig.
Die Geräte müssen sich dann einmal neu anmelden - jolt räumt die toten Abos
beim nächsten Versand von selbst weg.
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

from app.push import generate_key  # noqa: E402


def main() -> int:
    private_, publicly = generate_key()
    print("# In die .env übernehmen - der private Schlüssel bleibt auf dem "
          "Server:\n")
    print(f"VAPID_PRIVATE_KEY={private_}")
    print(f"VAPID_PUBLIC_KEY={publicly}")
    print("VAPID_SUBJECT=mailto:du@example.org")
    print("\n# VAPID_SUBJECT auf eine erreichbare Adresse ändern: Manche "
          "Push-Dienste\n# lehnen ab, wenn dort niemand steht, den sie bei "
          "Auffälligkeiten erreichen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
