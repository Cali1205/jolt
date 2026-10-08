#!/usr/bin/env python3
"""Prüft Anmeldung und Rate-Limit - `deps.py` und `security.py`.

Entstanden aus dem Bug-Scan #49: Drei Fehler dort fielen erst auf, als jemand
den Code las, weil bisher keine Prüfung die Härtung im Betrieb ansah - der
Prüflauf von `check_backend.py` läuft ausdrücklich ohne Passwort und mit
unbegrenzter Anfragezahl.

Ohne Netz, ohne Postgres, ohne API-Schlüssel:

    ./tools/check_security.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from examine import Check, application_provide  # noqa: E402

application_provide("sicherheit", db_name=False)

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="jolt-sicherheit-"), "check.db")
# Ein Passwort mit Umlaut: genau das, woran compare_digest bisher scheiterte.
PASSWORD = "Gehäimnis-ü-123"
os.environ["APP_PASSWORT"] = PASSWORD
os.environ["RATE_LIMIT_PER_MIN"] = "100000"

from fastapi.testclient import TestClient  # noqa: E402

from app import deps, security  # noqa: E402
from app.main import app  # noqa: E402

verify = Check()


def part_password() -> None:
    verify.section("Passwort vergleichen")
    verify(deps.examine_password(PASSWORD) is True,
           "das richtige Passwort wird angenommen, auch mit Umlaut - "
           "compare_digest auf str wirft bei Nicht-ASCII einen TypeError")
    verify(deps.examine_password("falsch") is False,
           "ein falsches wird abgelehnt")
    verify(deps.examine_password("Gehäimnis-ö-123") is False,
           "auch eines, das sich nur im Umlaut unterscheidet")
    verify(deps.examine_password("") is False and deps.examine_password(None) is False,
           "und ein leeres oder fehlendes")

    client = TestClient(app)
    response = client.post("/api/login", json={"password": PASSWORD})
    verify(response.status_code == 200 and response.json().get("token"),
           "der Login mit Umlaut-Passwort liefert einen Token statt 500",
           f"HTTP {response.status_code}")
    response = client.post("/api/login", json={"password": "Gehäimnis-ö-123"})
    verify(response.status_code == 401,
           "ein falsches Passwort mit Umlaut ergibt 401, nicht 500",
           f"HTTP {response.status_code}")


def part_limit() -> None:
    verify.section("Rate-Limit")
    client = TestClient(app)
    security._hit.clear()
    security.GLOBAL_MAX = 5       # in den übrigen Abschnitten wäre das im Weg
    codes = [client.get("/api/status").status_code for _ in range(8)]
    verify(codes[:5] == [200] * 5,
           "die ersten Anfragen innerhalb der Grenze gehen durch", str(codes))
    verify(all(c == 429 for c in codes[5:]),
           "danach kommt 429 - nicht 500 mit Traceback im Log, weil eine "
           "HTTPException in der Middleware an FastAPIs Handlern vorbeigeht",
           str(codes))
    response = client.get("/api/status")
    verify(response.status_code == 429 and "Zu viele" in response.text
           and response.headers.get("content-type", "").startswith("application/json"),
           "mit JSON-Meldung, wie der Rest der Schnittstelle")
    verify("X-Content-Type-Options" in response.headers,
           "und die Security-Header sitzen auch an der abgewiesenen Antwort")

    security.GLOBAL_MAX = 100000

    # Speicher: Schlüssel abgelaufener Absender dürfen nicht ewig bleiben.
    security._hit.clear()
    for nr in range(50):
        security._count(security._hit, f"10.0.0.{nr}", 60, 100)
    verify(len(security._hit) == 50, "50 Absender werden gezählt")
    for queue in security._hit.values():
        queue.clear()
        queue.append(0.0)     # lange her
    security._count(security._hit, "10.9.9.9", 60, 100,
                      cleanup_from=0.0)
    verify(len(security._hit) == 1,
           "abgelaufene Absender werden aus dem Speicher genommen - "
           "sonst wächst die Tabelle mit jeder neuen IP, die je kam",
           str(len(security._hit)))


def part_live() -> None:
    """Die Live-Endpunkte verlangen die Anmeldung - Bug-Scan #49, Punkt 1.

    Vorher waren Lesen, Messpunkte und WebSocket ohne Anmeldung erreichbar, und
    der einzige "Schlüssel" war eine fortlaufende Sitzungs-ID.
    """
    verify.section("Live-Endpunkte")
    security._hit.clear()
    client = TestClient(app)
    token = client.post("/api/login", json={"password": PASSWORD}).json()["token"]
    header = {"X-Token": token}
    point = {"lat": 50.0, "lon": 10.0, "soc": 60}

    for method, fs_path, body in (
            ("get", "/api/live/1", None),
            ("get", "/api/live/1/punkte", None),
            ("post", "/api/live/1/punkt", point),
            ("post", "/api/live/1/punkte", {"points": [point]})):
        response = getattr(client, method)(fs_path, **({"json": body} if body else {}))
        verify(response.status_code == 401,
               f"{method.upper()} {fs_path} ohne Anmeldung: 401", f"HTTP {response.status_code}")
        response = getattr(client, method)(
            fs_path, headers=header, **({"json": body} if body else {}))
        verify(response.status_code == 404,
               f"und mit Anmeldung geht es bis zur Sitzung (404 - es gibt keine)",
               f"HTTP {response.status_code}")

    # WebSocket: der Token kommt als erste Nachricht.
    def ws_result(first_item):
        try:
            with client.websocket_connect("/api/live/1/ws") as ws:
                if first_item is not None:
                    ws.send_text(first_item)
                return ws.receive_json()
        except Exception as failure:      # noqa: BLE001
            return type(failure).__name__

    from app.routers import live as live_router
    real = live_router._session_exists
    live_router._session_exists = lambda _id: True     # Sitzung 1 gibt es hier nicht
    try:
        verify(ws_result('{"token": "' + token + '"}') == {"kind": "bereit"},
               "der WebSocket nimmt den Token als erste Nachricht an und meldet 'bereit'")
    finally:
        live_router._session_exists = real
    verify(ws_result('{"token": "' + token + '"}') == "WebSocketDisconnect",
           "eine Sitzung, die es nicht gibt, wird nicht in den Verteiler aufgenommen "
           "(Bug-Scan #55, Punkt 7)")
    verify(ws_result('{"token": "falsch"}') == "WebSocketDisconnect",
           "mit falschem Token wird er geschlossen")
    verify(ws_result("kein json") == "WebSocketDisconnect",
           "mit Unsinn als erster Nachricht ebenso")
    verify(ws_result('{"token": 5}') == "WebSocketDisconnect"
           and ws_result("[1]") == "WebSocketDisconnect",
           "und mit einem Token, der keine Zeichenkette ist")

    # /melden bleibt offen, aber nur mit gültigem Logger-Token - und wer zu
    # oft ein falsches schickt, wird gebremst.
    security._report_error.clear()
    report = {"token": "gibt-es-nicht", "lat": 50, "lon": 10, "soc": 50}
    response = client.post("/api/live/melden", json=report)
    verify(response.status_code == 401,
           "/melden mit unbekanntem Logger-Token: 401", f"HTTP {response.status_code}")
    response = client.post("/api/live/melden",
                          json={"token": "gibt-es-nicht", "format": "gibt-es-nicht"})
    verify(response.status_code == 401,
           "das Token wird vor der Übersetzung geprüft - ein unbekanntes Format "
           "kostet ohne gültiges Token keine Rechenzeit", f"HTTP {response.status_code}")
    codes = [client.post("/api/live/melden", json=report).status_code
             for _ in range(security.REPORT_ERROR_MAX + 2)]
    verify(codes[-1] == 429 and codes[0] == 401,
           "nach zu vielen Fehlversuchen kommt 429 - vom allgemeinen Limit ist "
           "der Pfad ausgenommen, also braucht er ein eigenes", str(codes[-3:]))
    security._report_error.clear()

    # Die Gegenstelle: Eine Oberfläche, die den Token nicht schickt, wäre nach
    # der Absicherung stumm - der WebSocket schlösse sich, `/punkt` gäbe 401.
    frontend = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "frontend")
    live = open(os.path.join(frontend, "live.js"), encoding="utf-8").read()
    obd = open(os.path.join(frontend, "obd.js"), encoding="utf-8").read()
    verify("socket.send(JSON.stringify({ token: K.token() }))" in live
           and 'records.kind === "bereit"' in live,
           "die Live-Ansicht schickt den Token als erste WebSocket-Nachricht "
           "und gilt erst nach 'bereit' als verbunden")
    verify('"X-Token": joltToken() },\n      body: JSON.stringify(payload)' in obd,
           "die Diagnoseseite meldet Punkte mit Anmeldung")


class _Request:
    """Gerade genug Request für security.client_ip."""

    def __init__(self, peer, xff=None):
        self.client = type("C", (), {"host": peer})()
        self.headers = {"x-forwarded-for": xff} if xff is not None else {}


def part_client_ip() -> None:
    verify.section("Client-Adresse hinter dem Proxy")
    old = set(security.TRUSTED_PROXIES)
    security.TRUSTED_PROXIES.clear()
    security.TRUSTED_PROXIES.update({"172.18.0.2"})
    try:
        ip = security.client_ip
        verify(ip(_Request("172.18.0.2", "6.6.6.6, 203.0.113.9")) == "203.0.113.9",
               "hinter einem vertrauten Proxy gilt der letzte Eintrag, den er "
               "angehängt hat - der erste stammt vom Client und ist frei wählbar")
        verify(ip(_Request("172.18.0.2", "1.1.1.1")) != ip(_Request("172.18.0.2", "2.2.2.2")),
               "zwei verschiedene Absender behalten verschiedene Zähler")
        verify(ip(_Request("172.18.0.2", "203.0.113.9, 172.18.0.2")) == "203.0.113.9",
               "ein vertrauter Proxy am Ende der Kette wird übersprungen")
        verify(ip(_Request("198.51.100.7", "6.6.6.6")) == "198.51.100.7",
               "von einem Absender, der kein Proxy ist, wird der Header ignoriert")
        verify(ip(_Request("172.18.0.2")) == "172.18.0.2",
               "ohne Header gilt die Verbindungsadresse")
    finally:
        security.TRUSTED_PROXIES.clear()
        security.TRUSTED_PROXIES.update(old)


def part_inputs() -> None:
    """Grenzen bei Messpunkten und Fahrzeugen - Bug-Scan #49, Punkte 7 und 9."""
    verify.section("Eingaben begrenzen")
    security._hit.clear()
    client = TestClient(app)
    header = {"X-Token": client.post("/api/login",
                                   json={"password": PASSWORD}).json()["token"]}
    good = {"name": "Prüf-Auto", "battery_gross_kwh": 80, "battery_net_kwh": 77,
           "curb_mass_kg": 2400, "c_w": 0.29, "frontal_area_m2": 2.9,
           "p_aux_w": 500, "max_charge_power_kw": 200,
           "electricity_prices": [{"pattern": "Ionity", "eur_kwh": 0.39}],
           "charge_curve": [[0, 100], [50, 150], [80, 60]]}
    response = client.post("/api/fahrzeuge", json=good, headers=header)
    verify(response.status_code == 200,
           "ein ordentliches Fahrzeug wird angelegt", f"HTTP {response.status_code} {response.text[:120]}")
    verify(response.json()["electricity_prices"] == [{"pattern": "Ionity", "eur_kwh": 0.39}],
           "und der Strompreis kommt so zurück, wie er hineinging")

    for field, val in (("c_w", 0), ("c_w", -1), ("curb_mass_kg", 0),
                       ("curb_mass_kg", -5), ("frontal_area_m2", 0),
                       ("p_aux_w", -1), ("max_charge_power_kw", 0),
                       ("battery_gross_kwh", 1e9), ("name", ""),
                       ("connector_type", "x" * 500)):
        response = client.post("/api/fahrzeuge", json={**good, field: val}, headers=header)
        verify(response.status_code == 422,
               f"{field} = {val if not isinstance(val, str) else repr(val[:8])} "
               "wird abgelehnt - Null im Fahrwiderstand ergab Division durch null",
               f"HTTP {response.status_code}")
    for text, change in (
            ("eine Ladekurve mit negativer Leistung", {"charge_curve": [[0, -5]]}),
            ("mit Ladestand über 100 %", {"charge_curve": [[150, 50]]}),
            ("mit tausend Punkten", {"charge_curve": [[i / 10, 50] for i in range(1000)]}),
            ("ein Strompreis ohne Zahl", {"electricity_prices": [{"pattern": "x", "eur_kwh": "viel"}]}),
            ("ein negativer Strompreis", {"electricity_prices": [{"pattern": "x", "eur_kwh": -1}]}),
            ("hundert Strompreise", {"electricity_prices": [{"pattern": "x", "eur_kwh": 1}] * 100})):
        response = client.post("/api/fahrzeuge", json={**good, **change}, headers=header)
        verify(response.status_code == 422, f"{text} wird abgelehnt",
               f"HTTP {response.status_code}")
    response = client.post("/api/fahrzeuge", headers={**header, "Content-Type": "application/json"},
                          content=b'{"name":"x","battery_gross_kwh":NaN,"battery_net_kwh":1}')
    verify(response.status_code == 422, "NaN als Zahl wird abgelehnt - es verseucht jede Rechnung danach",
           f"HTTP {response.status_code}")

    # Messpunkte
    point = {"lat": 50.0, "lon": 10.0, "soc": 60}
    for text, change in (("Tempo über 500 km/h", {"speed_kmh": 900}),
                            ("negatives Tempo", {"speed_kmh": -3}),
                            ("Aussentemperatur von 400 Grad", {"outside_temp_c": 400}),
                            ("hundert Rohwerte", {"raw_values": {f"k{i}": 1 for i in range(100)}}),
                            ("ein Rohwert von 20 000 Zeichen", {"raw_values": {"a": "x" * 20000}})):
        response = client.post("/api/live/1/punkt", json={**point, **change}, headers=header)
        verify(response.status_code == 422, f"Messpunkt mit {text} wird abgelehnt",
               f"HTTP {response.status_code}")
    response = client.post("/api/live/1/punkt", headers=header,
                          json={**point, "speed_kmh": 130, "outside_temp_c": -5,
                                "raw_values": {"odometer_km": 1234, "soc_raw": 150}})
    verify(response.status_code == 404,
           "ein ordentlicher Messpunkt kommt durch die Prüfung (404: keine Sitzung)",
           f"HTTP {response.status_code}")


