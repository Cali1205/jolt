#!/usr/bin/env python3
"""Checks the VW EU Data Act portal module - `app/cloud/`.

The real portal needs a VW account and a car that reports, so none of this
talks to it. The data points come from a recorded shape
(tools/fixtures/vw_portal_points.json, synthetic VIN), the login runs
against a fake identity service and portal behind the real host names.

Without network, without Postgres:

    ./tools/check_vw_portal.py
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
from examine import Check, application_provide  # noqa: E402

application_provide("vw-portal")
for _name in ("VW_PORTAL_USER", "VW_PORTAL_PASSWORD", "VW_PORTAL_VIN",
              "VW_PORTAL_VEHICLE", "VW_PORTAL_INTERVAL_MIN"):
    os.environ.pop(_name, None)

import http.client  # noqa: E402
import io  # noqa: E402
import zipfile  # noqa: E402
from urllib.parse import parse_qsl, urlencode, urlsplit  # noqa: E402

import requests  # noqa: E402
import requests.adapters  # noqa: E402
import urllib3  # noqa: E402

from app.cloud import snapshot, vw_portal  # noqa: E402

verify = Check()

with open(os.path.join(TOOLS, "fixtures", "vw_portal_points.json"),
          encoding="utf-8") as _file:
    DATASET = json.load(_file)


def part_snapshot():
    verify.section("Datenpunkte -> Schnappschuss")
    points = snapshot.decode(DATASET["Data"])
    snap = snapshot.from_points(points)
    expected = {
        "soc_pct": 57, "range_km": 192, "odometer_km": 62067,
        "energy_kwh": 36.65, "capacity_kwh": 72.6,
        "batt_temp_min_c": 15.0, "batt_temp_max_c": 15.5,
        "charge_power_kw": 0, "charge_state": "not_ready",
        "target_soc_pct": 80, "locked": True, "climate_state": "off",
    }
    verify(snap is not None, "aus dem Datensatz entsteht ein Schnappschuss")
    if snap is None:
        return
    for name, value in expected.items():
        got = snap.values.get(name)
        verify(got == value and type(got) in (type(value), int, float)
               and isinstance(got, bool) == isinstance(value, bool),
               f"{name} = {value!r}", f"ist {got!r}")
    verify(set(snap.values) == set(expected),
           "nur bekannte Werte werden behalten (parking_brake, Türen fallen weg)",
           f"zusätzlich: {sorted(set(snap.values) - set(expected))}")
    verify(snap.captured_at == datetime(2026, 10, 20, 0, 17, 14, tzinfo=timezone.utc),
           "Stand ist car_captured_time, wann das Auto gemeldet hat",
           str(snap.captured_at))
    verify(snapshot.lookup(points, ("battery_level_HV.value",)).value == "57.0",
           "von zwei Einträgen derselben Kennung gewinnt der neuere, auch wenn er "
           "im Datensatz vorne steht")

    without_time = [p for p in points if p.name != snapshot.CAPTURED_AT]
    snap = snapshot.from_points(without_time)
    verify(snap is not None and snap.captured_at
           == datetime(2026, 10, 20, 0, 17, 13, tzinfo=timezone.utc),
           "ohne car_captured_time gilt der jüngste Zeitstempel eines Punktes",
           str(snap and snap.captured_at))

    verify(snapshot.from_points(snapshot.decode([
        {"key": "x", "dataFieldName": "parking_brake", "value": "true",
         "timestampUtc": "2026-10-20T00:17:13Z"}])) is None,
        "nur unbekannte Punkte: kein Schnappschuss")
    broken = snapshot.decode([
        {"key": "a", "dataFieldName": "battery_level_HV.value", "value": "n/a",
         "timestampUtc": "2026-10-20T00:17:13Z"},
        {"key": "b", "dataFieldName": "mileage.value", "value": "NaN",
         "timestampUtc": "2026-10-20T00:17:13Z"},
        {"key": "c", "dataFieldName": "locked", "value": "1",
         "timestampUtc": "2026-10-20T00:17:13Z"},
        {"key": "d", "dataFieldName": "max_temperature", "value": "16",
         "timestampUtc": "2026-10-20T00:17:13Z"}])
    snap = snapshot.from_points(broken)
    verify(snap is not None and snap.values == {"batt_temp_max_c": 16},
           "ein unlesbarer Wert fällt weg, statt den Rest mitzureissen",
           str(snap and snap.values))

    older = snapshot.decode([
        {"key": "a", "dataFieldName": "battery_level_HV.value", "value": "40",
         "timestampUtc": "2026-10-20T01:00:00Z"}])
    newer = snapshot.decode([
        {"key": snapshot.KEY_ENYAQ_SOC, "dataFieldName": "value", "value": "41",
         "timestampUtc": "2026-10-20T00:30:00Z"}])
    merged = snapshot.merge(snapshot.merge([], older, 1), newer, 2)
    snap = snapshot.from_points(merged)
    verify(snap.values["soc_pct"] == 41,
           "über mehrere Datensätze zählt der zuletzt gelieferte Wert, nicht die "
           "Reihenfolge der Kandidaten und nicht der Zeitstempel des Punktes",
           str(snap.values))

    parked = snapshot.decode([
        {"key": "k-config", "dataFieldName": "mileage.value", "value": "62067",
         "timestampUtc": "2026-10-20T05:44:20Z"}])
    stopped = snapshot.decode([
        {"key": "k-energy", "dataFieldName": "mileage.value", "value": "62071",
         "timestampUtc": "2026-10-20T05:59:42Z"}])
    merged = snapshot.merge(snapshot.merge([], parked, 1), stopped, 2)
    snap = snapshot.from_points(merged)
    verify(snap and snap.values.get("odometer_km") == 62071,
           "derselbe Feldname unter einem anderen Schlüssel: der neuere Wert "
           "gewinnt (Fahrt vom 20.10.: 62067 statt 62071 gespeichert)",
           str(snap and snap.values))


USER, PASSWORD, VIN = "fahrer@example.org", "geheim-123", DATASET["vin"]
AUTHORIZE = vw_portal.AUTHORIZE_URL
SIGNIN = vw_portal.IDENTITY + "/signin-service/v1/signin/test"
IDENTIFIER = vw_portal.IDENTITY + "/signin-service/v1/test/login/identifier"
AUTHENTICATE = vw_portal.IDENTITY + "/signin-service/v1/test/login/authenticate"
IDENTITY_CALLBACK = vw_portal.IDENTITY + "/oidc/v1/oauth/client/callback"
MARKETING = vw_portal.IDENTITY + "/consent/marketing/test"
PORTAL_CALLBACK = vw_portal.PORTAL + "/services/callbacklogin"
USER_PAGE = vw_portal.PORTAL + "/content/euda/de/de/user.html"
VEHICLES = vw_portal.PORTAL + "/proxy_api/consent/me/vehicles"
METADATA = vw_portal.PORTAL + f"/proxy_api/euda-apim/datarequest/vehicles/{VIN}/metadata/partial"
DELIVERY = vw_portal.PORTAL + f"/proxy_api/euda-apim/datadelivery/vehicles/{VIN}/ident-1"


def _zip(document: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("dataset.json", json.dumps(document))
    return buffer.getvalue()


OLDER = {"vin": VIN, "Data": [
    {"key": "00000000-0000-4000-8000-000000000001",
     "dataFieldName": "battery_level_HV.value", "value": "61.0",
     "timestampUtc": "2026-10-19T23:02:10Z"},
    {"key": "00000000-0000-4000-8000-000000000099",
     "dataFieldName": "climatisation_state", "value": "CLIMATISATION_STATE_OFF",
     "timestampUtc": "2026-10-19T23:02:10Z"}]}


class FakePortal(requests.adapters.BaseAdapter):
    """Identity service and portal behind their real host names, shaped after
    evcc's api_test.go and forms_test.go. Records every request."""

    def __init__(self, landing=USER_PAGE, marketing_callback=None):
        super().__init__()
        self.landing = landing
        self.marketing_callback = marketing_callback
        self.requests: list[tuple[str, str]] = []
        self.forms: dict[str, dict] = {}
        self.headers: dict[str, dict] = {}
        self.expire_next = 0
        self.downloaded: list[str] = []
        self.datasets = [
            {"name": "a_no_content_found.zip", "createdOn": "2026-10-20T00:30:00Z"},
            {"name": "ds2.zip", "createdOn": "2026-10-20T00:17:30Z"},
            {"name": "ds1.zip", "createdOn": "2026-10-19T23:02:30Z"},
        ]
        self.files = {"ds1.zip": _zip(OLDER), "ds2.zip": _zip(DATASET)}

    def send(self, request, **kwargs):
        parts = urlsplit(request.url)
        where = f"{parts.scheme}://{parts.netloc}{parts.path}"
        self.requests.append((request.method, where))
        self.headers[where] = dict(request.headers)
        if request.body:
            body = request.body.decode() if isinstance(request.body, bytes) else request.body
            self.forms[where] = dict(parse_qsl(body))
        cookie = request.headers.get("Cookie", "")
        status, headers, body = 200, {}, b""
        if where == AUTHORIZE:
            self.forms[where] = dict(parse_qsl(parts.query))
            status, headers = 302, {"Location": "/signin-service/v1/signin/test"}
        elif where == SIGNIN:
            body = (b'<html><form id="other" action="/nope"><input name="x" value="y">'
                    b'</form><form id="emailPasswordForm" action="/signin-service/v1/test/login/identifier">'
                    b'<input type="hidden" name="_csrf" value="csrf-1">'
                    b'<input type="hidden" name="relayState" value="relay-1">'
                    b'<input type="hidden" name="hmac" value="hmac-1">'
                    b'<input name="email"></form></html>')
        elif where == IDENTIFIER:
            body = (b"<script>\n  window._IDK = {\n    templateModel: "
                    b'{"hmac":"hmac-2","relayState":"relay-2","error":null,'
                    b'"postAction":"login/authenticate","identifierUrl":"login/identifier"},\n'
                    b"    disabledFeatures: {\n      isRTLEnabled: false,\n    },\n"
                    b"    currentLocale: 'de-DE',\n    csrf_parameterName: '_csrf',\n"
                    b"    csrf_token: 'csrf-2',\n  }\n</script>")
        elif where == AUTHENTICATE:
            location = PORTAL_CALLBACK
            if self.marketing_callback is not None:
                location = MARKETING + "?" + urlencode({"callback": self.marketing_callback})
            status, headers = 302, {"Location": location}
        elif where == IDENTITY_CALLBACK:
            status, headers = 302, {"Location": PORTAL_CALLBACK}
        elif where == PORTAL_CALLBACK:
            status, headers = 302, {"Location": self.landing,
                                    "Set-Cookie": "access_token=tok-1; Path=/; Secure; HttpOnly"}
        elif where.startswith(vw_portal.PORTAL + "/proxy_api/"):
            if "access_token=tok-1" not in cookie or self.expire_next:
                self.expire_next = max(0, self.expire_next - 1)
                status = 401
            elif where == VEHICLES:
                body = json.dumps({"vehicles": [{"vin": VIN, "nickName": "Buzz"}]}).encode()
            elif where == METADATA:
                body = b'{"Identifier": "ident-1"}'
            elif where == DELIVERY + "/list":
                body = json.dumps(self.datasets).encode()
            elif where == DELIVERY + "/download":
                self.downloaded.append(request.headers["filename"])
                body = self.files[request.headers["filename"]]
            else:
                status = 404
        return _response(request, status, headers, body)

    def close(self):
        pass


