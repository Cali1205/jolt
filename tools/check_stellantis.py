#!/usr/bin/env python3
"""Checks the Stellantis source (Opel, Peugeot, Citroën, DS) - `app/cloud/stellantis.py`
and `tools/stellantis_login.py`.

The real API needs an account, a car and a browser login, so none of this
talks to it. The status answer is shaped after the recorded responses in
flobz/psa_car_controller (tests/data/car_status.py) with made-up values and a
synthetic VIN; the token endpoint and the API run as fakes behind the real
host names.

Without network, without Postgres:

    ./tools/check_stellantis.py
"""
import base64
import hashlib
import importlib.util
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
from examine import Check, application_provide  # noqa: E402

application_provide("stellantis")
for _name in list(os.environ):
    if _name.startswith(("STELLANTIS_", "VW_PORTAL_")):
        os.environ.pop(_name)

import http.client  # noqa: E402
from urllib.parse import parse_qs, parse_qsl, urlsplit  # noqa: E402

import requests  # noqa: E402
import requests.adapters  # noqa: E402
import urllib3  # noqa: E402

from app import signals  # noqa: E402
from app.cloud import credentials, stellantis  # noqa: E402
from app.database import SessionLocal, migrate  # noqa: E402

verify = Check()
migrate()

USER, VIN, VEHICLE_ID = "fahrer@example.org", "VXKUSYNTHETIC0001", "vid-corsa-1"
OPEL = stellantis.BRANDS["opel"]
TOKEN_URL = OPEL.token_url
VEHICLES = stellantis.API + "/user/vehicles"
STATUS_URL = stellantis.API + f"/user/vehicles/{VEHICLE_ID}/status"
MAINTENANCE_URL = stellantis.API + f"/user/vehicles/{VEHICLE_ID}/maintenance"
TRIPS_URL = stellantis.API + f"/user/vehicles/{VEHICLE_ID}/trips"
TRIPS_PER_PAGE = 3
ACCOUNT = stellantis.account_key("opel", USER)

STATUS = {
    "lastPosition": {"type": "Feature",
                     "geometry": {"type": "Point", "coordinates": [9.20512, 48.49371, 341]},
                     "properties": {"updatedAt": "2026-10-20T06:58:02Z", "type": "Acquire",
                                    "signalQuality": 9}},
    "preconditionning": {"airConditioning": {"updatedAt": "2026-10-20T06:50:11Z",
                                             "status": "Disabled"}},
    "energy": [{"updatedAt": "2026-09-14T20:39:06Z", "type": "Fuel", "level": 0},
               {"updatedAt": "2026-10-20T07:02:54Z", "type": "Electric", "level": 59,
                "autonomy": 122,
                "charging": {"plugged": True, "status": "InProgress",
                             "remainingTime": "PT1H30M", "chargingRate": 20,
                             "chargingMode": "Quick", "nextDelayedTime": "PT22H31M"},
                "battery": {"health": {"capacity": 96, "resistance": 90}}}],
    "createdAt": "2026-10-20T07:02:54Z",
    "battery": {"voltage": 83.5, "current": 0, "createdAt": "2026-10-20T06:52:11Z"},
    "kinetic": {"createdAt": "2026-10-20T06:22:51Z", "moving": False},
    "privacy": {"createdAt": "2026-10-20T07:02:53Z", "state": "None"},
    "service": {"type": "Electric", "createdAt": "2026-10-20T07:02:54Z"},
    "_links": {"self": {"href": STATUS_URL}},
    "odometer": {"createdAt": None, "mileage": 3196.5},
    "updatedAt": "2026-10-20T07:02:54Z"}


# The shape a Corsa-e answered with in October 2026 (`energies[]` next to the
# older `energy[]`), synthetic values. `energy[]` says 59 % so that reading
# it instead of `energies[]` shows.
REPORT = "2026-10-21T05:37:47Z"
STATUS_FULL = {
    "createdAt": REPORT, "updatedAt": REPORT,
    "lastPosition": {"type": "Feature",
                     "geometry": {"type": "Point", "coordinates": [9.18, 48.78, 250.0]},
                     "properties": {"createdAt": "2026-10-21T05:24:11Z", "type": "Acquire",
                                    "signalQuality": 9, "fixStatus": "3D"}},
    "ignition": {"createdAt": "2026-10-21T04:10:00Z", "type": "Stop"},
    "battery": {"voltage": 85.0, "createdAt": REPORT},
    "environment": {"luminosity": {"createdAt": REPORT, "day": True},
                    "air": {"createdAt": REPORT, "temp": 9.5}},
    "odometer": {"createdAt": REPORT, "mileage": 7066.9},
    "kinetic": {"createdAt": "2026-05-21T18:09:26Z", "speed": 42.0, "moving": True},
    "alarm": {"status": {"createdAt": REPORT, "activation": "Active"}},
    "preconditioning": {"airConditioning": {"createdAt": REPORT, "status": "Disabled"}},
    "energies": [{"createdAt": REPORT, "type": "Electric", "subType": "ElectricEnergy",
                  "level": 60.0, "autonomy": 192,
                  "extension": {"electric": {
                      "battery": {"load": {"createdAt": REPORT, "capacity": 36608,
                                           "residual": 20000},
                                  "health": {"createdAt": REPORT, "capacity": 100,
                                             "resistance": 100}},
                      "charging": {"plugged": False, "status": "Disconnected",
                                   "chargingRate": 0, "chargingMode": "No",
                                   "nextDelayedTime": "PT0S", "schedule": []}}}}],
    "energy": [{"createdAt": REPORT, "updatedAt": REPORT, "type": "Electric", "level": 59.0,
                "autonomy": 190,
                "charging": {"plugged": False, "status": "Disconnected", "chargingMode": "No"},
                "battery": {"load": {"capacity": 36608, "residual": 20000},
                            "health": {"capacity": 100, "resistance": 100}}}],
    "_links": {"self": {"href": "/connectedcar/v4/user/vehicles/x/status"}}}
MAINTENANCE = {"createdAt": REPORT, "updatedAt": REPORT, "mileageBeforeMaintenance": 17920.0,
               "daysBeforeMaintenance": 120}
NET_KWH = 49.0


def full_with(change) -> dict:
    doc = json.loads(json.dumps(STATUS_FULL))
    change(doc)
    return doc


def status_with(**charging) -> dict:
    doc = json.loads(json.dumps(STATUS))
    doc["energy"][1]["charging"].update(charging)
    return doc


