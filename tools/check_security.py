#!/usr/bin/env python3
"""Checks login and rate limit - `deps.py` and `security.py`.

Born from bug scan #49: three errors there were only noticed when someone
read the code, because no check had looked at the hardening in operation so
far - the run of `check_backend.py` explicitly runs without a password and
with an unlimited number of requests.

Without network, without Postgres, without API key:

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
# A password with an umlaut: exactly what compare_digest used to fail on.
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

    # Bug scan #127: successful logins must not use up the guessing limit.
    security._login_hit.clear()
    codes = [client.post("/api/login", json={"password": PASSWORD}).status_code
             for _ in range(security.LOGIN_MAX + 3)]
    verify(codes == [200] * len(codes),
           "erfolgreiche Anmeldungen zählen nicht gegen das Limit", str(codes))
    codes = [client.post("/api/login", json={"password": "falsch"}).status_code
             for _ in range(security.LOGIN_MAX + 1)]
    verify(codes[-1] == 429 and codes[0] == 401,
           "falsche Passwörter schon - danach 429", str(codes))
    security._login_hit.clear()

    # Bug scan #135, point 1: the attempt is reserved *before* the password
    # check, so parallel requests cannot all slip through.
    class _R:
        client = type("c", (), {"host": "9.9.9.9"})()
        headers: dict = {}
    security._login_hit.clear()
    passed = 0
    for _ in range(security.LOGIN_MAX + 5):
        try:
            security.login_limit(_R)
            passed += 1
        except Exception:
            pass
    verify(passed == security.LOGIN_MAX,
           "Reservierung: ohne Antwort dazwischen kommen nur LOGIN_MAX Versuche durch", str(passed))
    security._login_hit.clear()
    security.login_limit(_R)
    security.release_login(_R)
    verify(len(security._login_hit["9.9.9.9"]) == 0,
           "ein erfolgreicher Login gibt den Platz zurueck")
    security._login_hit.clear()

    # Point 3: tokens are stored as hashes.
    from app import models
    from app.database import SessionLocal
    token = client.post("/api/login", json={"password": PASSWORD}).json()["token"]
    db = SessionLocal()
    try:
        rows = [r.token for r in db.query(models.AuthSession).all()]
    finally:
        db.close()
    verify(token not in rows and security.token_hash(token) in rows,
           "die Datenbank enthaelt nur den Hash des Sitzungs-Tokens")
    verify(client.get("/api/vehicles", headers={"X-Token": token}).status_code == 200,
           "und die Anmeldung mit dem Original-Token funktioniert weiter")


def part_limit() -> None:
    verify.section("Rate-Limit")
    client = TestClient(app)
    security._hit.clear()
    security.GLOBAL_MAX = 5       # in the remaining sections this would be in the way
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
    verify("geolocation=(self)" in response.headers.get("Permissions-Policy", "")
           and "camera=()" in response.headers.get("Permissions-Policy", ""),
           "die Seite darf nur Standort und Bluetooth vom Geraet verlangen")

    # Starlette rebuilt the URL from the Host header: "x/y" moved the path, and
    # the checks that look at the path (limit, body size) were skipped.
    security._hit.clear()
    forged = {"Host": "x/y"}
    codes = [client.get("/api/status", headers=forged).status_code for _ in range(8)]
    verify(codes[:5] == [200] * 5 and all(c == 429 for c in codes[5:]),
           "ein gefaelschter Host-Header umgeht das Anfrage-Limit nicht", str(codes))
    big = "a" * (security.MAX_BODY_BYTES + 10)
    refused = client.post("/api/login", content=big,
                          headers={"Host": "x/y", "Content-Type": "application/json"})
    verify(refused.status_code == 413,
           "und auch nicht die Groessengrenze fuer Anfragen", str(refused.status_code))
    security._hit.clear()

    security.GLOBAL_MAX = 100000

    # Memory: keys of expired senders must not stay forever.
    security._hit.clear()
    for nr in range(50):
        security._count(security._hit, f"10.0.0.{nr}", 60, 100)
    verify(len(security._hit) == 50, "50 Absender werden gezählt")
    for queue in security._hit.values():
        queue.clear()
        queue.append(0.0)     # long ago
    security._count(security._hit, "10.9.9.9", 60, 100,
                      cleanup_from=0.0)
    verify(len(security._hit) == 1,
           "abgelaufene Absender werden aus dem Speicher genommen - "
           "sonst wächst die Tabelle mit jeder neuen IP, die je kam",
           str(len(security._hit)))


def part_live() -> None:
    """The live endpoints require login - bug scan #49, point 1.

    Before, reading, measurement points and WebSocket were reachable without
    login, and the only "key" was a sequential session ID.
    """
    verify.section("Live-Endpunkte")
    security._hit.clear()
    client = TestClient(app)
    token = client.post("/api/login", json={"password": PASSWORD}).json()["token"]
    header = {"X-Token": token}
    point = {"lat": 50.0, "lon": 10.0, "soc": 60}

    for method, fs_path, body in (
            ("get", "/api/live/1", None),
            ("get", "/api/live/1/points", None),
            ("post", "/api/live/1/point", point),
            ("post", "/api/live/1/points", {"points": [point]})):
        response = getattr(client, method)(fs_path, **({"json": body} if body else {}))
        verify(response.status_code == 401,
               f"{method.upper()} {fs_path} ohne Anmeldung: 401", f"HTTP {response.status_code}")
        response = getattr(client, method)(
            fs_path, headers=header, **({"json": body} if body else {}))
        verify(response.status_code == 404,
               f"und mit Anmeldung geht es bis zur Sitzung (404 - es gibt keine)",
               f"HTTP {response.status_code}")

    # WebSocket: the token arrives as the first message.
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
    live_router._session_exists = lambda _id: True     # session 1 does not exist here
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

    # /report stays open, but only with a valid logger token - and whoever sends
    # a wrong one too often gets throttled.
    security._report_error.clear()
    report = {"token": "gibt-es-nicht", "lat": 50, "lon": 10, "soc": 50}
    response = client.post("/api/live/report", json=report)
    verify(response.status_code == 401,
           "/report mit unbekanntem Logger-Token: 401", f"HTTP {response.status_code}")
    response = client.post("/api/live/report",
                          json={"token": "gibt-es-nicht", "format": "gibt-es-nicht"})
    verify(response.status_code == 401,
           "das Token wird vor der Übersetzung geprüft - ein unbekanntes Format "
           "kostet ohne gültiges Token keine Rechenzeit", f"HTTP {response.status_code}")
    codes = [client.post("/api/live/report", json=report).status_code
             for _ in range(security.REPORT_ERROR_MAX + 2)]
    verify(codes[-1] == 429 and codes[0] == 401,
           "nach zu vielen Fehlversuchen kommt 429 - vom allgemeinen Limit ist "
           "der Pfad ausgenommen, also braucht er ein eigenes", str(codes[-3:]))
    security._report_error.clear()

    # The other end: a UI that does not send the token would be mute after the
    # hardening - the WebSocket would close, `/point` would give 401.
    frontend = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "frontend")
    live = open(os.path.join(frontend, "live.js"), encoding="utf-8").read()
    obd = open(os.path.join(frontend, "obd.js"), encoding="utf-8").read()
    verify("socket.send(JSON.stringify({ token: K.token() }))" in live
           and 'message.kind === "bereit"' in live,
           "die Live-Ansicht schickt den Token als erste WebSocket-Nachricht "
           "und gilt erst nach 'bereit' als verbunden")
    verify('"X-Token": joltToken() },\n      body: JSON.stringify(payload)' in obd,
           "die Diagnoseseite meldet Punkte mit Anmeldung")


class _Request:
    """Just enough of a request for security.client_ip."""

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
        security.TRUSTED_PROXIES.clear()
        security.TRUSTED_PROXIES.update({"172.24.0.0/16", "nonsense"})
        verify(ip(_Request("172.24.0.5", "6.6.6.6, 203.0.113.9")) == "203.0.113.9",
               "ein Netz als Eintrag (Tunnel-Container mit wechselnder Adresse) gilt als vertraut")
        verify(ip(_Request("172.24.0.5", "203.0.113.9, 172.24.0.77")) == "203.0.113.9",
               "auch am Ende der Kette wird ein Teilnehmer des Netzes uebersprungen")
        verify(ip(_Request("172.25.0.5", "6.6.6.6")) == "172.25.0.5",
               "ein Absender ausserhalb des Netzes wird nicht vertraut")
        verify(ip(_Request("2001:db8::1", "6.6.6.6")) == "2001:db8::1",
               "eine IPv6-Adresse bleibt gegen ein IPv4-Netz unvertraut - und ein kaputter Eintrag stoert nicht")
    finally:
        security.TRUSTED_PROXIES.clear()
        security.TRUSTED_PROXIES.update(old)


def part_inputs() -> None:
    """Limits on measurement points and vehicles - bug scan #49, points 7 and 9."""
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
    response = client.post("/api/vehicles", json=good, headers=header)
    verify(response.status_code == 200,
           "ein ordentliches Fahrzeug wird angelegt", f"HTTP {response.status_code} {response.text[:120]}")
    verify(response.json()["electricity_prices"] == [{"pattern": "Ionity", "eur_kwh": 0.39}],
           "und der Strompreis kommt so zurück, wie er hineinging")

    for field, val in (("c_w", 0), ("c_w", -1), ("curb_mass_kg", 0),
                       ("curb_mass_kg", -5), ("frontal_area_m2", 0),
                       ("p_aux_w", -1), ("max_charge_power_kw", 0),
                       ("battery_gross_kwh", 1e9), ("name", ""),
                       ("connector_type", "x" * 500)):
        response = client.post("/api/vehicles", json={**good, field: val}, headers=header)
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
        response = client.post("/api/vehicles", json={**good, **change}, headers=header)
        verify(response.status_code == 422, f"{text} wird abgelehnt",
               f"HTTP {response.status_code}")
    response = client.post("/api/vehicles", headers={**header, "Content-Type": "application/json"},
                          content=b'{"name":"x","battery_gross_kwh":NaN,"battery_net_kwh":1}')
    verify(response.status_code == 422, "NaN als Zahl wird abgelehnt - es verseucht jede Rechnung danach",
           f"HTTP {response.status_code}")

    # Measurement points
    point = {"lat": 50.0, "lon": 10.0, "soc": 60}
    for text, change in (("Tempo über 500 km/h", {"speed_kmh": 900}),
                            ("negatives Tempo", {"speed_kmh": -3}),
                            ("Aussentemperatur von 400 Grad", {"outside_temp_c": 400}),
                            ("dreihundert Rohwerte", {"raw_values": {f"k{i}": 1 for i in range(300)}}),
                            ("ein Rohwert von 30 000 Zeichen", {"raw_values": {"a": "x" * 30000}})):
        response = client.post("/api/live/1/point", json={**point, **change}, headers=header)
        verify(response.status_code == 422, f"Messpunkt mit {text} wird abgelehnt",
               f"HTTP {response.status_code}")
    response = client.post("/api/live/1/point", headers=header,
                          json={**point, "speed_kmh": 130, "outside_temp_c": -5,
                                "raw_values": {"odometer_km": 1234, "soc_raw": 150}})
    verify(response.status_code == 404,
           "ein ordentlicher Messpunkt kommt durch die Prüfung (404: keine Sitzung)",
           f"HTTP {response.status_code}")