def _response(request, status, headers, body):
    message = http.client.HTTPMessage()
    for name, value in headers.items():
        message[name] = value
    original = type("Original", (), {"msg": message,
                                     "isclosed": lambda self: False})()
    raw = urllib3.HTTPResponse(body=io.BytesIO(body), headers=headers,
                               status=status, preload_content=False,
                               original_response=original)
    return requests.adapters.HTTPAdapter().build_response(request, raw)


def _login_error(fake) -> str:
    try:
        vw_portal.VwPortal(USER, PASSWORD, adapter=fake).vehicles()
    except vw_portal.PortalError as failure:
        return str(failure)
    return ""


def part_login():
    verify.section("Anmeldung und Datensätze gegen ein nachgebautes Portal")
    fake = FakePortal()
    portal = vw_portal.VwPortal(USER, PASSWORD, adapter=fake)
    vins = portal.vehicles()
    verify(vins == [VIN], "das Konto nennt sein Fahrzeug", str(vins))
    expected = [("GET", AUTHORIZE), ("GET", SIGNIN), ("POST", IDENTIFIER),
                ("POST", AUTHENTICATE), ("GET", PORTAL_CALLBACK), ("GET", VEHICLES)]
    verify(fake.requests == expected,
           "Reihenfolge: authorize, Anmeldeseite, E-Mail, Passwort, Rückruf, "
           "Fahrzeugliste - ohne Abruf der Startseite und der user.html",
           str(fake.requests))
    query = fake.forms[AUTHORIZE]
    verify(query.get("client_id", "").startswith("9b58543e")
           and query.get("state") == "de__en__VOLKSWAGEN_PASSENGER_CARS"
           and query.get("scope") == "openid cars profile"
           and query.get("redirect_uri") == vw_portal.PORTAL + "/login"
           and query.get("prompt") == "login" and len(query.get("nonce", "")) == 43,
           "der Login beginnt mit dem OIDC-Aufruf von evcc", str(query))
    verify(fake.forms[IDENTIFIER] == {"_csrf": "csrf-1", "relayState": "relay-1",
                                      "hmac": "hmac-1", "email": USER},
           "E-Mail-Schritt schickt die versteckten Felder des Formulars mit",
           str(fake.forms[IDENTIFIER]))
    verify(fake.forms[AUTHENTICATE] == {"_csrf": "csrf-2", "relayState": "relay-2",
                                        "hmac": "hmac-2", "email": USER,
                                        "password": PASSWORD},
           "Passwort-Schritt nimmt csrf/hmac aus window._IDK", str(fake.forms[AUTHENTICATE]))
    verify(fake.headers[IDENTIFIER].get("Referer") == SIGNIN
           and fake.headers[VEHICLES].get("User-Agent", "").startswith("jolt/")
           and fake.headers[VEHICLES].get("Accept") == "application/json",
           "Referer beim Formular, ehrlicher User-Agent, Accept JSON",
           str(fake.headers[VEHICLES]))

    found = portal.updates(VIN)
    verify([s.captured_at for s in found]
           == [datetime(2026, 10, 19, 23, 2, 10, tzinfo=timezone.utc),
               datetime(2026, 10, 20, 0, 17, 14, tzinfo=timezone.utc)]
           and [s.values.get("soc_pct") for s in found] == [61, 57],
           "zwei Meldungen in einem Abruf: zwei Momentaufnahmen, jede mit "
           "ihrer eigenen Zeit (ohne car_captured_time der jüngste Punkt)",
           str([(s.captured_at, s.values.get("soc_pct")) for s in found]))
    verify(len(found) == 2 and len(found[0].raw) == 2
           and len(found[1].raw) == len(snapshot.decode(DATASET["Data"]))
           and found[0].raw["00000000-0000-4000-8000-000000000001"][:2]
           == ["battery_level_HV.value", "61.0"],
           "die Rohpunkte sind die der jeweiligen Meldung, nicht der ganze "
           "fortgeschriebene Stand", str(found and found[0].raw))
    verify(found[1].values.get("climate_state") == "off",
           "Felder, die eine Meldung auslässt, behalten den letzten Wert",
           str(found[1].values))
    snap = portal.latest(VIN)
    verify([fake.headers[DELIVERY + "/list"].get("type")] == ["partial"],
           "die Liste wird als partial angefragt")
    verify(snap is not None and snap.values.get("soc_pct") == 57
           and snap.values.get("odometer_km") == 62067,
           "beide Datensätze werden geladen, der neuere gewinnt",
           str(snap and snap.values))
    verify(len(portal.points(VIN)) > len(snapshot.FIELDS),
           "die Rohpunkte bleiben für das Prüfwerkzeug abrufbar")
    count = len(fake.requests)
    again = portal.latest(VIN)
    verify(fake.requests[count:] == [("GET", DELIVERY + "/list")]
           and again == snap,
           "beim nächsten Abruf ohne neuen Datensatz wird nichts erneut geladen",
           str(fake.requests[count:]))
    verify(fake.downloaded == ["ds1.zip", "ds2.zip"],
           "geladen wird ältester zuerst, Platzhalter ohne Inhalt gar nicht",
           str(fake.downloaded))

    fake.expire_next = 1
    count = len(fake.requests)
    vins = portal.vehicles()
    verify(vins == [VIN] and fake.requests[count:]
           == [("GET", VEHICLES), ("GET", AUTHORIZE), ("GET", SIGNIN),
               ("POST", IDENTIFIER), ("POST", AUTHENTICATE),
               ("GET", PORTAL_CALLBACK), ("GET", VEHICLES)],
           "abgelaufene Sitzung (401): einmal neu anmelden, dann wiederholen",
           str(fake.requests[count:]))
    fake.expire_next = 5
    try:
        portal.vehicles()
        refused = ""
    except vw_portal.StatusError as failure:
        refused = str(failure)
    verify(refused == "vehicles: HTTP 401",
           "bleibt es bei 401, wird nur einmal neu angemeldet und dann aufgegeben",
           refused)

    fake = FakePortal(marketing_callback=IDENTITY_CALLBACK + "?scopes=openid cars")
    error = _login_error(fake)
    verify(error == "" and ("GET", IDENTITY_CALLBACK) in fake.requests
           and not any(u == MARKETING for _, u in fake.requests),
           "Marketing-Einwilligung: weiter über den Rückruf, ohne die Seite zu öffnen",
           error or str(fake.requests))

    for landing, needle, text in (
            ("https://attacker.example/content/euda/de/de/user.html",
             "unexpected login redirect host attacker.example",
             "eine Weiterleitung auf einen fremden Host wird verweigert"),
            (vw_portal.PORTAL + "/consent", "confirm the consent",
             "fehlende Einwilligung im Portal: klare Meldung"),
            (vw_portal.PORTAL + "/content/euda/de/de/notuser.html",
             "unexpected landing page", "falsche Zielseite wird nicht akzeptiert"),
            (PORTAL_CALLBACK, "stopped after 10 redirects",
             "Weiterleitungsschleife endet nach 10 Schritten")):
        fake = FakePortal(landing=landing)
        error = _login_error(fake)
        verify(needle in error and not any("attacker" in u for _, u in fake.requests),
               text, error)
        verify(USER not in error and PASSWORD not in error,
               "die Meldung enthält weder Benutzer noch Passwort", error)

    fake = FakePortal(marketing_callback="https://attacker.example/oidc/v1/oauth/client/callback")
    error = _login_error(fake)
    verify("unexpected marketing consent callback URL" in error,
           "ein Rückruf auf fremden Host wird nicht aufgerufen", error)


