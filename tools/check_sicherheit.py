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


def teil_live() -> None:
    """Die Live-Endpunkte verlangen die Anmeldung - Bug-Scan #49, Punkt 1.

    Vorher waren Lesen, Messpunkte und WebSocket ohne Anmeldung erreichbar, und
    der einzige "Schlüssel" war eine fortlaufende Sitzungs-ID.
    """
    pruefe.abschnitt("Live-Endpunkte")
    security._treffer.clear()
    client = TestClient(app)
    token = client.post("/api/login", json={"passwort": PASSWORT}).json()["token"]
    kopf = {"X-Token": token}
    punkt = {"lat": 50.0, "lon": 10.0, "soc": 60}

    for methode, pfad, rumpf in (
            ("get", "/api/live/1", None),
            ("get", "/api/live/1/punkte", None),
            ("post", "/api/live/1/punkt", punkt),
            ("post", "/api/live/1/punkte", {"punkte": [punkt]})):
        antwort = getattr(client, methode)(pfad, **({"json": rumpf} if rumpf else {}))
        pruefe(antwort.status_code == 401,
               f"{methode.upper()} {pfad} ohne Anmeldung: 401", f"HTTP {antwort.status_code}")
        antwort = getattr(client, methode)(
            pfad, headers=kopf, **({"json": rumpf} if rumpf else {}))
        pruefe(antwort.status_code == 404,
               f"und mit Anmeldung geht es bis zur Sitzung (404 - es gibt keine)",
               f"HTTP {antwort.status_code}")

    # WebSocket: der Token kommt als erste Nachricht.
    def ws_ergebnis(erste):
        try:
            with client.websocket_connect("/api/live/1/ws") as ws:
                if erste is not None:
                    ws.send_text(erste)
                return ws.receive_json()
        except Exception as fehler:      # noqa: BLE001
            return type(fehler).__name__

    pruefe(ws_ergebnis('{"token": "' + token + '"}') == {"typ": "bereit"},
           "der WebSocket nimmt den Token als erste Nachricht an und meldet 'bereit'")
    pruefe(ws_ergebnis('{"token": "falsch"}') == "WebSocketDisconnect",
           "mit falschem Token wird er geschlossen")
    pruefe(ws_ergebnis("kein json") == "WebSocketDisconnect",
           "mit Unsinn als erster Nachricht ebenso")
    pruefe(ws_ergebnis('{"token": 5}') == "WebSocketDisconnect"
           and ws_ergebnis("[1]") == "WebSocketDisconnect",
           "und mit einem Token, der keine Zeichenkette ist")

    # /melden bleibt offen, aber nur mit gültigem Logger-Token - und wer zu
    # oft ein falsches schickt, wird gebremst.
    security._melden_fehler.clear()
    meldung = {"token": "gibt-es-nicht", "lat": 50, "lon": 10, "soc": 50}
    antwort = client.post("/api/live/melden", json=meldung)
    pruefe(antwort.status_code == 401,
           "/melden mit unbekanntem Logger-Token: 401", f"HTTP {antwort.status_code}")
    antwort = client.post("/api/live/melden",
                          json={"token": "gibt-es-nicht", "format": "gibt-es-nicht"})
    pruefe(antwort.status_code == 401,
           "das Token wird vor der Übersetzung geprüft - ein unbekanntes Format "
           "kostet ohne gültiges Token keine Rechenzeit", f"HTTP {antwort.status_code}")
    codes = [client.post("/api/live/melden", json=meldung).status_code
             for _ in range(security.MELDEN_FEHLER_MAX + 2)]
    pruefe(codes[-1] == 429 and codes[0] == 401,
           "nach zu vielen Fehlversuchen kommt 429 - vom allgemeinen Limit ist "
           "der Pfad ausgenommen, also braucht er ein eigenes", str(codes[-3:]))
    security._melden_fehler.clear()

    # Die Gegenstelle: Eine Oberfläche, die den Token nicht schickt, wäre nach
    # der Absicherung stumm - der WebSocket schlösse sich, `/punkt` gäbe 401.
    frontend = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "frontend")
    live = open(os.path.join(frontend, "live.js"), encoding="utf-8").read()
    obd = open(os.path.join(frontend, "obd.js"), encoding="utf-8").read()
    pruefe("steckdose.send(JSON.stringify({ token: K.token() }))" in live
           and 'daten.typ === "bereit"' in live,
           "die Live-Ansicht schickt den Token als erste WebSocket-Nachricht "
           "und gilt erst nach 'bereit' als verbunden")
    pruefe('"X-Token": joltToken() },\n      body: JSON.stringify(nutzlast)' in obd,
           "die Diagnoseseite meldet Punkte mit Anmeldung")


def main() -> int:
    teil_passwort()
    teil_limit()
    teil_live()
    return pruefe.bilanz()


if __name__ == "__main__":
    sys.exit(main())