def part_parse():
    verify.section("Status -> Schnappschuss")
    snap = stellantis.parse_status(STATUS)
    expected = {"soc_pct": 59, "range_km": 122, "odometer_km": 3196.5, "plugged": True,
                "charge_state": "charging", "charge_type": "DC",
                "charge_remaining_min": 90.0, "battery_health_pct": 96,
                "lat": 48.49371, "lon": 9.20512, "position_at": "2026-10-20T06:58:02Z",
                "climate_state": "disabled", "aux_battery_pct": 83.5,
                "battery_resistance_pct": 90}
    for name, value in expected.items():
        got = snap.values.get(name)
        verify(got == value and isinstance(got, bool) == isinstance(value, bool),
               f"{name} = {value!r}", f"ist {got!r}")
    verify(set(snap.values) == set(expected), "keine weiteren Werte",
           str(sorted(set(snap.values) - set(expected))))
    verify(snap.values["lat"] == STATUS["lastPosition"]["geometry"]["coordinates"][1],
           "GeoJSON ist [Länge, Breite]: die Breite ist die zweite Zahl")
    verify(snap.captured_at == datetime(2026, 10, 20, 7, 2, 54, tzinfo=timezone.utc),
           "Stand ist die Zeit des Ladestands (energy Electric updatedAt)",
           str(snap.captured_at))
    verify(not signals.unknown(snap.values), "alles Katalognamen")
    verify(snap.raw.get("energy[1].charging.chargingRate") == ["energy[1].charging.chargingRate",
                                                               20, None]
           and not any(k.startswith("_links") for k in snap.raw),
           "alle Rohwerte bleiben erhalten, ohne die Links")

    cases = [({"status": "InProgress", "plugged": True}, "charging"),
             ({"status": "Finished", "plugged": True}, "purpose_reached"),
             ({"status": "Failure", "plugged": True}, "error"),
             ({"status": "Stopped", "plugged": True}, "ready"),
             ({"status": "Disconnected", "plugged": False}, "not_ready")]
    got = [stellantis.parse_status(status_with(**c)).values.get("charge_state")
           for c, _ in cases]
    verify(got == [e for _, e in cases],
           "Ladestatus: InProgress lädt, Finished Ziel erreicht, Failure Fehler, "
           "sonst nach dem Stecker bereit oder nicht", str(got))
    idle = stellantis.parse_status(status_with(status="Disconnected", plugged=False,
                                               chargingMode="No", remainingTime="PT0S"))
    verify("charge_type" not in idle.values and "charge_remaining_min" not in idle.values
           and idle.values["plugged"] is False,
           "ohne Laden keine Ladeart und keine Restzeit", str(idle.values))
    slow = stellantis.parse_status(status_with(chargingMode="Slow"))
    verify(slow.values.get("charge_type") == "AC", "Slow ist AC")
    durations = {"PT0S": 0.0, "PT45M": 45.0, "PT1H30M": 90.0, "P1DT2H": 1560.0,
                 "PT90S": 1.5, "": None, "soon": None, "P": None, None: None}
    got = {k: stellantis.duration_min(k) for k in durations}
    verify(got == durations, "ISO-8601-Dauern in Minuten", str(got))

    nowhere = json.loads(json.dumps(STATUS))
    nowhere["lastPosition"]["geometry"]["coordinates"] = [0, 0]
    broken = json.loads(json.dumps(STATUS))
    broken["lastPosition"]["geometry"]["coordinates"] = [48.5]
    verify(all("lat" not in stellantis.parse_status(d).values
               and "position_at" not in stellantis.parse_status(d).values
               for d in (nowhere, broken)),
           "keine Position bei (0, 0) oder unvollständigen Koordinaten")
    verify(stellantis.parse_status({"energy": [], "updatedAt": "2026-10-20T07:00:00Z"}) is None,
           "ein leerer Status ergibt keinen Schnappschuss")
    fuel_only = {"energy": [{"type": "Fuel", "level": 40}], "odometer": {"mileage": 10.0}}
    verify(stellantis.parse_status(fuel_only) is None,
           "ohne Zeitstempel kein Schnappschuss")


def part_full_status():
    verify.section("Voller Status: Zündung, Alarm, Temperatur, 12 V, Energie, Wartung")
    scale = stellantis.status_scale(STATUS_FULL, NET_KWH)
    verify(scale == stellantis.KWH_CORRECTION,
           "36608 bei 49 kWh nutzbar und 100 % Gesundheit: die Einheit ist 1,343-mal "
           "kleiner als Wh (HA #272)", str(scale))
    snap = stellantis.parse_status(STATUS_FULL, scale)
    expected = {"soc_pct": 60.0, "range_km": 192, "odometer_km": 7066.9, "plugged": False,
                "charge_state": "not_ready", "battery_health_pct": 100,
                "battery_resistance_pct": 100, "capacity_kwh": 49.16, "energy_kwh": 26.86,
                "lat": 48.78, "lon": 9.18, "position_at": "2026-10-21T05:24:11Z",
                "climate_state": "disabled", "ignition": "off", "alarm_active": True,
                "outside_temp_c": 9.5, "aux_battery_pct": 85.0}
    got = {k: snap.values.get(k) for k in expected}
    verify(got == expected and set(snap.values) == set(expected)
           and all(isinstance(snap.values[k], bool) == isinstance(v, bool)
                   for k, v in expected.items()),
           "genau diese Werte, aus energies[] statt energy[] (60 %, nicht 59 %)",
           str({k: v for k, v in snap.values.items() if expected.get(k) != v}))
    verify("speed_kmh" not in snap.values, "kinetic ist Monate alt: kein Tempo")
    verify(snap.captured_at == datetime(2026, 10, 21, 5, 37, 47, tzinfo=timezone.utc)
           and snap.raw.get("environment.luminosity.day") == ["environment.luminosity.day",
                                                              True, None]
           and snap.raw.get("lastPosition.properties.fixStatus") is not None
           and snap.raw.get("energy[0].level") is not None,
           "der ganze Status steht flach in raw, beide Energie-Listen", str(len(snap.raw)))

    def stale(doc):
        doc["environment"]["air"]["createdAt"] = "2026-10-20T13:27:16Z"
        doc["battery"]["createdAt"] = "2026-10-21T05:00:00Z"
    old = stellantis.parse_status(full_with(stale), scale).values
    verify("outside_temp_c" not in old and "aux_battery_pct" not in old
           and old.get("ignition") == "off",
           "Messwerte von lange vor der Meldung zählen nicht, der Zündzustand schon",
           str(old))
    edge = stellantis.parse_status(full_with(
        lambda d: d["environment"]["air"].update(createdAt="2026-10-21T05:07:47Z")), scale)
    verify(edge.values.get("outside_temp_c") == 9.5, "30 Minuten alt zählt noch")

    def state(ignition, alarm):
        def change(doc):
            doc["ignition"]["type"] = ignition
            doc["alarm"]["status"]["activation"] = alarm
        values = stellantis.parse_status(full_with(change), scale).values
        return values.get("ignition"), values.get("alarm_active")
    got = [state("StartUp", "Inactive"), state("Start", "Active"), state("Free", "Weird")]
    verify(got == [("on", False), ("on", True), (None, None)],
           "StartUp und Start sind an, Stop aus; Alarm Active/Inactive; Unbekanntes fehlt",
           str(got))

    scales = [stellantis.energy_scale(45000, 49.0, 100),
              stellantis.energy_scale(36750, 49.0, 75),
              stellantis.energy_scale(36608, 49.0, None),
              stellantis.energy_scale(20000, 49.0, 100),
              stellantis.energy_scale(36608, None, 100),
              stellantis.energy_scale(None, 49.0, 100)]
    verify(scales == [1.0, 1.0, stellantis.KWH_CORRECTION, None, None, None],
           "die Einheit nach dem Profil: Wh, wenn es passt (auch gealtert), 1,343 bei "
           "zu kleiner Kapazität, keine bei unpassender oder ohne Profil", str(scales))
    unscaled = stellantis.parse_status(STATUS_FULL, None).values
    verify("capacity_kwh" not in unscaled and "energy_kwh" not in unscaled,
           "ohne Einheit keine Energiewerte", str(unscaled))
    far = stellantis.parse_status(full_with(
        lambda d: d["energies"][0].update(level=90.0)), scale).values
    above = stellantis.parse_status(full_with(
        lambda d: d["energies"][0]["extension"]["electric"]["battery"]["load"]
        .update(residual=40000)), scale).values
    verify(far.get("capacity_kwh") == 49.16 and "energy_kwh" not in far
           and "energy_kwh" not in above,
           "Restenergie 55 % bei Ladestand 90 % oder über der Kapazität: keine "
           "Energie, die Kapazität bleibt", f"{far} {above}")

    _seed()
    corsa = _vehicle("Corsa voll", net_kwh=NET_KWH)
    fake = FakeStellantis(status=STATUS_FULL, maintenance=MAINTENANCE)
    client = stellantis.Stellantis("opel", USER, SessionLocal, adapter=fake, vehicle_id=corsa)
    first, second = client.latest(VIN), client.latest(VIN)
    asked = [w for _, w in fake.requests].count(MAINTENANCE_URL)
    verify(first.values.get("maintenance_km") == 17920.0
           and first.values.get("maintenance_days") == 120
           and first.raw.get("maintenance.mileageBeforeMaintenance")
           == ["maintenance.mileageBeforeMaintenance", 17920.0, None]
           and second.values.get("maintenance_km") == 17920.0 and asked == 1,
           "Wartung einmal am Tag gefragt, in jedem Schnappschuss", f"{asked} {first.values}")
    charging = stellantis.parse_status(full_with(
        lambda d: d["energies"][0]["extension"]["electric"]["charging"].update(
            plugged=True, status="InProgress", chargingMode="Quick", remainingTime="PT40M")),
        scale).values
    delivered = set(first.values) | set(charging)
    verify(delivered == stellantis.PROVIDES and client.scale == stellantis.KWH_CORRECTION,
           "PROVIDES ist genau, was ein voller Status mit Wartung liefert, beim Laden "
           "Ladeart und Restzeit dazu", str(sorted(stellantis.PROVIDES ^ delivered)))
    broken = stellantis.Stellantis("opel", USER, SessionLocal, adapter=FakeStellantis(
        status=STATUS_FULL), vehicle_id=corsa).latest(VIN)
    verify(broken is not None and "maintenance_km" not in broken.values,
           "ohne Wartungsantwort trotzdem ein Schnappschuss")


