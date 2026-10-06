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
os.environ["RATE_LIMIT_PER_MIN"] = "100000"

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
    security.GLOBAL_MAX = 5       # in den übrigen Abschnitten wäre das im Weg
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

    security.GLOBAL_MAX = 100000

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

    from app.routers import live as live_router
    echt = live_router._sitzung_existiert
    live_router._sitzung_existiert = lambda _id: True     # Sitzung 1 gibt es hier nicht
    try:
        pruefe(ws_ergebnis('{"token": "' + token + '"}') == {"typ": "bereit"},
               "der WebSocket nimmt den Token als erste Nachricht an und meldet 'bereit'")
    finally:
        live_router._sitzung_existiert = echt
    pruefe(ws_ergebnis('{"token": "' + token + '"}') == "WebSocketDisconnect",
           "eine Sitzung, die es nicht gibt, wird nicht in den Verteiler aufgenommen "
           "(Bug-Scan #55, Punkt 7)")
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


class _Anfrage:
    """Gerade genug Request für security.client_ip."""

    def __init__(self, peer, xff=None):
        self.client = type("C", (), {"host": peer})()
        self.headers = {"x-forwarded-for": xff} if xff is not None else {}


def teil_client_ip() -> None:
    pruefe.abschnitt("Client-Adresse hinter dem Proxy")
    alt = set(security.TRUSTED_PROXIES)
    security.TRUSTED_PROXIES.clear()
    security.TRUSTED_PROXIES.update({"172.18.0.2"})
    try:
        ip = security.client_ip
        pruefe(ip(_Anfrage("172.18.0.2", "6.6.6.6, 203.0.113.9")) == "203.0.113.9",
               "hinter einem vertrauten Proxy gilt der letzte Eintrag, den er "
               "angehängt hat - der erste stammt vom Client und ist frei wählbar")
        pruefe(ip(_Anfrage("172.18.0.2", "1.1.1.1")) != ip(_Anfrage("172.18.0.2", "2.2.2.2")),
               "zwei verschiedene Absender behalten verschiedene Zähler")
        pruefe(ip(_Anfrage("172.18.0.2", "203.0.113.9, 172.18.0.2")) == "203.0.113.9",
               "ein vertrauter Proxy am Ende der Kette wird übersprungen")
        pruefe(ip(_Anfrage("198.51.100.7", "6.6.6.6")) == "198.51.100.7",
               "von einem Absender, der kein Proxy ist, wird der Header ignoriert")
        pruefe(ip(_Anfrage("172.18.0.2")) == "172.18.0.2",
               "ohne Header gilt die Verbindungsadresse")
    finally:
        security.TRUSTED_PROXIES.clear()
        security.TRUSTED_PROXIES.update(alt)


