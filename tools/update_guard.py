#!/usr/bin/env python3
"""Pre-update hook for Watchtower: do not restart while a trip is running.

Watchtower runs this inside the jolt-app container before it replaces it
(label `com.centurylinklabs.watchtower.lifecycle.pre-update`). Exit code 75
(EX_TEMPFAIL) means "skip this container for now" - Watchtower asks again at
the next run. Every other code lets the update proceed.

A restart cuts the connection of a running trip: the measurement points of the
phone fail for a few seconds and the live view drops. Waiting costs nothing
that matters, so a running session wins over a new version.

Fail-open on purpose: if the database cannot be asked, the update goes ahead.
Otherwise a broken installation could never be repaired by a new image.

    ./tools/update_guard.py        # exit 75 = trip running, 0 = go ahead
"""
import os
import sys

from sqlalchemy import create_engine, text

SKIP = 75


def running_sessions(url: str) -> int:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return int(connection.execute(
                text("SELECT COUNT(*) FROM live_sessions WHERE running")
            ).scalar() or 0)
    finally:
        engine.dispose()


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("update guard: no DATABASE_URL - update may proceed")
        return 0
    try:
        count = running_sessions(url)
    except Exception as failure:      # noqa: BLE001
        print("update guard: database not readable (%s) - update may proceed"
              % type(failure).__name__)
        return 0
    if count:
        print("update guard: %d trip(s) running - update postponed" % count)
        return SKIP
    print("update guard: no trip running - update may proceed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