def _response(request, status, body=b"", headers=None):
    headers = headers or {}
    message = http.client.HTTPMessage()
    for name, value in headers.items():
        message[name] = value
    original = type("Original", (), {"msg": message, "isclosed": lambda self: False})()
    raw = urllib3.HTTPResponse(body=io.BytesIO(body), headers=headers, status=status,
                               preload_content=False, original_response=original)
    return requests.adapters.HTTPAdapter().build_response(request, raw)


def _json(request, status, document):
    return _response(request, status, json.dumps(document).encode(),
                     {"Content-Type": "application/json"})


class FakeStellantis(requests.adapters.BaseAdapter):
    """Opel's token endpoint and the connected-car API. Records every request
    and, for each API call, the refresh token stored at that moment."""

    def __init__(self, status=STATUS, maintenance=None, trips=None):
        super().__init__()
        self.status = status
        self.maintenance = maintenance
        # The trips the car made, oldest first; served `TRIPS_PER_PAGE` a page.
        self.trips = trips or []
        self.issued = 0
        self.access = None
        self.refuse = None
        self.expire_next = 0
        self.code = "code-1"
        self.challenge = None
        self.requests: list[tuple[str, str]] = []
        self.forms: list[dict] = []
        self.headers: list[dict] = []
        self.queries: list[dict] = []
        self.stored_at_call: list[str | None] = []

    def _issue(self, request):
        self.issued += 1
        self.access = f"access-{self.issued}"
        return _json(request, 200, {"access_token": self.access, "token_type": "Bearer",
                                    "refresh_token": f"refresh-{self.issued}",
                                    "expires_in": 899, "id_token": "id"})

    def send(self, request, **kwargs):
        parts = urlsplit(request.url)
        where = f"{parts.scheme}://{parts.netloc}{parts.path}"
        self.requests.append((request.method, where))
        self.headers.append(dict(request.headers))
        self.queries.append(parse_qs(parts.query))
        if where == TOKEN_URL:
            form = dict(parse_qsl(request.body if isinstance(request.body, str)
                                  else request.body.decode()))
            self.forms.append(form)
            basic = base64.b64encode(f"{OPEL.client_id}:{OPEL.client_secret}".encode()).decode()
            if request.headers.get("Authorization") != f"Basic {basic}":
                return _json(request, 401, {"error": "invalid_client"})
            if self.refuse:
                return _json(request, 400, {"error": self.refuse,
                                            "error_description": "grant is invalid"})
            if form.get("grant_type") == "authorization_code":
                digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
                challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                if form.get("code") != self.code or challenge != self.challenge:
                    return _json(request, 400, {"error": "invalid_grant"})
            return self._issue(request)
        if where.startswith(stellantis.API):
            db = SessionLocal()
            stored = credentials.load(db, stellantis.SOURCE, ACCOUNT)
            db.close()
            self.stored_at_call.append(stored and stored.get("refresh_token"))
            if request.headers.get("Authorization") != f"Bearer {self.access}" or self.expire_next:
                self.expire_next = max(0, self.expire_next - 1)
                return _json(request, 401, {"error": "invalid_token"})
            if where == VEHICLES:
                return _json(request, 200, {"_embedded": {"vehicles": [
                    {"id": VEHICLE_ID, "vin": VIN, "label": "Corsa"}]}})
            if where == STATUS_URL:
                return _json(request, 200, self.status)
            if where == MAINTENANCE_URL and self.maintenance is not None:
                return _json(request, 200, self.maintenance)
            if where == TRIPS_URL:
                return _json(request, 200, self.trips_page(parse_qs(parts.query)))
        return _response(request, 404)

    def trips_page(self, query) -> dict:
        """HAL paging as the real API does it: an opaque pageToken per page."""
        pages = max(1, -(-len(self.trips) // TRIPS_PER_PAGE))
        token = (query.get("pageToken") or [None])[0]
        number = int(token.removeprefix("tok-")) if token else 1

        def link(n):
            return {"href": f"/connectedcar/v4/user/vehicles/{VEHICLE_ID}/trips"
                            f"?client_id={OPEL.client_id}&pageToken=tok-{n}"}
        links = {"self": link(number), "first": link(1), "last": link(pages)}
        if number < pages:
            links["next"] = link(number + 1)
        if number > 1:
            links["prev"] = link(number - 1)
        chunk = self.trips[(number - 1) * TRIPS_PER_PAGE:number * TRIPS_PER_PAGE]
        return {"total": len(self.trips), "currentPage": number, "totalPage": pages,
                "_links": links, "_embedded": {"trips": chunk}}

    def close(self):
        pass


def _seed(access="access-0", refresh="refresh-0", expires_in=-60):
    db = SessionLocal()
    credentials.save(db, stellantis.SOURCE, ACCOUNT, {
        "brand": "opel", "country": "de", "access_token": access, "refresh_token": refresh,
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()})
    db.close()


def _stored() -> dict | None:
    db = SessionLocal()
    data = credentials.load(db, stellantis.SOURCE, ACCOUNT)
    db.close()
    return data


def part_tokens():
    verify.section("Tokens: erneuern, speichern, ablehnen")
    _seed()
    fake = FakeStellantis()
    client = stellantis.Stellantis("opel", USER, SessionLocal, adapter=fake)
    vins = client.vehicles()
    verify(vins == [VIN], "das Konto nennt sein Fahrzeug", str(vins))
    verify(fake.requests == [("POST", TOKEN_URL), ("GET", VEHICLES)],
           "abgelaufenes Token: erst erneuern, dann abfragen", str(fake.requests))
    verify(fake.forms[0] == {"grant_type": "refresh_token", "refresh_token": "refresh-0"},
           "die Erneuerung schickt das gespeicherte Refresh-Token", str(fake.forms[0]))
    verify(fake.stored_at_call == ["refresh-1"],
           "das neue Refresh-Token steht in der Datenbank, bevor das Access-Token "
           "benutzt wird", str(fake.stored_at_call))
    api = fake.headers[1]
    verify(api.get("Authorization") == "Bearer access-1"
           and api.get("X-Introspect-Realm") == "clientsB2COpel"
           and api.get("Accept") == "application/hal+json"
           and api.get("User-Agent", "").startswith("jolt/"),
           "Abfrage mit neuem Token, Realm der Marke, HAL-JSON", str(api))
    verify(fake.queries[1] == {"client_id": [OPEL.client_id]},
           "die API-Abfrage nennt den Client der Marke", str(fake.queries[1]))
    snap = client.latest(VIN)
    verify(snap is not None and snap.values.get("soc_pct") == 59
           and ("GET", STATUS_URL) in fake.requests[-2:] and len(fake.forms) == 1,
           "gültiges Token: Status ohne erneute Erneuerung", str(fake.requests))

    fake.expire_next = 1
    count = len(fake.requests)
    client.vehicles()
    verify(fake.requests[count:] == [("GET", VEHICLES), ("POST", TOKEN_URL), ("GET", VEHICLES)]
           and _stored()["refresh_token"] == "refresh-2",
           "401 von der API: einmal erneuern, neues Token speichern, wiederholen",
           str(fake.requests[count:]))

    other = stellantis.Stellantis("opel", USER, SessionLocal, adapter=fake)
    count = len(fake.requests)
    other.vehicles()
    verify(fake.requests[count:] == [("GET", VEHICLES)],
           "ein zweiter Client nimmt die gespeicherten frischen Tokens, statt "
           "selbst zu erneuern", str(fake.requests[count:]))

    _seed(access="access-x", refresh="refresh-old")
    fake = FakeStellantis()
    fake.refuse = "invalid_grant"
    client = stellantis.Stellantis("opel", USER, SessionLocal, adapter=fake)
    try:
        client.vehicles()
        error = ""
    except stellantis.LoginNeeded as failure:
        error = str(failure)
    verify("invalid_grant" in error and "tools/stellantis_login.py" in error,
           "abgelehnte Erneuerung: klare Meldung mit dem Anmeldewerkzeug", error)
    verify("refresh-old" not in error and "access-x" not in error and USER not in error,
           "die Meldung enthält weder Tokens noch Konto", error)
    verify(_stored()["refresh_token"] == "refresh-old"
           and not any(m == "GET" for m, _ in fake.requests),
           "nichts überschrieben, keine Abfrage mit ungültigem Token", str(fake.requests))

    db = SessionLocal()
    credentials.update(db, stellantis.SOURCE, ACCOUNT, lambda data: None)
    db.close()
    verify(_stored()["refresh_token"] == "refresh-old", "update ohne Änderung lässt alles stehen")
    try:
        stellantis.Stellantis("opel", "niemand@example.org", SessionLocal,
                              adapter=FakeStellantis()).vehicles()
        error = ""
    except stellantis.LoginNeeded as failure:
        error = str(failure)
    verify("no tokens stored" in error, "ohne gespeicherte Tokens: anmelden", error)


def _vehicle(name: str, net_kwh: float = 46.3) -> int:
    from app import models
    db = SessionLocal()
    vehicle = models.Vehicle(name=name, battery_gross_kwh=51.0, battery_net_kwh=net_kwh)
    db.add(vehicle)
    db.commit()
    vehicle_id = vehicle.id
    db.close()
    return vehicle_id


class FakeVw:
    def __init__(self, snap):
        self.snap = snap

    def vehicles(self):
        return ["WVGZZZSYNTHETIC01"]

    def updates(self, vin):
        return [self.snap]


def part_poller():
    verify.section("Abruf: zwei Quellen, zwei Fahrzeuge")
    import asyncio
    from fastapi.testclient import TestClient
    from app import cloud, main, models
    from app.cloud import poller, snapshot

    db = SessionLocal()
    db.query(models.CloudCredential).delete()
    db.commit()
    db.close()
    verify(cloud.active({"STELLANTIS_USER": USER}) == [],
           "nur STELLANTIS_USER ohne gespeicherte Tokens: aus")
    _seed()
    configs = cloud.active({"STELLANTIS_USER": USER, "STELLANTIS_VEHICLE": "7"})
    verify(len(configs) == 1 and configs[0].name == "stellantis"
           and configs[0].vehicle == "7" and configs[0].interval_min == 15
           and configs[0].sampled
           and configs[0].provides == stellantis.PROVIDES and USER not in repr(configs[0]),
           "mit Tokens: eingerichtet, das Konto steht nicht in der Darstellung", repr(configs))
    verify(cloud.active({"STELLANTIS_USER": USER, "STELLANTIS_BRAND": "peugeot"}) == [],
           "Tokens gehören zur Marke: Peugeot findet die Opel-Tokens nicht")

    buzz, corsa = _vehicle("ID.Buzz"), _vehicle("Corsa")
    fake = FakeStellantis()
    vw_snap = snapshot.Snapshot(captured_at=datetime(2026, 10, 20, 7, tzinfo=timezone.utc),
                                values={"soc_pct": 57})
    configs = [
        cloud.Config(name="vw_portal", env="VW_PORTAL", vin=None, vehicle=str(buzz),
                     interval_min=15, provides=snapshot.PROVIDES,
                     connect=lambda _: FakeVw(vw_snap)),
        cloud.Config(name="stellantis", env="STELLANTIS", vin=None, vehicle=str(corsa),
                     interval_min=15, provides=stellantis.PROVIDES,
                     connect=lambda vehicle_id: stellantis.Stellantis(
                         "opel", USER, SessionLocal, adapter=fake, vehicle_id=vehicle_id))]
    polls = [poller.Poll(c, SessionLocal) for c in configs]
    delays: dict[str, list] = {"vw_portal": [], "stellantis": []}

    def sleeper(name):
        async def sleep(seconds):
            delays[name].append(seconds)
            if name == "stellantis" and len(delays[name]) == 1:
                fake.refuse = "invalid_grant"
                db = SessionLocal()
                credentials.update(db, stellantis.SOURCE, ACCOUNT,
                                   lambda d: {**d, "expires_at": "2000-01-01T00:00:00+00:00"})
                db.close()
            if len(delays[name]) == 3:
                raise asyncio.CancelledError
        return sleep

    async def both():
        await asyncio.gather(*(poller.run(p, sleep=sleeper(p.config.name)) for p in polls),
                             return_exceptions=True)

    asyncio.run(both())
    db = SessionLocal()
    rows = {(r.vehicle_id, r.source): r for r in db.query(models.VehicleSnapshot).all()}
    db.close()
    verify((buzz, "vw_portal") in rows and (corsa, "stellantis") in rows
           and (buzz, "stellantis") not in rows and (corsa, "vw_portal") not in rows,
           "jede Quelle speichert für ihr Fahrzeug", str(sorted(rows)))
    corsa_row = rows.get((corsa, "stellantis"))
    verify(corsa_row is not None and corsa_row.values.get("lat") == 48.49371
           and corsa_row.captured_at == datetime(2026, 10, 20, 7, 2, 54),
           "der Opel-Schnappschuss mit Position, Zeit der Meldung", str(corsa_row and corsa_row.values))
    status = {p.config.name: p.status for p in polls}
    verify(status["vw_portal"].failures == 0 and status["vw_portal"].vehicle_id == buzz,
           "VW läuft weiter, während Stellantis scheitert", str(status["vw_portal"]))
    verify(status["stellantis"].failures == 2
           and "tools/stellantis_login.py" in (status["stellantis"].last_error or "")
           and delays["stellantis"] == [900, 1800, 3600],
           "abgelehnte Erneuerung: Fehler im Status, kein Absturz, der Abruf wartet länger",
           f"{status['stellantis']} {delays['stellantis']}")

    os.environ.update({"VW_PORTAL_USER": "a@b.c", "VW_PORTAL_PASSWORD": "pw",
                       "STELLANTIS_USER": USER})
    started = []

    async def no_loop(db_factory):
        started.append(db_factory)

    real_loop, main.cloud_poller.loop = main.cloud_poller.loop, no_loop
    try:
        with TestClient(main.app) as client:
            listed = client.get("/api/cloud/status").json()
            snap = client.get(f"/api/vehicles/{corsa}/snapshot").json()
    finally:
        main.cloud_poller.loop = real_loop
        for name in ("VW_PORTAL_USER", "VW_PORTAL_PASSWORD", "STELLANTIS_USER"):
            os.environ.pop(name, None)
    names = [s["name"] for s in listed.get("sources", [])]
    text = json.dumps(listed)
    verify(len(started) == 1, "mit beiden Quellen startet ein Abruf (für beide)")
    verify(listed.get("configured") is True and names == ["vw_portal", "stellantis"]
           and "invalid_grant" in text and USER not in text and "refresh-" not in text,
           "/api/cloud/status nennt beide Quellen, ohne Konto und Tokens", text)
    verify(snap.get("source_label") == "Stellantis" and snap["values"].get("lon") == 9.20512,
           "/api/vehicles/{id}/snapshot: Stellantis mit Position", str(snap))


def _login_tool():
    spec = importlib.util.spec_from_file_location(
        "stellantis_login", os.path.join(TOOLS, "stellantis_login.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def part_login():
    verify.section("Anmeldewerkzeug")
    import contextlib
    tool = _login_tool()
    db = SessionLocal()
    from app import models
    db.query(models.CloudCredential).delete()
    db.commit()
    db.close()
    fake = FakeStellantis()
    printed = io.StringIO()

    def read(prompt):
        url = next(line.strip() for line in printed.getvalue().splitlines()
                   if line.strip().startswith(OPEL.authorize_url))
        query = parse_qs(urlsplit(url).query)
        fake.challenge = query["code_challenge"][0]
        read.query = query
        return f"mymopsdk://oauth2redirect/de?code={fake.code}&scope=openid%20profile"

    with contextlib.redirect_stdout(printed):
        result = tool.main(["--brand", "opel", "--user", USER, "--country", "DE"],
                           read=read, adapter=fake)
    out = printed.getvalue()
    query = {k: v[0] for k, v in read.query.items()}
    verify(result == 0, "die Anmeldung gelingt", out[-300:])
    verify(query.get("client_id") == OPEL.client_id
           and query.get("redirect_uri") == "mymopsdk://oauth2redirect/de"
           and query.get("response_type") == "code" and query.get("scope") == "openid profile"
           and query.get("code_challenge_method") == "S256",
           "die Adresse ist evccs: Opel-Client, mymopsdk-Rückruf, PKCE S256", str(query))
    verify(fake.forms[0].get("grant_type") == "authorization_code"
           and fake.forms[0].get("code") == "code-1"
           and fake.forms[0].get("redirect_uri") == "mymopsdk://oauth2redirect/de",
           "der Code aus der kopierten Adresse wird mit Prüfwert eingetauscht",
           str(fake.forms[0]))
    stored = _stored()
    verify(stored and stored.get("refresh_token") == "refresh-1" and stored.get("brand") == "opel"
           and stored.get("country") == "de" and stored.get("access_token") == "access-1",
           "die Tokens stehen in cloud_credentials", str(stored and sorted(stored)))
    verify("…0001" in out and VIN not in out and "refresh-1" not in out and "access-1" not in out,
           "die Ausgabe nennt das Fahrzeug gekürzt und keine Tokens", out[-400:])
    verify(tool.code_from("  abc-123 ") == "abc-123"
           and tool.code_from("mymap://oauth2redirect/fr?code=xyz&state=") == "xyz",
           "Code allein oder ganze Adresse")

    fake = FakeStellantis()
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        result = tool.main(["--user", USER], read=lambda prompt: "falsch", adapter=fake)
    verify(result == 1 and "invalid_grant" in printed.getvalue()
           and _stored().get("refresh_token") == "refresh-1",
           "ein falscher Code: Fehler, gespeicherte Tokens bleiben", printed.getvalue()[-200:])


def part_logbook():
    verify.section("Fahrtenbuch aus Stellantis-Schnappschüssen")
    from app import cloud, models
    from app.cloud import poller

    corsa = _vehicle("Corsa Fahrtenbuch")
    # Yesterday: the poller reads the last 48 hours of the real clock.
    day = datetime.utcnow().date() - timedelta(days=1)

    def at(clock):
        return datetime.fromisoformat(f"{day}T{clock}:00")

    reports = [  # (time, odometer, soc, plugged, state, AC/DC)
        ("05:00", 3196.5, 60, False, "not_ready", None),
        ("07:00", 3196.5, 60, False, "not_ready", None),
        ("07:15", 3210.0, 55, False, "not_ready", None),
        ("07:30", 3224.0, 50, False, "not_ready", None),
        ("07:45", 3224.0, 50, True, "charging", "AC"),
        ("08:00", 3224.0, 55, True, "charging", "AC"),
        ("08:15", 3224.0, 60, True, "charging", "AC"),
        ("08:30", 3224.0, 64, True, "purpose_reached", None),
        ("09:00", 3224.0, 64, True, "ready", None)]
    db = SessionLocal()
    for clock, odometer, soc, plugged, state, kind in reports:
        moment = at(clock)
        values = {"odometer_km": odometer, "soc_pct": soc, "plugged": plugged,
                  "charge_state": state, "lat": 48.49, "lon": 9.2}
        if kind:
            values["charge_type"] = kind
        db.add(models.VehicleSnapshot(vehicle_id=corsa, source="stellantis", captured_at=moment,
                                      received_at=moment, values=values, raw=None))
    db.commit()
    poll = poller.Poll(cloud.Config(name="stellantis", env="STELLANTIS", vin=None,
                                    vehicle=str(corsa), interval_min=15,
                                    provides=stellantis.PROVIDES, connect=lambda _: None,
                                    sampled=True),
                       SessionLocal)
    poll.status.vehicle_id = corsa
    poll.update_logbook(db)
    drives = db.query(models.VehicleDrive).filter_by(vehicle_id=corsa).all()
    charges = db.query(models.VehicleCharge).filter_by(vehicle_id=corsa).all()
    db.close()
    verify(poll.logbook_error is None, "das Fahrtenbuch läuft ohne Fehler", str(poll.logbook_error))
    d = drives[0] if len(drives) == 1 else None
    verify(d is not None and (d.odo_start, d.odo_end, d.soc_start, d.soc_end)
           == (3196.5, 3224.0, 60, 50) and d.sources == ["stellantis"]
           and d.started_at == at("07:00") and d.ended_at == at("07:30"),
           "eine Fahrt 3196,5 -> 3224 km, 60 -> 50 %, von Stellantis",
           str([(x.odo_start, x.odo_end, x.started_at, x.ended_at, x.sources) for x in drives]))
    c = charges[0] if len(charges) == 1 else None
    verify(c is not None and (c.soc_start, c.soc_end) == (50, 64)
           and c.energy_kwh == round(14 * 46.3 / 100, 2) and c.max_power_kw is None
           and c.charge_type == "AC" and c.sources == ["stellantis"]
           and c.started_at == at("07:45") and c.ended_at == at("08:30"),
           "eine AC-Ladung 50 -> 64 %, Energie aus dem Ladestand, ohne Leistung",
           str([(x.soc_start, x.soc_end, x.energy_kwh, x.max_power_kw, x.charge_type,
                 x.started_at, x.ended_at) for x in charges]))


def part_corsa_night():
    verify.section("Fahrtenbuch: die Nacht zum 21. Oktober 2026")
    from app import models
    from app.cloud import logbook
    with open(os.path.join(TOOLS, "fixtures", "corsa_night.json"), encoding="utf-8") as f:
        snapshots = json.load(f)["snapshots"]
    corsa = _vehicle("Corsa Nacht")
    db = SessionLocal()
    try:
        for s in snapshots:
            moment = datetime.fromisoformat(s["captured_at"])
            db.add(models.VehicleSnapshot(vehicle_id=corsa, source="stellantis",
                                          captured_at=moment, received_at=moment,
                                          values=s["values"], raw=None))
        db.commit()

        def detect():
            return logbook.store_from_snapshots(db, corsa, logbook.STELLANTIS, powered=False,
                                                stop_split=timedelta(minutes=30),
                                                now=datetime(2026, 10, 21, 7, 0))

        # What production stored before: four charges for one night at the
        # cable, a fifth up to the departure, and the drive from 04:57.
        logbook.store_charges(db, corsa, logbook.STELLANTIS, [
            {"started_at": a, "ended_at": b, "soc_start": s0, "soc_end": s1,
             "energy_kwh": kwh, "max_power_kw": None, "avg_power_kw": None,
             "charge_type": "AC", "odometer_km": odo, "power_track": []}
            for a, b, s0, s1, kwh, odo in (
                ("2026-10-20T21:55:43", "2026-10-20T22:21:41", 56.0, 57.0, 0.49, 7059.6),
                ("2026-10-21T00:31:44", "2026-10-21T01:14:43", 57.0, 59.0, 0.98, 7059.6),
                ("2026-10-21T02:06:43", "2026-10-21T02:41:44", 59.0, 60.0, 0.49, 7059.6),
                ("2026-10-21T03:36:53", "2026-10-21T03:59:41", 60.0, 61.0, 0.49, 7059.6),
                ("2026-10-21T04:57:09", "2026-10-21T05:24:47", 61.0, 62.0, 0.49, 7060.2))])
        logbook.store_drives(db, corsa, logbook.STELLANTIS, [
            {"started_at": "2026-10-21T04:57:09", "ended_at": "2026-10-21T05:37:47",
             "odo_start": 7059.6, "odo_end": 7066.9, "distance_km": 7.3, "soc_start": 61.0,
             "soc_end": 60.0, "energy_used_kwh": None, "km_track": [], "soc_track": [],
             "live_session_id": None}])
        db.commit()
        verify(db.query(models.VehicleCharge).filter_by(vehicle_id=corsa).count() == 5,
               "vorher: fünf Ladevorgänge wie in der Datenbank")
        first = detect()
        drives = db.query(models.VehicleDrive).filter_by(vehicle_id=corsa).all()
        d = drives[0] if len(drives) == 1 else None
        verify(d is not None and d.started_at == datetime(2026, 10, 21, 5, 23, 35)
               and d.start_estimated and d.ended_at == datetime(2026, 10, 21, 5, 37, 47)
               and (d.odo_start, d.odo_end) == (7059.6, 7066.9),
               "die Fahrt am Morgen: Start geschätzt 05:23:35 (0,6 km vor dem Bericht um "
               "05:24:47, nicht 04:57 beim letzten Bericht am Kabel), Ende 05:37:47",
               str([(x.started_at, x.start_estimated, x.ended_at, x.odo_start, x.odo_end)
                    for x in drives]))
        charges = db.query(models.VehicleCharge).filter_by(vehicle_id=corsa).all()
        c = charges[0] if len(charges) == 1 else None
        verify(c is not None and c.started_at == datetime(2026, 10, 20, 21, 55, 43)
               and c.ended_at == datetime(2026, 10, 21, 5, 24, 47)
               and (c.soc_start, c.soc_end) == (56, 62)
               and c.energy_kwh == round(6 * 46.3 / 100, 2)
               and len(c.details["by_source"]["stellantis"]) == 1,
               "die Nacht am Kabel ist ein Ladevorgang: 21:55 bis zum Abstecken um 05:24, "
               "56 -> 62 %, die fünf alten Zeilen sind in einer aufgegangen",
               f"{first['charges']} " + str([(x.started_at, x.ended_at, x.soc_start, x.soc_end,
                                              x.energy_kwh) for x in charges]))
        again = detect()
        verify(again["drives"] == {"new": 0, "updated": 0, "unchanged": 1}
               and again["charges"] == {"new": 0, "updated": 0, "unchanged": 1}
               and db.query(models.VehicleCharge).filter_by(vehicle_id=corsa).count() == 1,
               "noch einmal erkannt: nichts ändert sich", str(again))
    finally:
        db.close()


def trip(n: int, start: str, minutes: int, odo: float, km: float, soc=(60.0, 58.0),
         consumption=896.0, done=True, lon=9.18, lat=48.78) -> dict:
    """One trip as `/trips` lists it, synthetic; it ends 0.01° north of its start."""
    began = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    ended = began + timedelta(minutes=minutes)
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    return {"id": f"trip-{n:03d}", "startedAt": began.strftime(stamp),
            "stoppedAt": ended.strftime(stamp), "duration": minutes * 60, "distance": km,
            "createdAt": ended.strftime(stamp), "updatedAt": ended.strftime(stamp),
            "done": done, "startMileage": odo,
            "startPosition": {"type": "Feature", "geometry": {"type": "Point",
                                                              "coordinates": [lon, lat]},
                              "properties": {"type": "Acquire"}},
            "stopPosition": {"type": "Feature", "geometry": {"type": "Point",
                                                             "coordinates": [lon, round(lat + 0.01, 6)]},
                             "properties": {"type": "Acquire"}},
            "startEnergies": [{"type": "Electric", "level": soc[0], "autonomy": 200}],
            "endEnergies": [{"type": "Electric", "level": soc[1], "autonomy": 195}],
            "energyConsumptions": [{"type": "Electric", "consumption": consumption,
                                    "avgConsumption": consumption / km * 100 if km else 0}],
            "kinetic": {"avgSpeed": round(km * 1000 / (minutes * 60), 2), "maxSpeed": 0.0},
            "_links": {"self": {"href": f"/connectedcar/v4/user/vehicles/x/trips/trip-{n:03d}"}}}


def part_trips_parse():
    verify.section("Fahrten der Stellantis-Cloud -> Fahrtenbuch-Einträge")
    t = trip(1, "2026-10-21T05:24:09", 13, 7060.0, 6.9, soc=(62.0, 60.0), consumption=896.0)
    record = stellantis.trip_record(t, stellantis.KWH_CORRECTION)
    expected = {"started_at": "2026-10-21T05:24:09", "ended_at": "2026-10-21T05:37:09",
                "odo_start": 7060.0, "odo_end": 7066.9, "distance_km": 6.9,
                "soc_start": 62.0, "soc_end": 60.0, "energy_used_kwh": 1.2,
                "trip_id": "trip-001", "avg_speed_kmh": 31.9, "max_speed_kmh": None,
                "start_lat": 48.78, "start_lon": 9.18, "end_lat": 48.79, "end_lon": 9.18,
                "live_session_id": None}
    got = {k: record.get(k) for k in expected} if record else None
    verify(got == expected, "Zeiten, Kilometer, Ladestand, 896 x 1,343 Wh = 1,2 kWh, "
           "8,85 m/s = 31,9 km/h, Position als Breite/Länge", str(got))
    verify(record["km_track"] == [["2026-10-21T05:24:09", 7060.0],
                                  ["2026-10-21T05:37:09", 7066.9]]
           and record["soc_track"][-1] == ["2026-10-21T05:37:09", 60.0],
           "Kilometer- und Ladestandsverlauf aus Start und Ende")
    plain = stellantis.trip_record(t, 1.0)
    unscaled = stellantis.trip_record(t, None)
    verify(plain["energy_used_kwh"] == 0.9 and unscaled["energy_used_kwh"] is None
           and unscaled["distance_km"] == 6.9,
           "Energie in der Einheit des Status: Wh bei 1, keine ohne Einheit",
           f"{plain['energy_used_kwh']} {unscaled['energy_used_kwh']}")
    rejected = [stellantis.trip_record(trip(2, "2026-10-21T06:00:00", 5, 7070.0, 3.0,
                                            done=False), 1.0),
                stellantis.trip_record(trip(3, "2026-10-21T06:00:00", 2, 7070.0, 0.8), 1.0),
                stellantis.trip_record({**t, "startMileage": None}, 1.0)]
    verify(rejected == [None, None, None],
           "nicht fertig, unter einem Kilometer oder ohne Kilometerstand: kein Eintrag")
    greedy = stellantis.trip_record(trip(4, "2026-10-21T06:00:00", 5, 7070.0, 2.0,
                                         consumption=2000.0), stellantis.KWH_CORRECTION)
    lost = stellantis.trip_record(trip(5, "2026-10-21T06:00:00", 10, 7070.0, 6.6,
                                       consumption=0.0), stellantis.KWH_CORRECTION)
    verify(greedy["energy_used_kwh"] is None and lost["energy_used_kwh"] is None
           and lost["distance_km"] == 6.6,
           "134 kWh/100 km oder 0 über 6,6 km: Energie unglaubwürdig, die Fahrt bleibt",
           f"{greedy['energy_used_kwh']} {lost['energy_used_kwh']}")
    nowhere = stellantis.trip_record({**t, "startPosition": None, "stopPosition": {}}, 1.0)
    verify(nowhere["start_lat"] is None and nowhere["end_lon"] is None,
           "ohne Positionen keine")


def part_trips_paging():
    verify.section("Fahrtenliste: Seiten nachladen")
    _seed()
    trips = [trip(n, f"2026-10-{1 + n:02d}T07:00:00", 15, 7000.0 + 10 * n, 8.0)
             for n in range(8)]
    fake = FakeStellantis(trips=trips)
    client = stellantis.Stellantis("opel", USER, SessionLocal, adapter=fake)
    client.vehicles()
    count = len(fake.requests)
    got = client.trips(VIN, everything=True)
    pages = [q.get("pageToken", [None])[0] for (_, w), q in
             zip(fake.requests[count:], fake.queries[count:]) if w == TRIPS_URL]
    verify([t["id"] for t in got] == [t["id"] for t in trips] and pages == [None, "tok-2", "tok-3"],
           "alles: erste Seite, dann `next` bis zur letzten", str(pages))
    count = len(fake.requests)
    got = client.trips(VIN, everything=False)
    pages = [q.get("pageToken", [None])[0] for (_, w), q in
             zip(fake.requests[count:], fake.queries[count:]) if w == TRIPS_URL]
    verify([t["id"] for t in got] == [t["id"] for t in trips[3:]]
           and pages == [None, "tok-3", "tok-2"],
           "danach: über `last` die letzte Seite und die davor", str(pages))

    class Looping(FakeStellantis):
        def trips_page(self, query):
            page = super().trips_page(query)
            page["_links"]["next"] = {"href": "/x/trips?pageToken=tok-2"}
            return page
    looping = Looping(trips=trips)
    client = stellantis.Stellantis("opel", USER, SessionLocal, adapter=looping)
    client.vehicles()
    count = len(looping.requests)
    client.trips(VIN, everything=True)
    verify([w for _, w in looping.requests[count:]].count(TRIPS_URL) == 2,
           "eine Seite, die wieder auf sich zeigt, beendet das Nachladen")
    capped, stellantis.MAX_TRIP_PAGES = stellantis.MAX_TRIP_PAGES, 2
    try:
        count = len(fake.requests)
        client = stellantis.Stellantis("opel", USER, SessionLocal, adapter=fake)
        got = client.trips(VIN, everything=True)
    finally:
        stellantis.MAX_TRIP_PAGES = capped
    verify(len(got) == 6, "höchstens MAX_TRIP_PAGES Seiten", str(len(got)))


def _drives(corsa):
    from app import models
    db = SessionLocal()
    rows = (db.query(models.VehicleDrive).filter_by(vehicle_id=corsa)
            .order_by(models.VehicleDrive.started_at).all())
    db.close()
    return rows


def part_trips_poll():
    verify.section("Abruf: Fahrtenliste ins Fahrtenbuch")
    from app import cloud
    from app.cloud import poller
    _seed()
    corsa = _vehicle("Corsa Fahrten", net_kwh=NET_KWH)
    trips = [trip(n, f"2026-10-{1 + n:02d}T07:00:00", 15, 7000.0 + 10 * n, 8.0)
             for n in range(8)]
    fake = FakeStellantis(status=STATUS_FULL, trips=trips)
    config = cloud.Config(name="stellantis", env="STELLANTIS", vin=None, vehicle=str(corsa),
                          interval_min=15, provides=stellantis.PROVIDES, sampled=True,
                          connect=lambda vehicle_id: stellantis.Stellantis(
                              "opel", USER, SessionLocal, adapter=fake, vehicle_id=vehicle_id))
    poll = poller.Poll(config, SessionLocal)

    def trip_requests(since):
        return [q.get("pageToken", [None])[0] for (_, w), q in
                zip(fake.requests[since:], fake.queries[since:]) if w == TRIPS_URL]
    poll.tick()
    rows = _drives(corsa)
    first = [(r.started_at, r.odo_start, r.energy_used_kwh, r.sources) for r in rows]
    verify(len(rows) == 8 and all(r.sources == ["stellantis_trips"] for r in rows)
           and rows[0].energy_used_kwh == 1.2 and poll.trips_error is None,
           "erster Abruf: alle acht Fahrten, Energie mit der Einheit aus dem Status",
           f"{poll.trips_error} {first[:2]}")
    count = len(fake.requests)
    fake.trips.append(trip(8, "2026-10-10T07:00:00", 15, 7080.0, 8.0))
    poll.last_new = datetime.utcnow()
    poll.tick()
    verify(trip_requests(count) == [None, "tok-3", "tok-2"] and len(_drives(corsa)) == 9,
           "danach nur die letzten Seiten, die neue Fahrt kommt dazu", str(trip_requests(count)))
    verify([(r.started_at, r.odo_start, r.energy_used_kwh, r.sources)
            for r in _drives(corsa)][:8] == first,
           "nochmals abgerufen: die alten Zeilen bleiben, wie sie waren")
    count = len(fake.requests)
    poll.last_new = datetime.utcnow() - timedelta(hours=3)
    poll.tick()
    verify(trip_requests(count) == [], "ein Auto, das seit Stunden nichts meldet: keine Abfrage")


def part_trips_merge():
    verify.section("Fahrtenbuch: Fahrtenliste, Schnappschüsse und Aufzeichnung")
    from app.cloud import logbook
    record = stellantis.trip_record(
        trip(1, "2026-10-21T05:24:09", 13, 7060.0, 6.9, soc=(62.0, 60.0)), stellantis.KWH_CORRECTION)
    status_drive = {"started_at": "2026-10-21T05:23:35", "ended_at": "2026-10-21T05:37:47",
                    "odo_start": 7059.6, "odo_end": 7066.9, "distance_km": 7.3,
                    "soc_start": 61.0, "soc_end": 60.0, "energy_used_kwh": None,
                    "km_track": [], "soc_track": [], "live_session_id": None,
                    "parked_at": "2026-10-21T04:57:09"}
    for order in ((logbook.STELLANTIS, status_drive), (logbook.STELLANTIS_TRIPS, record)), \
                 ((logbook.STELLANTIS_TRIPS, record), (logbook.STELLANTIS, status_drive)):
        corsa = _vehicle("Corsa Zusammenführung")
        db = SessionLocal()
        for source, item in order:
            logbook.store_drives(db, corsa, source, [item])
        db.commit()
        db.close()
        rows = _drives(corsa)
        r = rows[0] if len(rows) == 1 else None
        verify(r is not None and r.sources == ["stellantis_trips", "stellantis"]
               and (r.started_at, r.ended_at) == (datetime(2026, 10, 21, 5, 24, 9),
                                                  datetime(2026, 10, 21, 5, 37, 9))
               and not r.start_estimated and (r.odo_start, r.odo_end) == (7060.0, 7066.9)
               and r.energy_used_kwh == 1.2
               and (r.start_lat, r.start_lon, r.end_lat, r.end_lon) == (48.78, 9.18, 48.79, 9.18),
               f"Status-Fahrt und Fahrt der Liste ({order[0][0]} zuerst): eine Zeile, "
               "Zeiten, Kilometer, Energie und Orte aus der Liste, kein geschätzter Start",
               str([(x.sources, x.started_at, x.start_estimated, x.odo_start, x.start_lat)
                    for x in rows]))

    a = stellantis.trip_record(trip(2, "2026-10-21T08:00:00", 8, 7066.9, 3.1), 1.0)
    b = stellantis.trip_record(trip(3, "2026-10-21T08:20:00", 9, 7070.0, 4.0), 1.0)
    spanning = {**status_drive, "started_at": "2026-10-21T07:50:00",
                "ended_at": "2026-10-21T08:30:00", "odo_start": 7066.9, "odo_end": 7074.0,
                "distance_km": 7.1, "parked_at": None}
    for order in ([(logbook.STELLANTIS_TRIPS, a), (logbook.STELLANTIS_TRIPS, b),
                   (logbook.STELLANTIS, spanning)],
                  [(logbook.STELLANTIS, spanning), (logbook.STELLANTIS_TRIPS, a),
                   (logbook.STELLANTIS_TRIPS, b), (logbook.STELLANTIS, spanning)]):
        corsa = _vehicle("Corsa zwei Fahrten")
        db = SessionLocal()
        for source, item in order:
            logbook.store_drives(db, corsa, source, [item])
        db.commit()
        db.close()
        rows = _drives(corsa)
        verify([(r.odo_start, r.odo_end) for r in rows] == [(7066.9, 7070.0), (7070.0, 7074.0)],
               f"zwei Fahrten der Liste mit kurzem Halt, die der Status als eine sah "
               f"({order[0][0]} zuerst): zwei Zeilen, nichts verklebt",
               str([(r.odo_start, r.odo_end, r.sources) for r in rows]))

    jolt = {"started_at": "2026-10-21T05:23:50", "ended_at": "2026-10-21T05:37:30",
            "odo_start": 7060, "odo_end": 7067, "distance_km": 7.0, "soc_start": 62.5,
            "soc_end": 60.1, "energy_used_kwh": 1.08, "km_track": [], "soc_track": [],
            "live_session_id": None, "start_lat": 48.7801, "start_lon": 9.1801,
            "end_lat": 48.7899, "end_lon": 9.1799}
    corsa = _vehicle("Corsa mit Dongle")
    db = SessionLocal()
    logbook.store_drives(db, corsa, logbook.JOLT, [jolt])
    logbook.store_drives(db, corsa, logbook.STELLANTIS_TRIPS, [record])
    db.commit()
    db.close()
    rows = _drives(corsa)
    r = rows[0] if len(rows) == 1 else None
    verify(r is not None and r.sources == ["stellantis_trips", "jolt"]
           and r.energy_used_kwh == 1.08 and r.started_at == datetime(2026, 10, 21, 5, 24, 9)
           and (r.odo_start, r.odo_end) == (7060.0, 7066.9) and r.start_lat == 48.78,
           "Aufzeichnung und Fahrt der Liste: eine Zeile, Energie vom Dongle, Zeiten, "
           "Kilometer und Orte aus der Liste",
           str([(x.sources, x.energy_used_kwh, x.started_at, x.start_lat) for x in rows]))


PARTS = [part_parse, part_full_status, part_trips_parse, part_trips_paging, part_trips_poll,
         part_trips_merge, part_tokens, part_poller, part_login, part_logbook, part_corsa_night]


def main() -> int:
    for part in PARTS:
        part()
    return verify.balance(
        "Nicht geprüft: die echte Stellantis-API und ihre Anmeldung. Dafür "
        "tools/stellantis_login.py mit eigenem Konto.")


if __name__ == "__main__":
    sys.exit(main())