def teil_eingaben() -> None:
    """Grenzen bei Messpunkten und Fahrzeugen - Bug-Scan #49, Punkte 7 und 9."""
    pruefe.abschnitt("Eingaben begrenzen")
    security._treffer.clear()
    client = TestClient(app)
    kopf = {"X-Token": client.post("/api/login",
                                   json={"passwort": PASSWORT}).json()["token"]}
    gut = {"name": "Prüf-Auto", "akku_brutto_kwh": 80, "akku_netto_kwh": 77,
           "leermasse_kg": 2400, "c_w": 0.29, "stirnflaeche_m2": 2.9,
           "p_neben_w": 500, "max_ladeleistung_kw": 200,
           "strompreise": [{"muster": "Ionity", "eur_kwh": 0.39}],
           "ladekurve": [[0, 100], [50, 150], [80, 60]]}
    antwort = client.post("/api/fahrzeuge", json=gut, headers=kopf)
    pruefe(antwort.status_code == 200,
           "ein ordentliches Fahrzeug wird angelegt", f"HTTP {antwort.status_code} {antwort.text[:120]}")
    pruefe(antwort.json()["strompreise"] == [{"muster": "Ionity", "eur_kwh": 0.39}],
           "und der Strompreis kommt so zurück, wie er hineinging")

    for feld, wert in (("c_w", 0), ("c_w", -1), ("leermasse_kg", 0),
                       ("leermasse_kg", -5), ("stirnflaeche_m2", 0),
                       ("p_neben_w", -1), ("max_ladeleistung_kw", 0),
                       ("akku_brutto_kwh", 1e9), ("name", ""),
                       ("steckertyp", "x" * 500)):
        antwort = client.post("/api/fahrzeuge", json={**gut, feld: wert}, headers=kopf)
        pruefe(antwort.status_code == 422,
               f"{feld} = {wert if not isinstance(wert, str) else repr(wert[:8])} "
               "wird abgelehnt - Null im Fahrwiderstand ergab Division durch null",
               f"HTTP {antwort.status_code}")
    for text, aenderung in (
            ("eine Ladekurve mit negativer Leistung", {"ladekurve": [[0, -5]]}),
            ("mit Ladestand über 100 %", {"ladekurve": [[150, 50]]}),
            ("mit tausend Punkten", {"ladekurve": [[i / 10, 50] for i in range(1000)]}),
            ("ein Strompreis ohne Zahl", {"strompreise": [{"muster": "x", "eur_kwh": "viel"}]}),
            ("ein negativer Strompreis", {"strompreise": [{"muster": "x", "eur_kwh": -1}]}),
            ("hundert Strompreise", {"strompreise": [{"muster": "x", "eur_kwh": 1}] * 100})):
        antwort = client.post("/api/fahrzeuge", json={**gut, **aenderung}, headers=kopf)
        pruefe(antwort.status_code == 422, f"{text} wird abgelehnt",
               f"HTTP {antwort.status_code}")
    antwort = client.post("/api/fahrzeuge", headers={**kopf, "Content-Type": "application/json"},
                          content=b'{"name":"x","akku_brutto_kwh":NaN,"akku_netto_kwh":1}')
    pruefe(antwort.status_code == 422, "NaN als Zahl wird abgelehnt - es verseucht jede Rechnung danach",
           f"HTTP {antwort.status_code}")

    # Messpunkte
    punkt = {"lat": 50.0, "lon": 10.0, "soc": 60}
    for text, aenderung in (("Tempo über 500 km/h", {"tempo_kmh": 900}),
                            ("negatives Tempo", {"tempo_kmh": -3}),
                            ("Aussentemperatur von 400 Grad", {"aussentemp_c": 400}),
                            ("hundert Rohwerte", {"rohwerte": {f"k{i}": 1 for i in range(100)}}),
                            ("ein Rohwert von 20 000 Zeichen", {"rohwerte": {"a": "x" * 20000}})):
        antwort = client.post("/api/live/1/punkt", json={**punkt, **aenderung}, headers=kopf)
        pruefe(antwort.status_code == 422, f"Messpunkt mit {text} wird abgelehnt",
               f"HTTP {antwort.status_code}")
    antwort = client.post("/api/live/1/punkt", headers=kopf,
                          json={**punkt, "tempo_kmh": 130, "aussentemp_c": -5,
                                "rohwerte": {"km_stand": 1234, "soc_roh": 150}})
    pruefe(antwort.status_code == 404,
           "ein ordentlicher Messpunkt kommt durch die Prüfung (404: keine Sitzung)",
           f"HTTP {antwort.status_code}")