def part_eventloop() -> None:
    """Handlers that compute synchronously must not stop the event loop.

    A behavioural check here would be a stopwatch on a machine whose clock
    speed nobody knows - hence the structural approach: all `async def`
    handlers hand their database work to the thread pool.
    """
    verify.section("Event-Loop frei halten")
    source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                               "backend", "app", "routers", "live.py"),
                  encoding="utf-8").read()
    verify("async with _session_lock(session.id):" in source
           and "await run_in_threadpool(\n            _record_locked" in source
           and "live_session.record_sample(" in source,
           "das Einsortieren eines Messpunkts läuft im Threadpool - ein Stapel "
           "von 500 Punkten hielt sonst jede andere Anfrage und jeden WebSocket an")
    from app.routers import live as live_router
    import asyncio as _aio

    async def same_lock():
        lock = live_router._session_lock(4711)
        return (live_router._session_lock(4711) is lock
                and live_router._session_lock(4712) is not lock)
    verify(_aio.run(same_lock()),
           "je Sitzung gibt es genau eine Sperre, verschiedene Sitzungen teilen sie nicht")
    import re
    for name in ("report_point", "report_points", "report_logger", "simulate"):
        body = re.search(r"async def %s\(.*?(?=\n@router|\nclass |\Z)" % name,
                          source, re.S).group(0)
        verify("run_in_threadpool" in body and "db.get(" not in body
               and "db.query(" not in body,
               f"{name} reicht seine Datenbankarbeit an den Threadpool")