def part_storage():
    verify.section("Speichern")
    from app import models
    from app.cloud import poller
    from app.database import SessionLocal, migrate, seed_templates
    migrate()
    seed_templates()
    db = SessionLocal()
    try:
        vehicle_id = db.query(models.Vehicle.id).first()[0]
        snap = snapshot.from_points(snapshot.decode(DATASET["Data"]))
        first = poller.store(db, vehicle_id, "vw_portal", snap)
        second = poller.store(db, vehicle_id, "vw_portal", snap)
        rows = db.query(models.VehicleSnapshot).filter_by(vehicle_id=vehicle_id).all()
        verify(first and not second and len(rows) == 1,
               "derselbe Bericht zweimal: eine Zeile", f"{first}, {second}, {len(rows)}")
        verify(rows[0].raw and len(rows[0].raw) == len(snapshot.decode(DATASET["Data"])),
               "alle Rohpunkte der Meldung werden mitgespeichert",
               str(rows[0].raw and len(rows[0].raw)))
        verify(rows[0].captured_at == datetime(2026, 10, 20, 0, 17, 14)
               and rows[0].values["soc_pct"] == 57,
               "gespeichert als UTC ohne Zone, mit den Werten",
               f"{rows[0].captured_at} {rows[0].values}")
        newer = snapshot.Snapshot(
            captured_at=datetime(2026, 10, 20, 2, 17, tzinfo=timezone.utc),
            values={**snap.values, "soc_pct": 55})
        verify(poller.store(db, vehicle_id, "vw_portal", newer)
               and db.query(models.VehicleSnapshot).filter_by(
                   vehicle_id=vehicle_id).count() == 2,
               "ein neuerer Bericht: zweite Zeile")
    finally:
        db.close()

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        body = client.get(f"/api/vehicles/{vehicle_id}/snapshot").json()
    verify(body.get("source") == "vw_portal"
           and body.get("captured_at") == "2026-10-20T02:17:00Z"
           and body.get("received_at", "").endswith("Z")
           and body.get("values", {}).get("soc_pct") == 55,
           "die API liefert den neuesten Bericht, Zeiten in UTC mit Z", str(body))