def part_eventloop() -> None:
    """Die Handler, die synchron rechnen, dürfen den Event-Loop nicht anhalten.

    Eine Verhaltensprüfung wäre hier ein Zeitmesser auf einem Rechner, dessen
    Takt niemand kennt - deshalb die Bauart: Alle `async def`-Handler reichen
    ihre Datenbankarbeit an den Threadpool.
    """
    verify.section("Event-Loop frei halten")
    source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                               "backend", "app", "routers", "live.py"),
                  encoding="utf-8").read()
    verify("await run_in_threadpool(\n        live_session.record_sample" in source,
           "das Einsortieren eines Messpunkts läuft im Threadpool - ein Stapel "
           "von 500 Punkten hielt sonst jede andere Anfrage und jeden WebSocket an")
    import re
    for name in ("report_point", "report_points", "report_logger", "simulate"):
        body = re.search(r"async def %s\(.*?(?=\n@router|\nclass |\Z)" % name,
                          source, re.S).group(0)
        verify("run_in_threadpool" in body and "db.get(" not in body
               and "db.query(" not in body,
               f"{name} reicht seine Datenbankarbeit an den Threadpool")


def part_55() -> None:
    """Bug-Scan #55: SSRF über den Push-Endpunkt, Kanal-Obergrenze, Simulation."""
    import asyncio
    import socket
    from app import push
    from app.live import channel

    verify.section("Push-Endpunkt (SSRF)")
    responses = {"push.example.org": "93.184.216.34", "intern.example": "10.0.0.5",
                 "lokal.example": "127.0.0.1", "meta.example": "169.254.169.254",
                 "db": "172.18.0.2"}
    real = socket.getaddrinfo

    def wrong(host, port, *a, **k):
        if host in responses:
            return [(2, 1, 6, "", (responses[host], port))]
        if host.replace(".", "").isdigit():
            return [(2, 1, 6, "", (host, port))]
        raise socket.gaierror("unbekannt")
    socket.getaddrinfo = wrong
    try:
        for url, expected, text in (
                ("https://push.example.org/abo", True, "ein öffentlicher https-Dienst"),
                ("http://push.example.org/abo", False, "http statt https"),
                ("https://intern.example/abo", False, "ein Name, der auf 10.x zeigt"),
                ("https://lokal.example/abo", False, "ein Name, der auf Loopback zeigt"),
                ("https://169.254.169.254/latest", False, "die Cloud-Metadaten-Adresse"),
                ("https://db:5432/", False, "der Docker-Name der Datenbank"),
                ("https://nirgendwo.example/", False, "ein nicht auflösbarer Name"),
                ("https://u:p@push.example.org/", False, "Zugangsdaten in der Adresse"),
                ("ftp://push.example.org/", False, "ein anderes Schema")):
            verify(push.endpoint_allowed(url) is expected, text)
    finally:
        socket.getaddrinfo = real

    verify.section("Kanal-Obergrenze")

    async def fill():
        channel._connections.clear()
        results = [await channel.sign_in(99, object())
                      for _ in range(channel.MAX_PER_SESSION + 1)]
        channel._connections.clear()
        return results
    results = asyncio.run(fill())
    verify(all(results[:-1]) and results[-1] is False,
           "mehr als %d Zuschauer je Sitzung werden abgewiesen" % channel.MAX_PER_SESSION)

    verify.section("Simulation nur einmal je Fahrt")
    source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                               "backend", "app", "routers", "live.py"),
                  encoding="utf-8").read()
    verify("if session_id in _simulations" in source and "_task_hold(" in source,
           "ein zweiter Start für dieselbe Fahrt wird abgelehnt, und die Aufgabe "
           "wird festgehalten")


def main() -> int:
    part_password()
    part_client_ip()
    part_inputs()
    part_eventloop()
    part_limit()
    part_live()
    part_55()
    return verify.balance()


if __name__ == "__main__":
    sys.exit(main())
