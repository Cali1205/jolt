#!/usr/bin/env python3
"""Prüft Anmeldung und Rate-Limit - `deps.py` und `security.py`.

Entstanden aus dem Bug-Scan #49: Drei Fehler dort fielen erst auf, als jemand
den Code las, weil bisher keine Prüfung die Härtung im Betrieb ansah - der
Prüflauf von `check_backend.py` läuft ausdrücklich ohne Passwort und mit
unbegrenzter Anfragezahl.

Ohne Netz, ohne Postgres, ohne API-Schlüssel:

    ./tools/check_sicherheit.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pruefen import Pruefung, anwendung_bereitstellen  # noqa: E402

anwendung_bereitstellen("sicherheit", datenbank=False)

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="jolt-sicherheit-"), "check.db")
# Ein Passwort mit Umlaut: genau das, woran compare_digest bisher scheiterte.
PASSWORT = "Gehäimnis-ü-123"
os.environ["APP_PASSWORT"] = PASSWORT
os.environ["RATE_LIMIT_PER_MIN"] = "5"

from fastapi.testclient import TestClient  # noqa: E402

from app import deps, security  # noqa: E402
from app.main import app  # noqa: E402

pruefe = Pruefung()


def teil_passwort() -> None:
    pruefe.abschnitt("Passwort vergleichen")
    pruefe(deps.passwort_pruefen(PASSWORT) is True,
           "das richtige Passwort wird angenommen, auch mit Umlaut - "
           "compare_digest auf str wirft bei Nicht-ASCII einen TypeError")
    pruefe(deps.passwort_pruefen("falsch") is False,
           "ein falsches wird abgelehnt")
    pruefe(deps.passwort_pruefen("Gehäimnis-ö-123") is False,
           "auch eines, das sich nur im Umlaut unterscheidet")
    pruefe(deps.passwort_pruefen("") is False and deps.passwort_pruefen(None) is False,
           "und ein leeres oder fehlendes")

    client = TestClient(app)
    antwort = client.post("/api/login", json={"passwort": PASSWORT})
    pruefe(antwort.status_code == 200 and antwort.json().get("token"),
           "der Login mit Umlaut-Passwort liefert einen Token statt 500",
           f"HTTP {antwort.status_code}")
    antwort = client.post("/api/login", json={"passwort": "Gehäimnis-ö-123"})
    pruefe(antwort.status_code == 401,
           "ein falsches Passwort mit Umlaut ergibt 401, nicht 500",
           f"HTTP {antwort.status_code}")


def teil_limit() -> None:
    pruefe.abschnitt("Rate-Limit")
    client = TestClient(app)
    security._treffer.clear()
    codes = [client.get("/api/status").status_code for _ in range(8)]
    pruefe(codes[:5] == [200] * 5,
           "die ersten Anfragen innerhalb der Grenze gehen durch", str(codes))
    pruefe(all(c == 429 for c in codes[5:]),
           "danach kommt 429 - nicht 500 mit Traceback im Log, weil eine "
           "HTTPException in der Middleware an FastAPIs Handlern vorbeigeht",
           str(codes))
    antwort = client.get("/api/status")
    pruefe(antwort.status_code == 429 and "Zu viele" in antwort.text
           and antwort.headers.get("content-type", "").startswith("application/json"),
           "mit JSON-Meldung, wie der Rest der Schnittstelle")
    pruefe("X-Content-Type-Options" in antwort.headers,
           "und die Security-Header sitzen auch an der abgewiesenen Antwort")

    # Speicher: Schlüssel abgelaufener Absender dürfen nicht ewig bleiben.
    security._treffer.clear()
    for nr in range(50):
        security._zaehlen(security._treffer, f"10.0.0.{nr}", 60, 100)
    pruefe(len(security._treffer) == 50, "50 Absender werden gezählt")
    for warteschlange in security._treffer.values():
        warteschlange.clear()
        warteschlange.append(0.0)     # lange her
    security._zaehlen(security._treffer, "10.9.9.9", 60, 100,
                      aufraeumen_ab=0.0)
    pruefe(len(security._treffer) == 1,
           "abgelaufene Absender werden aus dem Speicher genommen - "
           "sonst wächst die Tabelle mit jeder neuen IP, die je kam",
           str(len(security._treffer)))


def main() -> int:
    teil_passwort()
    teil_limit()
    return pruefe.bilanz()


if __name__ == "__main__":
    sys.exit(main())