def _cloud_tasks(main) -> list:
    return [t for t in main._tasks
            if t.get_coro().cr_code.co_filename.endswith("poller.py")]


def part_disabled():
    verify.section("Ohne Zugangsdaten läuft nichts")
    from fastapi.testclient import TestClient
    from app import cloud, main
    verify(not cloud.configured(), "ohne VW_PORTAL_* ist nichts eingerichtet")
    verify(not cloud.configured({"VW_PORTAL_USER": "a@b.c"}),
           "nur der Benutzer ohne Passwort reicht nicht")
    with TestClient(main.app) as client:
        verify(not _cloud_tasks(main), "beim Start entsteht keine Abruf-Aufgabe",
               str(main._tasks))
        status = client.get("/api/cloud/status").json()
        verify(status == {"configured": False, "sources": []},
               "/api/cloud/status sagt configured false", str(status))
        response = client.get("/api/vehicles/1/snapshot")
        verify(response.status_code == 404, "/api/vehicles/1/snapshot ist 404",
               str(response.status_code))

    started = []

    async def fake_loop(db_factory):
        started.append(db_factory)

    os.environ.update({"VW_PORTAL_USER": USER, "VW_PORTAL_PASSWORD": PASSWORD,
                       "VW_PORTAL_INTERVAL_MIN": "2"})
    real_loop = main.cloud_poller.loop
    main.cloud_poller.loop = fake_loop
    try:
        configs = cloud.active()
        verify(len(configs) == 1 and configs[0].interval_min == 5
               and PASSWORD not in repr(configs[0]),
               "mit Zugangsdaten eingerichtet, Abstand mindestens 5 min, "
               "das Passwort steht nicht in der Darstellung", repr(configs))
        with TestClient(main.app) as client:
            verify(len(started) == 1, "mit Zugangsdaten startet der Abruf")
            status = client.get("/api/cloud/status").json()
            verify(status["configured"] is True
                   and status["sources"][0]["name"] == "vw_portal"
                   and PASSWORD not in json.dumps(status)
                   and USER not in json.dumps(status),
                   "der Status nennt die Quelle, aber keine Zugangsdaten", str(status))
    finally:
        main.cloud_poller.loop = real_loop
        for name in ("VW_PORTAL_USER", "VW_PORTAL_PASSWORD", "VW_PORTAL_INTERVAL_MIN"):
            os.environ.pop(name, None)