def part_55() -> None:
    """Bug scan #55: SSRF via the push endpoint, channel cap, simulation."""
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


def part_92() -> None:
    """Bug scan #92: rate limit only spares the high-frequency live paths."""
    from app import security
    verify.section("Rate-Limit-Ausnahme nur für Messpfade")
    for path in ("/api/live/report", "/api/live/5/point", "/api/live/5/points"):
        verify(security.is_exempt(path), "%s ist ausgenommen" % path)
    for path in ("/api/live/start/1", "/api/live/5/end", "/api/live/5",
                 "/api/live/5/simulate", "/api/vehicles"):
        verify(not security.is_exempt(path), "%s unterliegt dem Limit" % path)
    verify(not security.is_exempt("/api/live/5/points", "GET"),
           "Auslesen der Punkte (GET) unterliegt dem Limit")


def part_122() -> None:
    """Bug scan #122: measurement paths limited, body rules, WebSocket, search text."""
    verify.section("Messpfade und Anfragegröße")
    client = TestClient(app)
    security._points_hit.clear()
    old = security.POINTS_MAX
    security.POINTS_MAX = 3
    codes = [client.post("/api/live/report", json={}).status_code for _ in range(5)]
    verify(codes[3:] == [429, 429] and 429 not in codes[:3],
           "die Messpfade haben ein eigenes, großzügiges Limit", str(codes))
    security.POINTS_MAX = old
    security._points_hit.clear()

    response = client.post("/api/live/report", content=b"{}",
                           headers={"content-length": "abc"})
    verify(response.status_code in (400, 422),
           "eine unlesbare Content-Length wird nicht als 0 behandelt",
           f"HTTP {response.status_code}")
    response = client.post("/api/live/report", content=iter([b"{}"]))
    verify(response.status_code == 411,
           "ein Body ohne Längenangabe (chunked) wird abgewiesen",
           f"HTTP {response.status_code}")

    security._ws_hit.clear()
    old = security.WS_MAX
    security.WS_MAX = 2
    ok = [security.websocket_allowed(_Request("10.1.1.1")) for _ in range(4)]
    verify(ok == [True, True, False, False],
           "neue WebSockets je IP und Minute sind begrenzt", str(ok))
    security.WS_MAX = old
    security._ws_hit.clear()

    security._hit.clear()
    header = {"X-Token": client.post("/api/login",
                                   json={"password": PASSWORD}).json()["token"]}
    response = client.get("/api/places", params={"text": "x" * 500}, headers=header)
    verify(response.status_code == 422, "ein überlanger Suchtext wird abgelehnt",
           f"HTTP {response.status_code}")


def main() -> int:
    part_password()
    part_client_ip()
    part_inputs()
    part_eventloop()
    part_limit()
    part_live()
    part_55()
    part_92()
    part_122()
    return verify.balance()


if __name__ == "__main__":
    sys.exit(main())
