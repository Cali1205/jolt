#!/usr/bin/env python3
"""Checks that the old German URL paths still work (`legacy_paths.py`).

The API paths are English now. Clients set up earlier (an iOS shortcut posting
to `/api/live/melden`, an old bookmark calling `/api/fahrzeuge`) must keep
working until they are updated.

Without network, without Postgres, without API key:

    ./tools/check_legacy_paths.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from examine import Check, application_provide  # noqa: E402

application_provide("pfade", db_name=False)

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="jolt-pfade-"), "check.db")
os.environ.pop("APP_PASSWORT", None)
os.environ["RATE_LIMIT_PER_MIN"] = "100000"

from fastapi.testclient import TestClient  # noqa: E402

from app.legacy_paths import modern  # noqa: E402
from app.main import app  # noqa: E402

verify = Check()


def main() -> int:
    verify.section("Rewriting")
    verify(modern("/api/fahrzeuge") == "/api/vehicles", "a fixed segment is translated")
    verify(modern("/api/live/12/ende") == "/api/live/12/end",
           "ids stay, the segment after them is translated")
    verify(modern("/api/fahrten/3/ladeplan") == "/api/trips/3/charge-plan",
           "several segments in one path")
    verify(modern("/api/vehicles") == "/api/vehicles", "an English path stays")
    verify(modern("/static/live.js") == "/static/live.js"
           and modern("/obd") == "/obd", "paths outside /api/ are not touched")

    verify.section("Requests")
    client = TestClient(app)
    old = client.get("/api/fahrzeuge")
    new = client.get("/api/vehicles")
    verify(old.status_code == 200 and old.json() == new.json(),
           "the old vehicle list path answers like the new one",
           f"{old.status_code} {new.status_code}")
    verify(client.get("/api/fahrzeuge/vorlagen").json()
           == client.get("/api/vehicles/templates").json(),
           "also with a second translated segment")
    verify(client.get("/api/orte?text=Muenchen").status_code
           == client.get("/api/places?text=Muenchen").status_code,
           "and with a query string")
    unknown = client.get("/api/gibtsnicht")
    verify(unknown.status_code == 404, "an unknown path is still a 404")
    return verify.balance()


if __name__ == "__main__":
    sys.exit(main())