class FakeSource:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.vehicle_calls = 0

    def vehicles(self):
        self.vehicle_calls += 1
        return [VIN]

    def updates(self, vin):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return [outcome]


def part_poller():
    verify.section("Abruf im Hintergrund")
    import asyncio
    import logging
    from app import cloud, models
    from app.cloud import poller
    from app.database import SessionLocal

    db = SessionLocal()
    vehicle_id = db.query(models.Vehicle.id).first()[0]
    db.close()
    snap = snapshot.Snapshot(captured_at=datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc),
                             values={"soc_pct": 54})
    source = FakeSource([vw_portal.PortalError("login rejected: x"),
                         vw_portal.PortalError("login rejected: x"),
                         RuntimeError("portal down"),
                         snap, snap])
    config = cloud.Config(name="vw_portal", vin=None, vehicle=str(vehicle_id),
                          interval_min=15, connect=lambda: source)
    poll = poller.Poll(config, SessionLocal)

    lines = []

    class Collect(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    handler = Collect()
    logger = logging.getLogger("uvicorn.error")
    level = logger.level
    logger.setLevel(logging.INFO)       # what uvicorn sets when it runs
    logger.addHandler(handler)
    delays = []

    async def sleep(seconds):
        delays.append(seconds)
        if len(delays) == 5:
            raise asyncio.CancelledError

    try:
        asyncio.run(poller.run(poll, sleep=sleep))
    except asyncio.CancelledError:
        pass
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)

    verify(delays == [1800, 3600, 7200, 900, 900],
           "nach Fehlern wartet der Abruf doppelt so lang (max. 2 h), danach "
           "wieder 15 min", str(delays))
    verify(lines == ["vw_portal: login rejected: x", "vw_portal: RuntimeError: portal down",
                     "vw_portal: working again"],
           "ins Log kommt nur der Wechsel: Fehler, anderer Fehler, wieder ok",
           str(lines))
    verify(poll.status.failures == 0 and poll.status.last_error is None
           and poll.status.last_ok is not None and poll.status.vehicle_id == vehicle_id,
           "der Status steht wieder auf ok", str(poll.status))
    verify(source.vehicle_calls == 1,
           "die Fahrzeugliste wird nur einmal abgefragt", str(source.vehicle_calls))
    verify(poller.delay_s(15, 10) == 7200, "die Pause wächst nicht über 2 h")
    now = datetime(2026, 10, 20, 12, 0)
    verify([poller.delay_s(15, 0, now - timedelta(hours=1), now),
            poller.delay_s(15, 0, now - timedelta(hours=3), now),
            poller.delay_s(15, 0, None, now),
            poller.delay_s(90, 0, now - timedelta(hours=3), now)]
           == [900, 3600, 3600, 5400],
           "15 min, solange das Auto in den letzten 2 h gemeldet hat, sonst "
           "stündlich (ein längeres eingestelltes Intervall bleibt)")
    db = SessionLocal()
    rows = db.query(models.VehicleSnapshot).filter_by(
        vehicle_id=vehicle_id, captured_at=datetime(2026, 10, 20, 3, 0)).count()
    db.close()
    verify(rows == 1, "zweimal derselbe Bericht: eine Zeile", str(rows))

    for vehicle, vin, vins, needle in (
            ("999", None, [VIN], "no such vehicle"),
            ("x", None, [VIN], "must be a jolt vehicle id"),
            (str(vehicle_id), None, [VIN, "WVGZZZSYNTHETIC02"], "…IC01, …IC02")):
        bad = poller.Poll(cloud.Config(
            name="vw_portal", vin=vin, vehicle=vehicle, interval_min=15,
            connect=lambda: type("S", (), {"vehicles": lambda self: vins})()),
            SessionLocal)
        try:
            bad.tick()
            error = ""
        except poller.SetupError as failure:
            error = str(failure)
        verify(needle in error and VIN not in error,
               f"Einrichtungsfehler wird benannt, VIN nur gekürzt: {needle}", error)


PARTS = [part_snapshot, part_login, part_disabled, part_storage, part_poller]


def main() -> int:
    for part in PARTS:
        part()
    return verify.balance(
        "Nicht geprüft: das echte VW-Portal. Dafür tools/vw_portal_probe.py "
        "mit eigenen Zugangsdaten.")


if __name__ == "__main__":
    sys.exit(main())