def teil_eventloop() -> None:
    """Die Handler, die synchron rechnen, dürfen den Event-Loop nicht anhalten.

    Eine Verhaltensprüfung wäre hier ein Zeitmesser auf einem Rechner, dessen
    Takt niemand kennt - deshalb die Bauart: Alle `async def`-Handler reichen
    ihre Datenbankarbeit an den Threadpool.
    """
    pruefe.abschnitt("Event-Loop frei halten")
    quelle = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                               "backend", "app", "routers", "live.py"),
                  encoding="utf-8").read()
    pruefe("await run_in_threadpool(\n        live_sitzung.messpunkt_aufnehmen" in quelle,
           "das Einsortieren eines Messpunkts läuft im Threadpool - ein Stapel "
           "von 500 Punkten hielt sonst jede andere Anfrage und jeden WebSocket an")
    import re
    for name in ("punkt_melden", "punkte_melden", "logger_melden", "simulieren"):
        rumpf = re.search(r"async def %s\(.*?(?=\n@router|\nclass |\Z)" % name,
                          quelle, re.S).group(0)
        pruefe("run_in_threadpool" in rumpf and "db.get(" not in rumpf
               and "db.query(" not in rumpf,
               f"{name} reicht seine Datenbankarbeit an den Threadpool")


def teil_55() -> None:
    """Bug-Scan #55: SSRF über den Push-Endpunkt, Kanal-Obergrenze, Simulation."""
    import asyncio
    import socket
    from app import push
    from app.live import kanal

    pruefe.abschnitt("Push-Endpunkt (SSRF)")
    antworten = {"push.example.org": "93.184.216.34", "intern.example": "10.0.0.5",
                 "lokal.example": "127.0.0.1", "meta.example": "169.254.169.254",
                 "db": "172.18.0.2"}
    echt = socket.getaddrinfo

    def falsch(host, port, *a, **k):
        if host in antworten:
            return [(2, 1, 6, "", (antworten[host], port))]
        if host.replace(".", "").isdigit():
            return [(2, 1, 6, "", (host, port))]
        raise socket.gaierror("unbekannt")
    socket.getaddrinfo = falsch
    try:
        for url, erwartet, text in (
                ("https://push.example.org/abo", True, "ein öffentlicher https-Dienst"),
                ("http://push.example.org/abo", False, "http statt https"),
                ("https://intern.example/abo", False, "ein Name, der auf 10.x zeigt"),
                ("https://lokal.example/abo", False, "ein Name, der auf Loopback zeigt"),
                ("https://169.254.169.254/latest", False, "die Cloud-Metadaten-Adresse"),
                ("https://db:5432/", False, "der Docker-Name der Datenbank"),
                ("https://nirgendwo.example/", False, "ein nicht auflösbarer Name"),
                ("https://u:p@push.example.org/", False, "Zugangsdaten in der Adresse"),
                ("ftp://push.example.org/", False, "ein anderes Schema")):
            pruefe(push.endpoint_erlaubt(url) is erwartet, text)
    finally:
        socket.getaddrinfo = echt

    pruefe.abschnitt("Kanal-Obergrenze")

    async def fuellen():
        kanal._verbindungen.clear()
        ergebnisse = [await kanal.anmelden(99, object())
                      for _ in range(kanal.MAX_JE_SITZUNG + 1)]
        kanal._verbindungen.clear()
        return ergebnisse
    ergebnisse = asyncio.run(fuellen())
    pruefe(all(ergebnisse[:-1]) and ergebnisse[-1] is False,
           "mehr als %d Zuschauer je Sitzung werden abgewiesen" % kanal.MAX_JE_SITZUNG)

    pruefe.abschnitt("Simulation nur einmal je Fahrt")
    quelle = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                               "backend", "app", "routers", "live.py"),
                  encoding="utf-8").read()
    pruefe("if sitzung_id in _simulationen" in quelle and "_task_halten(" in quelle,
           "ein zweiter Start für dieselbe Fahrt wird abgelehnt, und die Aufgabe "
           "wird festgehalten")


def main() -> int:
    teil_passwort()
    teil_client_ip()
    teil_eingaben()
    teil_eventloop()
    teil_limit()
    teil_live()
    teil_55()
    return pruefe.bilanz()


if __name__ == "__main__":
    sys.exit(main())
