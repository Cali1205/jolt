#!/usr/bin/env python3
"""Generate a VAPID key pair for the notifications.

Run once, copy the three lines into the `.env`, restart jolt.

    ./tools/push_keyname.py

The **private** key belongs exclusively on the server. Whoever has it can
send notifications in the name of this installation - to the devices that
have registered here. It therefore does not belong in the repo, not in a
backup that others can read, and not in a chat message.

The **public** key is meant to be distributed: the browser needs it to
create a subscription.

Caution when changing: new keys invalidate all existing subscriptions.
The devices then have to register once again - jolt clears away the dead
subscriptions by itself on the next send.
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
