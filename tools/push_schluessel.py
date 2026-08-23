#!/usr/bin/env python3
"""Ein VAPID-Schlüsselpaar für die Benachrichtigungen erzeugen.

Einmal ausführen, die drei Zeilen in die `.env` übernehmen, jolt neu starten.

    ./tools/push_schluessel.py

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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "backend"))

from app.push import schluessel_erzeugen  # noqa: E402


def main() -> int:
    privat, oeffentlich = schluessel_erzeugen()
    print("# In die .env übernehmen - der private Schlüssel bleibt auf dem "
          "Server:\n")
    print(f"VAPID_PRIVATE_KEY={privat}")
    print(f"VAPID_PUBLIC_KEY={oeffentlich}")
    print("VAPID_SUBJECT=mailto:du@example.org")
    print("\n# VAPID_SUBJECT auf eine erreichbare Adresse ändern: Manche "
          "Push-Dienste\n# lehnen ab, wenn dort niemand steht, den sie bei "
          "Auffälligkeiten erreichen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
