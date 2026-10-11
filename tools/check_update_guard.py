#!/usr/bin/env python3
"""Checks the Watchtower pre-update hook: skip while a trip runs, else go ahead.

    ./tools/check_update_guard.py
"""
import os
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from examine import Check  # noqa: E402

verify = Check()
GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update_guard.py")


def run(url: str):
    env = {**os.environ, "DATABASE_URL": url}
    done = subprocess.run([sys.executable, GUARD], env=env,
                          capture_output=True, text=True)
    return done.returncode, done.stdout.strip()


def main() -> int:
    folder = tempfile.mkdtemp(prefix="jolt-guard-")
    path = os.path.join(folder, "guard.db")
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE live_sessions (id INTEGER PRIMARY KEY, running BOOLEAN)")
    connection.commit()
    url = "sqlite:///" + path

    verify.section("Update-Wächter")
    verify(run(url)[0] == 0, "ohne Sitzung darf das Update laufen")

    connection.execute("INSERT INTO live_sessions (running) VALUES (0)")
    connection.commit()
    verify(run(url)[0] == 0, "eine beendete Sitzung hält das Update nicht auf")

    connection.execute("INSERT INTO live_sessions (running) VALUES (1)")
    connection.commit()
    code, text = run(url)
    verify(code == 75, "eine laufende Fahrt verschiebt das Update (Exit-Code 75)")
    verify("postponed" in text, "und sagt im Log warum")
    connection.close()

    verify(run("sqlite:///" + os.path.join(folder, "gibt-es-nicht", "x.db"))[0] == 0,
           "ist die Datenbank nicht lesbar, geht das Update trotzdem durch")
    verify(subprocess.run([sys.executable, GUARD], capture_output=True,
                          env={k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
                          ).returncode == 0,
           "ohne DATABASE_URL geht das Update durch")
    return verify.balance()


if __name__ == "__main__":
    sys.exit(main())
