#!/usr/bin/env python3
"""The whole chain, played through once - no network, no Postgres, no car.

Checks run against a fresh SQLite file with the demo routing: schema,
vehicles, route, charger import, corridor search and the live tracking
including the simulator.

That is the whole point: without this run, the live feature could only be
checked once a car, a data supplier and a real long-distance trip come
together - in other words never, during development.

    ./tools/check_backend.py
"""
import os
import re
import sys
import tempfile

TOOLS = os.path.dirname(os.path.abspath(__file__))
# The frontend sits in the repo next to backend/, in the image directly next to tools/.
FRONTEND = next(
    (p for p in (os.path.join(TOOLS, "..", "frontend"),)
     if os.path.isdir(p)), os.path.join(TOOLS, "..", "frontend"))
sys.path.insert(0, TOOLS)
from examine import Check, application_provide  # noqa: E402

application_provide("backend", db_name=False)

# Set before any app import: the engine is built at import time.
_DB = os.path.join(tempfile.mkdtemp(prefix="jolt-check-"), "check.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.pop("ORS_API_KEY", None)      # force demo routing
os.environ.pop("APP_PASSWORT", None)     # no login in the check run

from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.charging.chargers_import import from_bnetza_csv  # noqa: E402
from app.live import simulator  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402

verify = Check()


# An excerpt in the format of the official register: preamble, semicolons,
# decimal comma, connector blocks.
#
# The coordinates lie on the straight line Hamburg-Munich, because the demo
# routing produces exactly that line. The place names are therefore just
# labels - a real routing leads over other points.
CSV_PROBE = """Ladesäulenregister der Bundesnetzagentur;;;;;;;;;;;;;;;;;;
Stand: 01.08.2026;;;;;;;;;;;;;;;;;;
Hinweis: Diese Datei enthält alle gemeldeten Ladeeinrichtungen.;;;;;;;;;;;;;;;;;;
;;;;;;;;;;;;;;;;;;
Betreiber;Straße;Hausnummer;Postleitzahl;Ort;Bundesland;Breitengrad;Längengrad;\
Inbetriebnahmedatum;Nennleistung Ladeeinrichtung [kW];Art der Ladeeinrichung;\
Anzahl Ladepunkte;Steckertypen1;P1 [kW];Steckertypen2;P2 [kW]
Autobahn Energie;Rastplatz Nord;1;21079;Hamburg;Hamburg;53,4900;10,0500;\
01.03.2023;300;Schnellladeeinrichtung;4;DC Combo (CCS);150;DC Combo (CCS);150
Stadtwerke Lüneburg;Am Markt;3;21335;Lüneburg;Niedersachsen;53,2500;10,4100;\
01.06.2021;22;Normalladeeinrichtung;2;AC Steckdose Typ 2;22;;
Raststätte Harz;A7 Ost;2;38644;Goslar;Niedersachsen;51,9000;10,4300;\
15.09.2024;350;Schnellladeeinrichtung;8;DC Combo (CCS);350;CHAdeMO;50
Raststätte Vogelsberg;A7;12;36037;Fulda;Hessen;50,5700;10,8600;\
20.11.2022;150;Schnellladeeinrichtung;4;DC Combo (CCS);150;;
Autohof Steigerwald;A7;7;97080;Würzburg;Bayern;49,4900;11,1800;\
05.05.2023;300;Schnellladeeinrichtung;6;DC Combo (CCS);300;;
Insel Sylt;Strandweg;1;25980;Westerland;Schleswig-Holstein;54,9000;8,3100;\
01.01.2020;50;Schnellladeeinrichtung;2;DC Combo (CCS);50;;
Kaputte Zeile;;;;;;0,0000;0,0000;;0;;0;;;;
"""


def main() -> int:
    client = TestClient(app)

    print("\nStart und Schema")
    status = client.get("/api/status").json()
    verify(status["demo_routing"] is True, "Demo-Routing ist aktiv (kein Schlüssel)")
    verify(status["password_required"] is False, "ohne APP_PASSWORT offener Zugang")

    print("\nFahrzeuge")
    vehicles = client.get("/api/fahrzeuge").json()
    verify(len(vehicles) == 1, "beim ersten Start wird ein Fahrzeug angelegt",
           f"sind {len(vehicles)}")
    verify(len(vehicles[0]["charge_curve"]) >= 5,
           "und es hat eine Ladekurve mit mehreren Stützstellen")
    verify(len(client.get("/api/fahrzeuge/vorlagen").json()) >= 4,
           "es gibt mehrere Vorlagen zur Auswahl")

    fresh = client.post("/api/fahrzeuge", json={
        "name": "Prüfwagen", "battery_gross_kwh": 82.0, "battery_net_kwh": 77.0,
        "max_charge_power_kw": 150.0,
        "charge_curve": [[0, 120], [20, 150], [50, 100], [80, 50], [100, 8]]}).json()
    verify(fresh["id"] != vehicles[0]["id"], "ein zweites Fahrzeug lässt sich anlegen")
    verify(len(fresh["charge_curve"]) == 5, "mit eigener Ladekurve")
    failure = client.post("/api/fahrzeuge", json={
        "name": "Unsinn", "battery_gross_kwh": 50.0, "battery_net_kwh": 60.0})
    verify(failure.status_code == 400, "netto über brutto wird abgelehnt",
           f"HTTP {failure.status_code}")

    # Regression: a duplicate charge level in the curve violates the
    # unique constraint (vehicle_id, soc_percent) - that must arrive as an
    # understandable 400, not as a bare 500 on commit.
    double = client.post("/api/fahrzeuge", json={
        "name": "Doppelte Kurve", "battery_gross_kwh": 82.0, "battery_net_kwh": 77.0,
        "charge_curve": [[0, 180], [20, 180], [80, 80], [90, 90], [90, 60],
                     [100, 45]]})
    verify(double.status_code == 400,
           "ein doppelter Ladestand in der Kurve wird sauber abgelehnt",
           f"HTTP {double.status_code}: {double.text[:120]}")
    verify("90" in double.json().get("detail", ""),
           "und die Meldung nennt den betroffenen Ladestand",
           double.json())

    # Regression: changing a vehicle while keeping the same charge levels as
    # before (only the kW values change - the normal case when editing) must
    # not crash. SQLAlchemy would otherwise write the new rows before deleting
    # the old ones in the same flush and violate the unique constraint, even
    # though the new curve on its own has no duplicates.
    changed = client.put(f"/api/fahrzeuge/{vehicles[0]['id']}", json={
        "name": vehicles[0]["name"], "battery_gross_kwh": vehicles[0]["battery_gross_kwh"],
        "battery_net_kwh": vehicles[0]["battery_net_kwh"],
        "charge_curve": [[soc, kw + 5] for soc, kw in vehicles[0]["charge_curve"]]})
    verify(changed.status_code == 200,
           "dieselben Ladestände beim Ändern zu behalten funktioniert",
           f"HTTP {changed.status_code}: {changed.text[:150]}")

    print("\nLadesäulen-Import (Format der Bundesnetzagentur)")
    db = SessionLocal()
    try:
        counter = from_bnetza_csv(db, CSV_PROBE.encode("utf-8"))
        again = from_bnetza_csv(db, CSV_PROBE.encode("utf-8"))
    finally:
        db.close()
    verify(counter["neu"] == 6, "sechs Ladepunkte eingelesen",
           f"sind {counter['neu']}")
    verify(counter["skipped"] == 1,
           "die Zeile mit Koordinate 0/0 wird verworfen")
    verify(again["neu"] == 0 and again["aktualisiert"] == 6,
           "ein zweiter Lauf legt nichts doppelt an - der Import ist idempotent",
           f"{again}")

    db = SessionLocal()
    try:
        harz = db.query(models.ChargePoint).filter(
            models.ChargePoint.city == "Goslar").one()
        # Only existence counts: `.one()` raises if the record is missing
        # or duplicated - either would be an import error.
        db.query(models.ChargePoint).filter(
            models.ChargePoint.city == "Westerland").one()
        lueneburg = db.query(models.ChargePoint).filter(
            models.ChargePoint.city == "Lüneburg").one()
    finally:
        db.close()
    verify(harz.max_kw == 350.0, "Leistung mit Dezimalkomma korrekt gelesen",
           f"ist {harz.max_kw}")
    verify("CCS" in harz.connector_types and "CHAdeMO" in harz.connector_types,
           "beide Steckertypen erkannt", harz.connector_types)
    verify(lueneburg.connector_types == "Typ2",
           "'AC Steckdose Typ 2' wird auf Typ2 abgebildet",
           lueneburg.connector_types)
    verify(abs(harz.lat - 51.9) < 1e-6, "Koordinate mit Komma korrekt gelesen",
           f"ist {harz.lat}")

    print("\nDoppelter foreign_id innerhalb eines Imports")
    # Regression: a multi-country query at Open Charge Map can return a
    # site near the border twice. Without a flush between two
    # _speichern() calls for the same foreign_id, the second lookup does not see
    # the first, not yet committed INSERT - the second INSERT then violates
    # the unique constraint (quelle, foreign_id) and the whole batch fails with
    # HTTP 500 (here: a bare IntegrityError).
    from app.charging.chargers_import import _save

    db = SessionLocal()
    try:
        at_first = _save(db, "ocm", "pruef-doppelt", {
            "name": "Erststand", "operator": "", "lat": 50.0, "lon": 10.0,
            "address": "", "postcode": "", "city": "", "country": "DE",
            "connectors": [], "max_kw": 50.0, "point_count": 1,
            "connector_types": "CCS", "as_of": ""})
        second = _save(db, "ocm", "pruef-doppelt", {
            "name": "Zweitstand", "operator": "", "lat": 50.0, "lon": 10.0,
            "address": "", "postcode": "", "city": "", "country": "DE",
            "connectors": [], "max_kw": 60.0, "point_count": 1,
            "connector_types": "CCS", "as_of": ""})
        db.commit()
        verify(at_first == "neu" and second == "aktualisiert",
               "der zweite Aufruf für dieselbe foreign_id aktualisiert, statt "
               "ein Duplikat anzulegen", f"{at_first}, {second}")
    finally:
        db.close()

    print("\nOpen-Charge-Map-Import ohne compact=true")
    # Regression 1: compact=true makes OCM deliver AddressInfo.Country and
    # OperatorInfo as bare IDs instead of objects - "land" and
    # "betreiber" therefore arrived empty for every imported point.
    # Regression 2: a request with comma-separated country codes
    # (countrycode=AT,CH,...) is not reliably honoured by OCM - in
    # practice sites from all over the world came back, not just from the
    # requested countries. Both are checked directly against a fake but
    # realistic (verbose) OCM response: no compact parameter, and
    # one call per country instead of a comma-separated list.
    from app.charging import chargers_import as chargers_import_module

    sent_countrycodes: list = []
    latest_params: dict = {}

    class _FakeOcmResponse:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return [{
                "ID": 999001,
                "AddressInfo": {"Latitude": 48.2, "Longitude": 16.4,
                                "AddressLine1": "Teststrasse 1", "Postcode": "1010",
                                "Town": "Wien", "Country": {"ISOCode": "AT"}},
                "OperatorInfo": {"Title": "EnBW mobility+"},
                "Connections": [{"ConnectionType": {"Title": "CCS"},
                                 "PowerKW": 150.0, "Quantity": 2}],
                "NumberOfPoints": 2, "DateLastStatusUpdate": "2026-08-24T00:00:00Z",
                # Exactly the fields the import used to throw away.
                # They say what makes a charging point unusable for a trip
                # - and that matters more than any objective function.
                "StatusType": {"Title": "Operational", "IsOperational": True},
                "UsageType": {"Title": "Private - Restricted access",
                              "IsMembershipRequired": True},
                "UsageCost": "0,59 EUR/kWh",
                "GeneralComments": "Kabel kurz, für Kastenwagen ungeeignet",
                "AccessComments": "Hinter Schranke, nachts geschlossen",
                "DateLastVerified": "2026-07-01T00:00:00Z",
            }]

    def _fake_ocm_get(url, timeout=None, params=None):
        latest_params.clear()
        latest_params.update(params or {})
        sent_countrycodes.append((params or {}).get("countrycode"))
        return _FakeOcmResponse()

    ocm_get_original = chargers_import_module.requests.get
    chargers_import_module.requests.get = _fake_ocm_get
    db = SessionLocal()
    try:
        chargers_import_module.from_ocm(db, "test-schluessel", countries=["AT", "CH"],
                                     max_results=1)
        verify("compact" not in latest_params,
               "compact=true wird nicht mehr gesendet", str(latest_params))
        verify(sent_countrycodes == ["AT", "CH"],
               "jedes Land wird einzeln angefragt, nicht als kommagetrennte Liste",
               str(sent_countrycodes))
        entry = (db.query(models.ChargePoint)
                   .filter_by(source="ocm", foreign_id="999001").one())
        verify(entry.country == "AT", "das Land wird aus der Antwort übernommen",
               f"ist {entry.country!r}")
        verify(entry.operator == "EnBW mobility+",
               "und der Betreiber ebenso", f"ist {entry.operator!r}")
    finally:
        chargers_import_module.requests.get = ocm_get_original
        db.close()

    # What is stated in words is now kept. A charging point can look
    # flawless - 150 kW, two posts - and still be unusable
    # because it sits behind a barrier.
    db = SessionLocal()
    try:
        lp = db.query(models.ChargePoint).filter_by(source="ocm",
                                                  foreign_id="999001").one()
        verify(lp.operational is True,
               "der Betriebszustand wird übernommen", str(lp.operational))
        verify(lp.access == "Private - Restricted access",
               "die Zugangsart auch", str(lp.access))
        verify(lp.membership_required is True,
               "und ob eine Mitgliedschaft nötig ist")
        verify((lp.hints or {}).get("cost") == "0,59 EUR/kWh",
               "der Preistext der Quelle wird aufgehoben",
               str(lp.hints))
        verify("Kastenwagen" in (lp.hints or {}).get("general", ""),
               "und die Kommentare - hier steht, was kein Datenfeld verrät",
               str((lp.hints or {}).get("general")))
        verify("Schranke" in (lp.hints or {}).get("access", ""),
               "Zugangshinweise ebenso")
        verify("checked_at" in (lp.hints or {}),
               "und wann die Angabe zuletzt geprüft wurde")
    finally:
        db.close()

    print("\nOpen-Charge-Map-Import entlang einer Strecke")
    # Regression: from_ocm()'s offset pagination does not reliably page through
    # a very large country (see above) - from_ocm_route() instead queries
    # several radii along the route geometry. Checked: several anchors on
    # a longer route, and a site that two overlapping radii both see is
    # counted only once.
    distance = [[16.37 + 0.01 * i, 48.21] for i in range(151)]  # ~150 km east-west

    requested_anchor: list = []

    class _FakeRoutesResponse:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return [{
                "ID": 888001,
                "AddressInfo": {"Latitude": 48.21, "Longitude": 16.5,
                                "Country": {"ISOCode": "AT"}},
                "OperatorInfo": {"Title": "IONITY"},
                "Connections": [{"ConnectionType": {"Title": "CCS"},
                                 "PowerKW": 350.0, "Quantity": 4}],
            }]

    def _fake_routes_get(url, timeout=None, params=None):
        requested_anchor.append((params.get("latitude"), params.get("longitude")))
        return _FakeRoutesResponse()

    ocm_get_original = chargers_import_module.requests.get
    chargers_import_module.requests.get = _fake_routes_get
    db = SessionLocal()
    try:
        counter = chargers_import_module.from_ocm_route(
            db, "test-schluessel", distance, radius_km=30.0)
        verify(len(requested_anchor) >= 2,
               "eine längere Strecke fragt mehrere Umkreise ab",
               f"{len(requested_anchor)} Anker")
        verify(counter["neu"] == 1,
               "ein Standort, den mehrere überlappende Umkreise sehen, "
               "wird nur einmal gezählt", str(counter))
    finally:
        chargers_import_module.requests.get = ocm_get_original
        db.close()

    print("\nOrtssuche ohne Länderfilter")
    # Regression: `land` used to be hard-coded to "DE", so /api/orte never
    # asked explicitly - every destination across the border vanished through
    # ORS' boundary.country filter. The demo routing makes no real
    # HTTP call, so the ORS adapter is checked directly here: which
    # parameters actually went to openrouteservice.
    from app.routing.ors import ORS
    import app.routing.ors as ors_module

    requested: dict = {}

    class _FakeResponse:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"features": []}

    def _fake_get(url, timeout=None, params=None, headers=None):
        requested.clear()
        requested.update(params or {})
        return _FakeResponse()

    ors_get_original = ors_module.requests.get
    ors_module.requests.get = _fake_get
    try:
        ORS(api_key="test").seek("Paris")
        verify("boundary.country" not in requested,
               "ohne Land wird nicht mehr fest auf DE eingeschränkt",
               str(requested))
        ORS(api_key="test").seek("Paris", country="FR")
        verify(requested.get("boundary.country") == "FR",
               "ein explizit gesetztes Land wird weiterhin übergeben",
               str(requested))
    finally:
        ors_module.requests.get = ors_get_original

    print("\nRoute Hamburg - München")
    response = client.post("/api/route", json={
        "vehicle_id": vehicles[0]["id"],
        "start": {"lat": 53.5511, "lon": 9.9937, "text": "Hamburg"},
        "destination": {"lat": 48.1351, "lon": 11.5820, "text": "München"},
        "start_soc": 80.0})
    verify(response.status_code == 200, "Route wird gerechnet",
           f"HTTP {response.status_code}: {response.text[:120]}")
    variants = response.json()["variants"]
    # The demo adapter makes no difference between the three ORS presets
    # and returns the same straight line for all - /api/route notices this and
    # the default is a single route, the fastest.
    #
    # Before, there were three ("fastest", "shortest", "recommended"). "shortest"
    # is gone entirely by now: on Le Gurp - Montchanin it returns 554 km
    # in 11.8 hours against 654 km in 6.3 - a hundred kilometres less,
    # bought with five and a half hours. And "recommended" on motorway
    # routes usually yields the same road as "fastest". Three requests
    # for one answer, with 2,500 ORS requests a day.
    verify(len(variants) == 1,
           "ohne Alternative wird genau eine Route gerechnet",
           f"{len(variants)} Varianten")
    route = variants[0]
    trip_id = route["trip_id"]
    verify(route["labels"] == ["schnellste"],
           "und sie ist die schnellste", str(route["labels"]))

    # With the alternative, the toll-free one is added. The demo routing invents
    # a straight line and knows no toll roads - both requests therefore
    # yield the same route, and /api/route merges them into one variant
    # with both labels. That is exactly what is to be checked here: that the
    # merging works and the same thing is not offered twice.
    with_old = client.post("/api/route", json={
        "vehicle_id": vehicles[0]["id"],
        "start": {"lat": 53.5511, "lon": 9.9937, "text": "Hamburg"},
        "destination": {"lat": 48.1351, "lon": 11.5820, "text": "München"},
        "start_soc": 80.0, "alternative": True}).json()["variants"]
    verify(len(with_old) == 1,
           "im Demo-Modus ist die mautfreie Route dieselbe - sie wird "
           "zusammengelegt statt doppelt angeboten",
           f"{len(with_old)} Varianten")
    verify("toll_free" in with_old[0]["labels"],
           "und das Etikett sagt es", str(with_old[0]["labels"]))
    verify(route["demo"] is True, "und ist als Demo gekennzeichnet")
    verify(500 < route["distance_km"] < 900, "Strecke plausibel",
           f"{route['distance_km']} km")
    verify(14 < route["consumption_kwh_100km"] < 30, "Verbrauch plausibel",
           f"{route['consumption_kwh_100km']} kWh/100 km")
    verify(route["suffices"] is False,
           "ein 60-kWh-Auto schafft die Strecke nicht ohne Nachladen")
    verify(route["reserve_point"] is not None,
           "und die Reserve-Marke hat eine Koordinate für die Karte")
    verify(route["reserve_at_km"] < route["distance_km"],
           "die Marke liegt vor dem Ziel")

    short = client.post("/api/route", json={
        "vehicle_id": vehicles[0]["id"],
        "start": {"lat": 53.5511, "lon": 9.9937, "text": "Hamburg"},
        "destination": {"lat": 53.0793, "lon": 8.8017, "text": "Bremen"},
        "start_soc": 80.0}).json()["variants"][0]
    verify(short["suffices"] is True, "Hamburg-Bremen reicht dagegen locker",
           f"SoC am Ziel {short['soc_at_target']} %")

    print("\nAnhänger und Höchstgeschwindigkeit")
    # The slider is at 150%: the routing speed is raised by half
    # - far beyond any limit a trailer combination has.
    def hamburg_bremen(**more):
        response = client.post("/api/route", json={
            "vehicle_id": vehicles[0]["id"],
            "start": {"lat": 53.5511, "lon": 9.9937, "text": "Hamburg"},
            "destination": {"lat": 53.0793, "lon": 8.8017, "text": "Bremen"},
            "start_soc": 80.0, "speed_factor": 1.5, **more})
        return response, (response.json()["variants"][0]
                         if response.status_code == 200 else None)

    _, free = hamburg_bremen()
    response, limited = hamburg_bremen(speed_max_kmh=100.0)
    verify(response.status_code == 200, "eine Fahrt mit Tempo-Grenze wird gerechnet",
           f"HTTP {response.status_code}: {response.text[:120]}")
    verify(limited["kwh_total"] < free["kwh_total"],
           "mit 100 km/h als Grenze braucht dieselbe Strecke weniger Energie "
           "als bei 150 % ungebremst",
           f"{limited['kwh_total']} gegen {free['kwh_total']} kWh")
    verify(limited["drive_time_minutes"] > free["drive_time_minutes"],
           "und dauert länger - die Grenze kostet Zeit, und die Anzeige sagt es",
           f"{limited['drive_time_minutes']} gegen {free['drive_time_minutes']} min")

    response, rig = hamburg_bremen(speed_max_kmh=100.0, trailer_kg=1300,
                                      trailer_cwa_m2=1.1)
    verify(response.status_code == 200, "mit Anhänger auch",
           f"HTTP {response.status_code}: {response.text[:120]}")
    verify(rig["kwh_total"] > limited["kwh_total"] * 1.25,
           "der Anhänger kostet bei gleichem Tempo deutlich mehr",
           f"{limited['kwh_total']} -> {rig['kwh_total']} kWh")
    saved = client.get(f"/api/fahrten/{rig['trip_id']}").json()
    verify(saved.get("trailer_kg") == 1300
           and saved.get("speed_max_kmh") == 100.0,
           "beides steht an der Fahrt - eine Umplanung unterwegs rechnet damit",
           f"{saved.get('trailer_kg')}, {saved.get('speed_max_kmh')}")
    verify(client.post("/api/route", json={
        "vehicle_id": vehicles[0]["id"],
        "start": {"lat": 53.5511, "lon": 9.9937}, "destination": {"lat": 53.0793, "lon": 8.8017},
        "speed_max_kmh": 5}).status_code == 422,
        "eine Grenze von 5 km/h ist ein Tippfehler und wird abgelehnt")

    bound = client.put(f"/api/fahrzeuge/{vehicles[0]['id']}", json={
        **vehicles[0],
        "max_speed_kmh": 120.0})
    verify(bound.status_code == 200 and bound.json().get("max_speed_kmh") == 120.0,
           "die Höchstgeschwindigkeit lässt sich am Fahrzeug setzen",
           f"HTTP {bound.status_code}: {bound.text[:120]}")
    _, at_auto = hamburg_bremen()
    verify(at_auto["kwh_total"] < free["kwh_total"],
           "und begrenzt jede Fahrt dieses Fahrzeugs",
           f"{at_auto['kwh_total']} gegen {free['kwh_total']} kWh")
    client.put(f"/api/fahrzeuge/{vehicles[0]['id']}", json={
        **vehicles[0],
        "max_speed_kmh": None})

    print("\nEigene Strecken als Kandidaten")
    # An earlier trip Hamburg - Munich (60 measurement points along the
    # route, with speed) turns the same request into one more candidate.
    #
    # The demo routing invents a slightly different line for every leg;
    # the candidate would always be longer and slower in it and would be discarded
    # as hopeless before it got a label. That is why a routing is used here that
    # records every call and always returns the same road: what is checked is
    # what this module is responsible for - that the intermediate points reach
    # the routing and the label sits on the route.
    from datetime import datetime as _dt, timedelta as _td
    from app import routing as _routing
    from app.routing.demo import DemoRouting as _Demo
    HH = (53.5511, 9.9937)
    MUC = (48.1351, 11.5820)

    class _Calls(_Demo):
        calls: list = []

        def route(self, start, destination, intermediate_stops=None, preference="recommended",
                  toll_free=False):
            _Calls.calls.append(list(intermediate_stops or []))
            return super().route(start, destination)         # same road, always

    def on_the_line(share):
        return (HH[0] + (MUC[0] - HH[0]) * share,
                HH[1] + (MUC[1] - HH[1]) * share)

    db = SessionLocal()
    try:
        old = models.LiveSession(trip_id=trip_id, started_at=_dt(2026, 9, 4, 10, 0),
                                 ended_at=_dt(2026, 9, 4, 16, 0), running=False)
        db.add(old)
        db.flush()
        old_id = old.id
        for i in range(60):
            lat, lon = on_the_line(i / 59)
            db.add(models.LivePoint(session_id=old_id, lat=lat, lon=lon,
                                    timestamp=_dt(2026, 9, 4, 10, 0) + _td(minutes=5 * i),
                                    speed_kmh=100.0))
        db.commit()
    finally:
        db.close()

    fallback = _routing.provider
    _routing.provider = lambda: _Calls()
    try:
        def schedule(start, destination, **more):
            _Calls.calls = []
            response = client.post("/api/route", json={
                "vehicle_id": vehicles[0]["id"],
                "start": {"lat": start[0], "lon": start[1], "text": "A"},
                "destination": {"lat": destination[0], "lon": destination[1], "text": "B"},
                "start_soc": 80.0, **more})
            verify(response.status_code == 200, "die Anfrage geht durch",
                   f"HTTP {response.status_code}: {response.text[:120]}")
            labels = [e for v in response.json()["variants"] for e in v["labels"]]
            return labels, [a for a in _Calls.calls if a]

        labels, with_via = schedule(HH, MUC)
        verify(len(with_via) == 1 and len(with_via[0]) >= 5,
               "dieselbe Strecke noch einmal: Das Routing bekommt die gefahrene "
               "als Zwischenpunkte - einmal, nicht je Messpunkt",
               f"{len(with_via)} Aufrufe mit Zwischenpunkten")
        verify(any(e.startswith("meine Strecke vom 04.09.2026") for e in labels),
               "und die Route trägt das Datum der Fahrt", str(labels))
        labels, with_via = schedule(HH, MUC, own_trips=False)
        verify(not with_via and not any("meine Strecke" in e for e in labels),
               "abgeschaltet gibt es weder Aufruf noch Etikett")
        labels, with_via = schedule(MUC, HH)
        verify(len(with_via) == 1
               and any("meine Strecke" in e and "Gegenrichtung" in e for e in labels),
               "andersherum gefahren zählt auch, und das Etikett sagt es",
               str(labels))
        labels, with_via = schedule(on_the_line(0.2), on_the_line(0.8))
        verify(len(with_via) == 1 and any("meine Strecke" in e for e in labels),
               "ein Teilstück genügt", str(labels))
        labels, with_via = schedule((52.52, 13.405), (51.05, 13.74))
        verify(not with_via and not any("meine Strecke" in e for e in labels),
               "eine Strecke, zu der keine Fahrt passt (Berlin - Dresden), "
               "kostet keine Anfrage mehr")
        labels, with_via = schedule((53.5511, 9.9937), (53.0793, 8.8017))
        verify(not with_via, "und eine kurze, die nur im selben Ort beginnt, auch nicht")
    finally:
        _routing.provider = fallback

    # Cleaning up: the later sections count sessions and trips. The
    # measurement points first: a bulk delete does not cascade, and under SQLite
    # nobody enforces the foreign key - the next session would get the same
    # ID and inherit the orphans.
    db = SessionLocal()
    try:
        db.query(models.LivePoint).filter_by(session_id=old_id).delete()
        db.query(models.LiveSession).filter_by(id=old_id).delete()
        db.commit()
    finally:
        db.close()

    print("\nTomTom als Berater")
    # TomTom delivers suggestions and traffic, nothing of it is stored. Here
    # two functions replace the network: a suggestion along the straight line and
    # a delay per route. The routing is one that makes a path with
    # intermediate points six percent faster - then the suggestion
    # without traffic is the fastest route, and that is exactly what traffic should flip.
    import os as _os
    from app.routing import tomtom as _tt
    from app.routing.demo import DemoRouting as _Demo2
    from app.routing.provider import Route as _Route

    class _TwoWay(_Demo2):
        calls: list = []

        def route(self, start, destination, intermediate_stops=None, preference="recommended",
                  toll_free=False):
            _TwoWay.calls.append(list(intermediate_stops or []))
            r = super().route(start, destination)
            if intermediate_stops:
                return _Route(points=r.points, speed_ms=[t * 1.06 for t in r.speed_ms],
                              distance_m=r.distance_m, drive_time_s=r.drive_time_s / 1.06)
            return r

    line = [(53.5511 + (48.1351 - 53.5511) * i / 50, 9.9937 + (11.5820 - 9.9937) * i / 50)
             for i in range(51)]
    delays: list = []
    counter = {"alternativen": 0, "traffic": 0}
    departures: list = []                 # the departure TomTom received

    def alternativen_fake(start, destination, maximal=5, departure=None):
        counter["alternativen"] += 1
        departures.append(departure)
        return [_tt.Suggestion(points=line, distance_m=780000.0, time_s=30000.0)]

    def traffic_fake(start, destination, between, departure=None):
        nr = counter["traffic"]
        counter["traffic"] += 1
        departures.append(departure)
        mins = delays[nr] if nr < len(delays) else 0.0
        return _tt.Traffic(delay_s=mins * 60.0, time_s=30000.0,
                           without_traffic_s=30000.0 - mins * 60.0)

    # Replace the weather and record the calls: the departure time applies to it too, and
    # without a replacement this section would go to the network.
    from app.energy import weather as _weather
    from app.energy.model import Environment as _Environment
    weather_calls: list = []

    def weather_fake(points, count=6, preset=None, departure=None, duration_s=0.0):
        weather_calls.append((departure, duration_s))
        return lambda lat, lon: _Environment(temp_c=7.0)

    def mean_fake(points, departure=None, duration_s=0.0):
        return _Environment(temp_c=7.0)

    real_weather = (_weather.along_route, _weather.mean)
    _weather.along_route, _weather.mean = weather_fake, mean_fake
    real_old, real_traffic = _tt.alternativen, _tt.traffic
    fallback_routing = _routing.provider
    _routing.provider = lambda: _TwoWay()
    _tt.alternativen, _tt.traffic = alternativen_fake, traffic_fake
    _os.environ["TOMTOM_API_KEY"] = "test"

    tail: dict = {}

    def tomtom_plan(**more):
        _TwoWay.calls = []
        counter["alternativen"] = counter["traffic"] = 0
        response = client.post("/api/route", json={
            "vehicle_id": vehicles[0]["id"],
            "start": {"lat": 53.5511, "lon": 9.9937, "text": "A"},
            "destination": {"lat": 48.1351, "lon": 11.5820, "text": "B"},
            "start_soc": 80.0, "own_trips": False, **more})
        verify(response.status_code == 200, "die Anfrage geht durch",
               f"HTTP {response.status_code}: {response.text[:120]}")
        tail["json"] = response.json() if response.status_code == 200 else {}
        return response.json()["variants"] if response.status_code == 200 else []

    def raw_plan(**more):
        return client.post("/api/route", json={
            "vehicle_id": vehicles[0]["id"],
            "start": {"lat": 53.5511, "lon": 9.9937, "text": "A"},
            "destination": {"lat": 48.1351, "lon": 11.5820, "text": "B"},
            "start_soc": 80.0, "own_trips": False, **more})

    def fastest(variants):
        return [v for v in variants if "insgesamt schnellste" in v["labels"]]

    try:
        delays[:] = [0.0, 0.0]
        vs = tomtom_plan()
        suggestion = [v for v in vs if any(e.startswith("TomTom-Vorschlag") for e in v["labels"])]
        verify(len(vs) == 2 and len(suggestion) == 1,
               "ein Vorschlag von TomTom wird zur zweiten Variante, mit Etikett",
               str([v["labels"] for v in vs]))
        verify(any(len(a) >= 5 for a in _TwoWay.calls),
               "das Routing bekommt ihn als Zwischenpunkte - gespeichert wird die "
               "Strasse von OpenRouteService, nicht die von TomTom")
        verify(counter["alternativen"] == 1 and counter["traffic"] == 2,
               "eine Anfrage nach Vorschlägen und eine Verkehrsabfrage je Route",
               str(counter))
        verify(all("traffic_min" in v and v["traffic_source"] == "TomTom" for v in vs),
               "der Verkehr steht an jeder Variante, mit Quelle")
        verify(len(fastest(vs)) == 1,
               "ohne Verkehr gewinnt genau eine der beiden",
               str([v["labels"] for v in vs]))
        # Whoever wins without traffic gets two hours of congestion. Which one that is
        # is decided by the model (more speed also means more energy and more
        # charging time) - the test does not anticipate it.
        winner_was_tomtom = any(e.startswith("TomTom-Vorschlag")
                                for e in fastest(vs)[0]["labels"])
        # The delays are requested in the order in which the
        # variants come into being: first the fastest from OpenRouteService, then
        # the suggestion.
        delays[:] = [0.0, 120.0] if winner_was_tomtom else [120.0, 0.0]
        vs = tomtom_plan()
        winner_actual_tomtom = any(e.startswith("TomTom-Vorschlag")
                                for e in fastest(vs)[0]["labels"])
        verify(len(fastest(vs)) == 1 and winner_actual_tomtom != winner_was_tomtom,
               "zwei Stunden Stau auf dem bisherigen Sieger drehen die Rangfolge: "
               "Der Verkehr gehört zur Zeit",
               str([v["labels"] for v in vs]))
        jam = [v for v in vs if v.get("traffic_min") == 120.0]
        verify(len(jam) == 1 and jam[0]["plan_total_with_traffic_min"]
               == round(jam[0]["plan_total_minutes"] + 120),
               "und die Gesamtzeit mit Verkehr steht daneben")

        _tt.alternativen = lambda *a, **k: (_ for _ in ()).throw(_tt.TomTomError("Kontingent erschöpft."))
        _tt.traffic = lambda *a, **k: (_ for _ in ()).throw(_tt.TomTomError("Kontingent erschöpft."))
        vs = tomtom_plan()
        verify(len(vs) == 1 and "traffic_min" not in vs[0]
               and not any(e.startswith("TomTom") for e in vs[0]["labels"]),
               "scheitert TomTom, läuft die Planung ohne weiter - ein Berater, der "
               "sie abbrechen liesse, wäre schlechter als keiner",
               str([v["labels"] for v in vs]))

        _tt.alternativen, _tt.traffic = alternativen_fake, traffic_fake
        vs = tomtom_plan(tomtom=False)
        verify(counter == {"alternativen": 0, "traffic": 0}
               and not any("traffic_min" in v for v in vs),
               "abgeschaltet wird TomTom nicht gefragt", str(counter))
        # --- Departure time: traffic and weather apply for this time ---------
        from datetime import datetime as _dt3, timedelta as _td3, timezone as _tz3
        tomorrow = (_dt3.now(_tz3.utc) + _td3(days=1)).replace(microsecond=0)
        delays[:] = [0.0, 0.0]
        departures.clear()
        weather_calls.clear()
        vs = tomtom_plan(departure=tomorrow.isoformat().replace("+00:00", "Z"))
        verify(departures and all(a == tomorrow for a in departures) and len(departures) == 3,
               "die Abfahrt geht an jede TomTom-Anfrage: Vorschläge und Verkehr je Route",
               str(departures))
        verify(weather_calls and all(a == tomorrow and d > 0 for a, d in weather_calls),
               "und ans Wetter, mit der Fahrzeit - sonst läge eine Fahrt morgen "
               "früh auf dem Wetter von heute Nachmittag", str(weather_calls))
        verify(all(v["traffic_basis"] == "prognose" for v in vs),
               "der Verkehr ist als Prognose gekennzeichnet")
        verify(tail["json"].get("departure") == tomorrow.isoformat(),
               "und die Antwort nennt die Abfahrt, mit der gerechnet wurde",
               str(tail["json"].get("departure")))

        departures.clear()
        weather_calls.clear()
        vs = tomtom_plan()
        verify(all(a is None for a in departures) and tail["json"]["departure"] is None
               and all(v["traffic_basis"] == "live" for v in vs),
               "ohne Abfahrt gilt jetzt: Live-Verkehr, keine Abfahrt in der Antwort")
        verify(weather_calls and all(a is None for a, _ in weather_calls),
               "und das aktuelle Wetter")

        departures.clear()
        tomtom_plan(departure=(_dt3.now(_tz3.utc) + _td3(minutes=3)).isoformat())
        verify(all(a is None for a in departures) and tail["json"]["departure"] is None,
               "eine Abfahrt in drei Minuten ist jetzt - wer die Uhrzeit eintippt, "
               "braucht eine Weile")
        tomtom_plan(departure=(_dt3.now(_tz3.utc) - _td3(minutes=4)).isoformat())
        verify(tail["json"]["departure"] is None,
               "und vor vier Minuten auch - es gilt jetzt")

        departures.clear()
        naiv = (_dt3.now(_tz3.utc) + _td3(days=2)).replace(microsecond=0, tzinfo=None)
        tomtom_plan(departure=naiv.isoformat())
        verify(departures and all(a.replace(tzinfo=None) == naiv for a in departures),
               "ohne Zeitzone gilt UTC - nicht stillschweigend die Ortszeit des Servers")

        elapsed = raw_plan(departure=(_dt3.now(_tz3.utc) - _td3(days=1)).isoformat())
        verify(elapsed.status_code == 422 and "Vergangenheit" in elapsed.text,
               "eine Abfahrt von gestern ist ein Tippfehler und wird abgelehnt, "
               "statt stillschweigend mit jetzt zu rechnen",
               f"HTTP {elapsed.status_code}: {elapsed.text[:100]}")
        far = raw_plan(departure=(_dt3.now(_tz3.utc) + _td3(days=61)).isoformat())
        verify(far.status_code == 422 and "60 Tage" in far.text,
               "und eine in 61 Tagen auch - so weit reicht keine Prognose",
               f"HTTP {far.status_code}: {far.text[:100]}")
        ok60 = raw_plan(departure=(_dt3.now(_tz3.utc) + _td3(days=59)).isoformat())
        verify(ok60.status_code == 200, "59 Tage gehen",
               f"HTTP {ok60.status_code}: {ok60.text[:100]}")
        nonsense = raw_plan(departure="morgen früh")
        verify(nonsense.status_code == 422, "und ein Wert, der kein Zeitpunkt ist, auch")

        del _os.environ["TOMTOM_API_KEY"]
        vs = tomtom_plan()
        verify(counter == {"alternativen": 0, "traffic": 0} and len(vs) == 1,
               "und ohne Schlüssel auch nicht - die Planung läuft wie bisher",
               str(counter))
    finally:
        _tt.alternativen, _tt.traffic = real_old, real_traffic
        _weather.along_route, _weather.mean = real_weather
        _routing.provider = fallback_routing
        _os.environ.pop("TOMTOM_API_KEY", None)

    print("\nZeitangaben und Aufzeichnungsstart")
    # The server stores UTC without a zone. `isoformat()` of such a time has
    # no Z - and a browser reads that as local time: in summer time it showed
    # 15:56 where it was 17:56, and every comparison with Date.now() was two
    # hours off. Every time therefore leaves the server with a Z.
    from datetime import datetime as _dz, timezone as _tzz
    # A recording has no energy profile from which a charge level could be
    # estimated. Without a measurement the live display stayed empty - until the car
    # answered for the first time, and it does not while parked (alarm system).
    start = client.post("/api/live/aufzeichnung", json={
        "vehicle_id": vehicles[0]["id"], "lat": 48.4770, "lon": 9.1444,
        "soc": 79.6, "name": "Start"}).json()
    sid = start["session_id"]
    first_item = client.get(f"/api/live/{sid}/punkte").json()["points"]
    verify(len(first_item) == 1 and first_item[0]["soc"] == 79.6,
           "der Startladestand ist der erste Messpunkt der Aufzeichnung",
           f"{len(first_item)} Punkte, erster: {first_item[:1]}")
    gps = client.post(f"/api/live/{sid}/punkt", json={
        "lat": 48.4771, "lon": 9.1445}).json()
    verify(gps["actual_soc"] == 79.6 and gps["soc_reported"] is False
           and gps["soc_source"] == "zuletzt",
           "ein Punkt nur mit Position zeigt die letzte Messung - und sagt, dass "
           "sie es ist, statt leer zu bleiben",
           f"{gps['actual_soc']} / {gps['soc_reported']} / {gps['soc_source']}")
    measured = client.post(f"/api/live/{sid}/punkt", json={
        "lat": 48.4772, "lon": 9.1446, "soc": 79.2}).json()
    verify(measured["actual_soc"] == 79.2 and measured["soc_reported"] is True
           and measured["soc_source"] == "gemessen",
           "und ein gemessener Wert ersetzt sie", str(measured["soc_source"]))
    only_gps = client.post(f"/api/live/{sid}/punkt", json={
        "lat": 48.4773, "lon": 9.1447}).json()
    verify(only_gps["actual_soc"] == 79.2 and only_gps["soc_source"] == "zuletzt",
           "danach gilt die neueste Messung, nicht die vom Start")
    client.post(f"/api/live/{sid}/ende")

    points_time = client.get(f"/api/live/{sid}/punkte").json()["points"]
    verify(points_time and all(p["timestamp"].endswith("Z") for p in points_time),
           "die Messzeiten tragen ein Z - sonst läse der Browser UTC als Ortszeit",
           str(points_time[:1]))
    verify(all(_dz.fromisoformat(p["timestamp"]).tzinfo is not None for p in points_time),
           "und sind für Python zonenbewusst lesbar")
    trips_list = client.get("/api/fahrten").json()
    verify(trips_list and all(f["created_at"].endswith("Z") for f in trips_list),
           "auch das Datum in der Fahrtenliste", str(trips_list[:1]))
    now_utc = _dz.now(_tzz.utc)
    newest = max(_dz.fromisoformat(f["created_at"]) for f in trips_list)
    verify(abs((now_utc - newest).total_seconds()) < 3600,
           "und es ist wirklich UTC: die jüngste Fahrt liegt höchstens eine "
           "Stunde zurück, nicht zwei Stunden daneben",
           f"{newest} gegen {now_utc}")

    without = client.post("/api/live/aufzeichnung", json={
        "vehicle_id": vehicles[0]["id"], "lat": 48.4770, "lon": 9.1444,
        "name": "Ohne Auto"}).json()
    verify(client.get(f"/api/live/{without['session_id']}/punkte").json()["points"] == [],
           "ohne gemeldeten Startladestand gibt es keinen Startpunkt - es wird "
           "nichts erfunden")
    empty = client.post(f"/api/live/{without['session_id']}/punkt", json={
        "lat": 48.4771, "lon": 9.1445}).json()
    verify(empty["actual_soc"] is None,
           "und der Ladestand bleibt unbekannt, statt mit 100 % zu raten",
           str(empty["actual_soc"]))
    client.post(f"/api/live/{without['session_id']}/ende")

    print("\nLadepunkte im Korridor")
    corridor = client.get(f"/api/saeulen/entlang/{trip_id}",
                          params={"min_kw": 100, "radius_km": 25}).json()
    places = {k["name"].split()[0] for k in corridor["candidates"]}
    verify(corridor["count"] == 4,
           "alle vier Schnelllader entlang der Route gefunden",
           f"sind {corridor['count']}: {places}")
    verify(not any("Sylt" in k["name"] for k in corridor["candidates"]),
           "Sylt liegt nicht auf dem Weg und taucht nicht auf", str(places))
    verify(not any(k["max_kw"] < 100 for k in corridor["candidates"]),
           "der 22-kW-Anschluss in Lüneburg fällt durch den Leistungsfilter")
    km = [k["km_on_route"] for k in corridor["candidates"]]
    verify(km == sorted(km), "sortiert nach Fortschritt entlang der Route", str(km))
    verify(all(k["detour_minutes"] > 0 for k in corridor["candidates"]),
           "jeder Kandidat hat einen bezifferten Umweg")

    first = corridor["candidates"][0]
    client.post(f"/api/saeulen/{first['id']}/belegt")
    after = client.get(f"/api/saeulen/entlang/{trip_id}",
                         params={"min_kw": 100, "radius_km": 25}).json()
    reported = [k for k in after["candidates"] if k["id"] == first["id"]]
    verify(reported and reported[0]["occupied_reported"] is True,
           "eine Belegt-Meldung schlägt in der Korridor-Antwort durch")
    client.delete(f"/api/saeulen/{first['id']}/belegt")

    print("\nLadeplan")
    # The demo route is the straight line, the charging points of the sample sit on
    # the A7. That puts them further off the route than they would be beside a
    # real road - hence a more generous detour limit here than
    # the ten minutes the optimizer otherwise works with.
    CHARGE_PLAN = {"min_kw": 100, "radius_km": 25, "detour_limit_min": 15}
    plan = client.post(f"/api/fahrten/{trip_id}/ladeplan",
                       params=CHARGE_PLAN).json()
    verify(plan["feasible"] is True,
           "für die Strecke, die ohne Nachladen nicht reicht, entsteht ein Plan",
           plan.get("reason", ""))
    verify(plan["stop_count"] >= 1, "mit mindestens einem Ladestopp",
           f"{plan['stop_count']}")
    verify(plan["soc_at_target"] >= vehicles[0]["target_soc"] - 0.5,
           "und der Ziel-Ladestand wird erreicht",
           f"{plan['soc_at_target']} % statt {vehicles[0]['target_soc']} %")
    verify(plan["total_minutes"] > plan["drive_time_minutes"],
           "die Gesamtzeit liegt über der reinen Fahrzeit - Laden kostet Zeit")
    km_stops = [s["km_on_route"] for s in plan["stops"]]
    verify(km_stops == sorted(km_stops),
           "die Stopps stehen in Fahrtreihenfolge", str(km_stops))
    verify(all(s["arrival_soc"] >= vehicles[0]["reserve_soc"] - 0.5
               for s in plan["stops"]),
           "an keinem Stopp wird unter der Reserve angekommen",
           str([s["arrival_soc"] for s in plan["stops"]]))
    verify(all(s["departure_soc"] > s["arrival_soc"] for s in plan["stops"]),
           "und an jedem Stopp wird tatsächlich geladen")
    verify(all(s["lat"] and s["lon"] for s in plan["stops"]),
           "jeder Stopp hat eine Koordinate für die Karte")

    # The occupied report is the only availability information that is accurate -
    # it must change the plan, not just colour the list.
    if plan["stops"]:
        planned = plan["stops"][0]["id"]
        client.post(f"/api/saeulen/{planned}/belegt")
        afterwards = client.post(f"/api/fahrten/{trip_id}/ladeplan",
                             params=CHARGE_PLAN).json()
        verify(planned not in [s["id"] for s in afterwards["stops"]],
               "ein als belegt gemeldeter Stopp verschwindet aus dem Plan")
        client.delete(f"/api/saeulen/{planned}/belegt")

    # The "effort per stop" slider is passed through to the optimizer. At zero
    # stopping is free, and the plan shatters into short stops - exactly the
    # behaviour the default of five minutes prevents.
    gratis = client.post(f"/api/fahrten/{trip_id}/ladeplan",
                         params={**CHARGE_PLAN, "stop_fixed_cost_min": 0}).json()
    expensive = client.post(f"/api/fahrten/{trip_id}/ladeplan",
                        params={**CHARGE_PLAN, "stop_fixed_cost_min": 20}).json()
    verify(gratis["holding_cost_minutes"] == 0,
           "mit Aufwand null kostet ein Halt nichts",
           str(gratis["holding_cost_minutes"]))
    verify(expensive["stop_count"] <= gratis["stop_count"],
           "und je teurer ein Halt, desto weniger Halte plant jolt",
           f"{expensive['stop_count']} bei 20 min gegen "
           f"{gratis['stop_count']} bei 0 min")
    verify(expensive["holding_cost_minutes"] == expensive["stop_count"] * 20,
           "die Haltekosten in der Bilanz sind Anzahl mal Aufwand",
           f"{expensive['holding_cost_minutes']} bei {expensive['stop_count']} Stopps")

    eng = client.post(f"/api/fahrten/{trip_id}/ladeplan",
                      params={**CHARGE_PLAN, "detour_limit_min": 0.5}).json()
    verify(eng["feasible"] is False,
           "mit einer Umweg-Grenze unter jedem Kandidaten bleibt nichts übrig")
    verify(bool(eng["reason"]),
           "und die Antwort sagt, warum - nicht nur, dass es nicht geht",
           eng["reason"])

    without_profile = client.post("/api/fahrten/999999/ladeplan")
    verify(without_profile.status_code == 404,
           "eine unbekannte Fahrt wird sauber abgelehnt",
           f"HTTP {without_profile.status_code}")

    print("\nLive-Nachführung")
    session = client.post(f"/api/live/start/{trip_id}").json()
    session_id = session["session_id"]

    db = SessionLocal()
    try:
        trip = db.get(models.Trip, trip_id)
        points_scheduled = simulator.steps(trip, extra_consumption=1.0)
        points_hungry = simulator.steps(trip, extra_consumption=1.25)
    finally:
        db.close()
    verify(len(points_scheduled) > 12, "der Simulator erzeugt Messpunkte",
           f"sind {len(points_scheduled)}")
    verify(len(points_hungry) < len(points_scheduled),
           "mit 25 % Mehrverbrauch kommt er sichtbar kürzer, bevor der Akku "
           "leer ist",
           f"{points_hungry[-1]['km']} km gegen "
           f"{points_scheduled[-1]['km']} km")
    verify(points_hungry[8]["soc"] < points_scheduled[8]["soc"] - 1.0,
           "und liegt auf halber Strecke deutlich tiefer",
           f"{points_hungry[8]['soc']} gegen {points_scheduled[8]['soc']} %")

    # Driving according to plan: the tracking must not trigger.
    for sample in points_scheduled[:12]:
        state = client.post(f"/api/live/{session_id}/punkt", json={
            "lat": sample["lat"], "lon": sample["lon"],
            "soc": sample["soc"]}).json()
    verify(abs(state["deviation_pp"]) < 1.0,
           "wer nach Plan fährt, weicht nicht ab",
           f"{state['deviation_pp']} Prozentpunkte")
    verify(0.9 < state["consumption_factor"] < 1.1,
           "und der Verbrauchsfaktor bleibt bei 1",
           f"ist {state['consumption_factor']}")
    verify(state["spacing_to_route_m"] < 500,
           "die Position liegt auf der Route")

    # Excess consumption: now the tracking must trigger.
    sitzung2 = client.post(f"/api/live/start/{trip_id}").json()["session_id"]
    for sample in points_hungry[:12]:
        zustand2 = client.post(f"/api/live/{sitzung2}/punkt", json={
            "lat": sample["lat"], "lon": sample["lon"],
            "soc": sample["soc"]}).json()
    verify(zustand2["consumption_factor"] > 1.15,
           "25 % Mehrverbrauch werden als Faktor erkannt",
           f"ist {zustand2['consumption_factor']}")
    verify(zustand2["deviation_pp"] < state["deviation_pp"],
           "der Ist-SoC liegt unter dem Soll",
           f"{zustand2['deviation_pp']} Prozentpunkte")
    verify(zustand2["reserve_at_km"] is not None
           and zustand2["reserve_at_km"] < route["reserve_at_km"],
           "und die Reserve rückt nach vorn - genau das ist die Live-Funktion",
           f"geplant km {route['reserve_at_km']}, "
           f"jetzt km {zustand2['reserve_at_km']}")
    verify(zustand2["replanning_required"] is True,
           "die Neuplanung wird angefordert", zustand2["reason"])

    # Off the route. Only the measurement is checked here - that a re-plan only
    # follows after a minute depends on timestamps and is therefore in
    # check_replanning.py, where they can be set.
    off_route = client.post(f"/api/live/{sitzung2}/punkt", json={
        "lat": 54.9, "lon": 8.31, "soc": 40.0}).json()
    verify(off_route["spacing_to_route_m"] > 500,
           "ein Sprung weg von der Route wird als Abstand erkannt",
           f"{off_route['spacing_to_route_m']} m")

    ended_at = client.post(f"/api/live/{sitzung2}/ende").json()
    verify(ended_at["ok"] is True, "die Sitzung lässt sich beenden")
    locked = client.post(f"/api/live/{sitzung2}/punkt", json={
        "lat": 52.0, "lon": 10.0, "soc": 30.0})
    verify(locked.status_code == 409,
           "danach werden keine Messpunkte mehr angenommen",
           f"HTTP {locked.status_code}")

    print("\nNachgereichte Messpunkte (Funkloch-Puffer)")
    # A phone without network collects points and submits them later. For that the
    # point needs a measurement time - otherwise all would sit on the second of
    # the submission, and the time factor (which models the congestion) would be nonsense.
    from datetime import datetime, timedelta, timezone
    buffer = client.post(f"/api/live/start/{trip_id}").json()["session_id"]
    now_ts = datetime.now(timezone.utc)
    batch = []
    for nr, mp in enumerate(points_scheduled[:6]):
        batch.append({"lat": mp["lat"], "lon": mp["lon"], "soc": mp["soc"],
                       "timestamp": (now_ts - timedelta(minutes=60 - 5 * nr)
                                ).isoformat().replace("+00:00", "Z")})
    # Deliberately in the wrong order: the server sorts by measurement time.
    batch.reverse()
    response = client.post(f"/api/live/{buffer}/punkte", json={"points": batch})
    verify(response.status_code == 200, "ein Stapel wird angenommen",
           f"HTTP {response.status_code} {response.text[:120]}")
    times = client.get(f"/api/live/{buffer}/punkte").json()["points"]
    verify(len(times) == 6, "alle sechs Punkte sind gespeichert", f"{len(times)}")
    ts = [z["timestamp"] for z in times]
    verify(ts == sorted(ts) and len(set(ts)) == 6,
           "mit ihrer Messzeit und nicht mit der des Nachreichens, in "
           "richtiger Reihenfolge", str(ts[:3]))
    expected = now_ts - timedelta(minutes=60)
    verify(abs((datetime.fromisoformat(ts[0]) - expected).total_seconds()) < 5,
           "der erste Punkt liegt eine Stunde zurück - Zone Z wurde als UTC gelesen",
           ts[0])
    verify(response.json().get("kind") == "zustand"
           and "consumption_factor" in response.json(),
           "die Antwort hat dieselbe Form wie bei /punkt")

    single = client.post(f"/api/live/{buffer}/punkt", json={
        "lat": points_scheduled[6]["lat"], "lon": points_scheduled[6]["lon"],
        "soc": points_scheduled[6]["soc"],
        "timestamp": (now_ts - timedelta(minutes=29)).isoformat()})
    verify(single.status_code == 200, "auch /punkt kennt die Messzeit",
           f"HTTP {single.status_code}")

    future = client.post(f"/api/live/{buffer}/punkt", json={
        "lat": 52.0, "lon": 10.0, "soc": 50.0,
        "timestamp": (now_ts + timedelta(hours=1)).isoformat()})
    verify(future.status_code == 422,
           "ein Zeitstempel aus der Zukunft wird abgelehnt",
           f"HTTP {future.status_code}")
    old = client.post(f"/api/live/{buffer}/punkt", json={
        "lat": 52.0, "lon": 10.0, "soc": 50.0, "timestamp": "1970-01-01T00:00:00Z"})
    verify(old.status_code == 422, "und einer aus dem Jahr 1970",
           f"HTTP {old.status_code}")
    earlier = len(client.get(f"/api/live/{buffer}/punkte").json()["points"])
    half = client.post(f"/api/live/{buffer}/punkte", json={"points": [
        {"lat": 52.0, "lon": 10.0, "soc": 50.0},
        {"lat": 52.0, "lon": 10.0, "soc": 50.0, "timestamp": "1970-01-01T00:00:00Z"}]})
    after = len(client.get(f"/api/live/{buffer}/punkte").json()["points"])
    verify(half.status_code == 422 and earlier == after,
           "ein schlechter Punkt im Stapel lehnt den ganzen Stapel ab - "
           "ohne dass der gute vorher geschrieben wurde",
           f"HTTP {half.status_code}, {earlier} -> {after} Punkte")
    empty = client.post(f"/api/live/{buffer}/punkte", json={"points": []})
    verify(empty.status_code == 422, "ein leerer Stapel ist ein Fehler",
           f"HTTP {empty.status_code}")
    # While parked jolt asks the car nothing, but keeps measuring the 12 V voltage
    # and sends it along: a point whose raw values contain only `batt_v`.
    only_voltage = client.post(f"/api/live/{buffer}/punkt", json={
        "lat": points_scheduled[6]["lat"], "lon": points_scheduled[6]["lon"],
        "raw_values": {"batt_v": 13.9}})
    verify(only_voltage.status_code == 200
           and only_voltage.json().get("kind") == "zustand",
           "ein Punkt ohne Fahrzeugabfrage, nur mit der 12-V-Spannung, wird "
           "angenommen - ohne Zähler und ohne Ladestand",
           f"HTTP {only_voltage.status_code}: {only_voltage.text[:120]}")
    client.post(f"/api/live/{buffer}/ende")
    to = client.post(f"/api/live/{buffer}/punkte", json={"points": [
        {"lat": 52.0, "lon": 10.0, "soc": 50.0}]})
    verify(to.status_code == 409,
           "in eine beendete Sitzung geht auch kein Stapel", f"HTTP {to.status_code}")

    print("\nLogger im Auto meldet sich über das Fahrzeug")
    # A device permanently installed in the car cannot know the session ID:
    # it is created when setting off in the app and changes with every trip.
    # It therefore identifies itself with the vehicle's logger token.
    vehicle_id = vehicles[0]["id"]
    client.post(f"/api/live/{session_id}/ende")      # first create some quiet

    wrong = client.post("/api/live/melden", json={
        "token": "gibtesnicht", "lat": 53.5, "lon": 10.0, "soc": 50.0})
    verify(wrong.status_code == 401,
           "ein unbekanntes Token wird abgewiesen",
           f"HTTP {wrong.status_code}")

    token = client.post(
        f"/api/fahrzeuge/{vehicle_id}/logger-token").json()["logger_token"]
    verify(len(token) >= 32, "ein Logger-Token lässt sich erzeugen",
           f"{len(token)} Zeichen")
    lst = client.get("/api/fahrzeuge").json()[0]
    verify(lst.get("logger_active") is True,
           "das Fahrzeug meldet, dass ein Logger eingerichtet ist")
    verify("logger_token" not in lst,
           "das Token selbst steht in keiner Listenantwort - es wird genau "
           "einmal gezeigt", str(list(lst.keys())))

    # The car is parked outside and the logger sends anyway. That is not an
    # error: an unattended device that receives error responses starts
    # logging or switches itself off.
    idle = client.post("/api/live/melden", json={
        "token": token, "lat": 53.5, "lon": 10.0, "soc": 50.0})
    verify(idle.status_code == 200
           and idle.json().get("recorded") is False,
           "ohne laufende Fahrt wird nichts aufgenommen - aber es ist kein "
           "Fehler", f"HTTP {idle.status_code}: {idle.text[:120]}")

    sitzung3 = client.post(f"/api/live/start/{trip_id}").json()["session_id"]
    sample = points_scheduled[3]
    reported = client.post("/api/live/melden", json={
        "token": token, "lat": sample["lat"], "lon": sample["lon"],
        "soc": sample["soc"]})
    verify(reported.status_code == 200
           and reported.json().get("recorded") is True,
           "sobald eine Fahrt läuft, findet der Logger sie von allein",
           f"HTTP {reported.status_code}: {reported.text[:120]}")
    verify(reported.json().get("session_id") == sitzung3,
           "und zwar die richtige", f"{reported.json().get('session_id')} "
           f"statt {sitzung3}")
    verify(client.get(f"/api/live/{sitzung3}").json()["points"] == 1,
           "der Messpunkt liegt in dieser Sitzung")

    # Foreign format: the same message, in the language of Iternio/ABRP. That
    # is the way the OBD2 data will come in - translation
    # happens in live/quellen/, details are checked by check_sources.py.
    foreign = client.post("/api/live/melden", json={
        "token": token, "format": "abrp",
        "tlm": {"utc": 1787654321, "soc": 44.0, "lat": sample["lat"],
                "lon": sample["lon"], "speed": 98.0, "ext_temp": 19.0,
                "is_charging": 0}})
    verify(foreign.status_code == 200
           and foreign.json().get("recorded") is True,
           "eine Meldung im ABRP-Format wird angenommen",
           f"HTTP {foreign.status_code}: {foreign.text[:140]}")
    verify(foreign.json().get("actual_soc") == 44.0,
           "und der Ladestand kommt übersetzt an",
           str(foreign.json().get("actual_soc")))
    verify(client.get(f"/api/live/{sitzung3}").json()["points"] == 2,
           "der übersetzte Punkt liegt in derselben Sitzung")

    broken = client.post("/api/live/melden", json={
        "token": token, "format": "abrp",
        "tlm": {"utc": 1787654321, "lat": 48.0, "lon": 11.0}})
    verify(broken.status_code == 400,
           "eine Meldung ohne Ladestand wird abgelehnt",
           f"HTTP {broken.status_code}")
    verify("soc" in broken.text.lower(),
           "und der Grund nennt das fehlende Feld", broken.text[:140])

    unknown = client.post("/api/live/melden", json={
        "token": token, "format": "torque", "lat": 48.0, "lon": 11.0,
        "soc": 50.0})
    verify(unknown.status_code == 400,
           "ein unbekanntes Format wird abgelehnt",
           f"HTTP {unknown.status_code}")

    # A new token invalidates the old one - otherwise "renew" would be worthless.
    newOne = client.post(
        f"/api/fahrzeuge/{vehicle_id}/logger-token").json()["logger_token"]
    verify(newOne != token, "ein erneuertes Token ist ein anderes")
    old = client.post("/api/live/melden", json={
        "token": token, "lat": sample["lat"], "lon": sample["lon"],
        "soc": sample["soc"]})
    verify(old.status_code == 401, "und das alte gilt nicht mehr",
           f"HTTP {old.status_code}")

    client.delete(f"/api/fahrzeuge/{vehicle_id}/logger-token")
    verify(client.get("/api/fahrzeuge").json()[0].get("logger_active") is False,
           "der Logger lässt sich wieder abmelden")
    invalidated = client.post("/api/live/melden", json={
        "token": newOne, "lat": sample["lat"], "lon": sample["lon"],
        "soc": sample["soc"]})
    verify(invalidated.status_code == 401,
           "danach wird von ihm nichts mehr angenommen",
           f"HTTP {invalidated.status_code}")
    client.post(f"/api/live/{sitzung3}/ende")

    print("\nOberfläche wird ausgeliefert")
    page = client.get("/")
    verify(page.status_code == 200 and b"jolt" in page.content.lower(),
           "index.html kommt zurück")
    verify(client.get("/manifest.json").status_code == 200, "manifest.json auch")

    # The bug that has struck four times: index.html is never
    # cached, but the files under /static are - Cloudflare
    # replaces the origin's no-cache there with max-age=14400. The
    # browser fetches fresh HTML and does not even ask for the JavaScript.
    # Four hours of new UI with old logic.
    contents = page.content
    verify(b"/static/app.js?v=" in contents and b"/static/trips.js?v=" in contents,
           "die Skriptverweise in index.html tragen eine Version - sonst "
           "zieht frisches HTML altes JavaScript nach",
           str([z for z in contents.split() if b"app.js" in z][:2]))
    verify(b'"/static/core.js"' not in contents,
           "und zwar alle, nicht nur einige",
           "core.js steht ohne Version im HTML")
    # The case that always threatens with a new script: it is in the HTML,
    # but not in INDEX_FILES (backend/app/main.py) - then it stays stuck in the cache for up to
    # four hours. That is why *every* reference is checked.
    import re as _re
    without_version = _re.findall(rb'src="(/static/[^"?]+\.js)"', contents)
    verify(not without_version,
           "kein einziger Skriptverweis in index.html ohne Version - ein neues "
           "Skript gehoert in INDEX_FILES", str(without_version))

    # The OBD2 page sits outside /static, because Cloudflare imposes a
    # browser lifetime of four hours on everything below it. When troubleshooting
    # in the car that is the difference between "the change has
    # no effect" and "the change is not even there yet".
    obd = client.get("/obd")
    verify(obd.status_code == 200 and b"aufzeichnen" in obd.content.lower(),
           "die Aufzeichnungsseite wird unter /obd ausgeliefert",
           f"HTTP {obd.status_code}")
    verify("no-cache" in obd.headers.get("Cache-Control", ""),
           "und zwar ohne Cache - sonst hängt das Telefon auf einer alten "
           "Fassung fest", obd.headers.get("Cache-Control", "(keiner)"))
    verify(b"/static/obd.js?v=" in obd.content
           and b"/static/obd.css?v=" in obd.content,
           "die Verweise auf Skript und Stylesheet tragen eine Version - "
           "sonst zieht eine frische Seite altes JavaScript nach",
           str([z for z in obd.content.split() if b"obd." in z][:3]))
    # A manifest of its own, so that the page sits as an icon on the
    # home screen. Without it, recording is a detour through Bluefy
    # and the address bar - and thus something you put off until "next time".
    obd_manifest = client.get("/manifest-obd.json")
    verify(obd_manifest.status_code == 200
           and obd_manifest.json().get("start_url") == "/obd",
           "und hat ein eigenes Manifest, das direkt auf /obd startet",
           f"HTTP {obd_manifest.status_code}")
    verify(b"/manifest-obd.json" in obd.content,
           "auf das die Seite auch verweist - ein Manifest, das niemand "
           "verlinkt, legt kein Symbol an")

    verify(client.get("/static/map.js").status_code == 200, "und die Skripte")

    # The settings: one tab, one section, one script - and in the
    # service worker shell, otherwise the view is missing on poor reception.
    page_text = client.get("/").text
    sw_text = client.get("/sw.js").text
    verify('data-ansicht="einstellungen"' in page_text
           and 'id="ansicht-einstellungen"' in page_text
           and "/static/settings.js" in page_text,
           "die Einstellungen haben Reiter, Abschnitt und Skript")
    verify("/static/tiles.js" in page_text and "/static/tiles.js" in sw_text
           and client.get("/static/tiles.js").status_code == 200,
           "die gezeichneten CarPlay-Kacheln werden ausgeliefert und stehen im Gerüst")
    verify(client.get("/static/settings.js").status_code == 200
           and "/static/settings.js" in sw_text,
           "das Skript wird ausgeliefert und steht im Gerüst des Service Workers")

    # Every value read out needs a label, otherwise the dashboard shows
    # "ptc_current_a" instead of "Heizstrom". The list sits as a table
    # in readings.js, the interpreter in obd-core.js, and the UI
    # obtains it via `FELDER` - otherwise a new data identifier never shows up
    # there. Comments are dropped beforehand so that a word in one does not count
    # as a field.
    core = open(os.path.join(FRONTEND, "obd-core.js"), encoding="utf-8").read()
    table = open(os.path.join(FRONTEND, "readings.js"),
                   encoding="utf-8").read()
    table = re.sub(r"/\*.*?\*/", "", table, flags=re.S)
    table = re.sub(r"^\s*//.*$", "", table, flags=re.M)
    # Main values (4 spaces) and the values from `auch` (8) alike.
    entries = re.findall(
        r'\{ name: "([a-z_]+)",(.*?)(?=\{ name:|\n  \],\n\};)', table, re.S)

    def entry(name: str) -> str:
        """The table text of a main value, including its `auch` values."""
        hit = re.search(r'\n    \{ name: "%s",(.*?)(?=\n    \{ name:|\n  \],\n\};)'
                            % name, table, re.S)
        return hit.group(1) if hit else ""

    without = [n for n, rest in entries if "title:" not in rest]
    verify(entries and not without,
           f"alle {len(entries)} ausgelesenen Messwerte tragen eine "
           f"Beschriftung fürs Dashboard", str(without))
    missing_unit = [n for n, rest in entries
                        if "unit:" not in rest]
    verify(not missing_unit,
           "und eine Einheit - auch wenn sie null ist, muss die Entscheidung "
           "dastehen", str(missing_unit))
    verify("FIELDS: READINGS.flatMap" in core,
           "die Liste wird exportiert statt in der Oberfläche wiederholt")
    live = open(os.path.join(FRONTEND, "live.js"), encoding="utf-8").read()
    verify("joltObd.FIELDS" in live,
           "und das Dashboard bezieht sie von dort - eine neue Datenkennung "
           "taucht damit von selbst auf")
    # Recording needs a **separate** vehicle choice. Before, it used
    # the one from the Plan view and, when that was empty, fell back on the
    # first vehicle in the list - the "Allgemeine E-Auto" created at first
    # start. Two real test drives were attributed to the wrong car that way.
    html = open(os.path.join(FRONTEND, "index.html"), encoding="utf-8").read()
    trips_js = open(os.path.join(FRONTEND, "trips.js"),
                      encoding="utf-8").read()
    verify('id="aufz-fahrzeug"' in html,
           "der Aufzeichnungs-Abschnitt hat eine eigene Fahrzeugwahl")
    verify("aufz-fahrzeug" in trips_js and "fahrzeug-wahl" not in trips_js,
           "und das Aufzeichnen nimmt sie, nicht die aus der Planen-Ansicht",
           "trips.js greift noch auf fahrzeug-wahl zu")
    verify("K.state.vehicles || [])[0]" not in trips_js,
           "ohne Rückfall auf das erste Fahrzeug der Liste - lieber gar "
           "nicht aufzeichnen als dem falschen Auto")
    verify("aufz-fahrzeug" in open(os.path.join(FRONTEND, "vehicle.js"),
                                   encoding="utf-8").read(),
           "und sie wird mit den Fahrzeugen gefüllt")

    # A control unit with an 11-bit identifier needs a different protocol.
    # If the switch goes wrong, it must not cost the mandatory values of the same round
    # - that is why these queries come last and the switch
    # is undone in the finally.
    names = re.findall(r'\n    \{ name: "([a-z_]+)"', table)
    climate = [n for n in ("outside_temp_c", "inside_temp_c") if n in names]
    verify(climate and all(names.index(n) > names.index("soc_raw")
                         for n in climate),
           "die Messwerte mit Protokollwechsel stehen hinter dem Ladestand - "
           "ein misslungener Wechsel darf die Pflichtwerte nicht mitreissen",
           str(names))
    verify(names and names[-1] in ("outside_temp_c", "inside_temp_c"),
           "und ganz am Ende der Runde", str(names[-2:]))
    verify("} finally {" in core and 'command("ATSP7")' in core,
           "das Protokoll wird im finally zurückgesetzt - eine Sitzung, die "
           "im falschen Protokoll hängen bleibt, kostet jede weitere Runde")
    # The reconnection must not give up as long as the trip is running. With
    # the old limit of six attempts it was over after two and a half minutes
    # - five minutes with the page in the background cost twenty kilometres
    # without a single vehicle value on a real trip.
    verify("attempt >= bound" not in core,
           "der Wiederaufbau gibt nicht nach sechs Versuchen auf - `weiter` "
           "beendet ihn, wenn die Fahrt endet")
    verify("AGAIN_MAX_DISTANCE_MS" in core,
           "stattdessen ist nur der Abstand gedeckelt")
    # What a **long** trip does differently.
    core_js = open(os.path.join(FRONTEND, "core.js"), encoding="utf-8").read()
    verify("sessionRemember" in core_js and "rememberedSession" in core_js,
           "die laufende Sitzung überlebt ein Neuladen - sonst beginnt jeder "
           "versehentliche Wisch eine neue")
    verify("resumeSession" in live,
           "und die Live-Ansicht nimmt sie beim Start wieder auf")
    verify("donglePause" in live and "function detach" in core,
           "der Dongle lässt sich trennen und pausieren - ein verriegeltes "
           "Auto, das weiter über CAN gefragt wird, löst die Alarmanlage aus")
    verify('id="dongle-an"' in html,
           "und er lässt sich auch auf einer geplanten Fahrt verbinden, "
           "nicht nur beim Aufzeichnen")

    verify("WIDTHS_MIN" in live and "BAR_AT_MOST" in live,
           "die Balkenbreite wächst mit der Fahrt - sechs Stunden wären "
           "sonst 360 Balken auf 340 Pixeln")
    verify("CHARGING_AS_OF_KWH" in live,
           "und der Verbrauch der Fahrt wird abschnittsweise summiert, "
           "damit ein Ladestopp ihn nicht auf null zieht")
    verify("consumption_track.length > 20000" in live,
           "die Messreihe reicht für mehr als zehn Stunden")

    verify("quietReported" in live and "age > 180" in live,
           "und das Dashboard sagt einmal deutlich, wenn nichts mehr aus dem "
           "Auto kommt - eine Aufzeichnung ohne Ladestand taugt nicht zum "
           "Lernen, und das erfährt man sonst erst hinterher")

    verify("changeFailed" in core,
           "und ein gescheiterter Wechsel wird nicht endlos wiederholt")

    # Without flow control every response that does not fit into a CAN
    # frame fails - the ELM327 must know with which header it sends the
    # flow-control packet. The WiCAN vehicle profile sets the three
    # commands before every query; jolt did not set them at all, and that is
    # exactly why the battery current never arrived in a single round.
    for command in ("ATFCSH", "ATFCSD300000", "ATFCSM1"):
        verify(command in core, f"die Flusskontrolle setzt {command}")
    verify(core.count("await flowControl(destination)") >= 2,
           "und zwar auf beiden Wegen - mit und ohne Protokollwechsel")
    verify(all(f'fcsh: "{h}"' in table
               for h in ("17FC007B", "17FC0076", "17FC00B9", "746", "710")),
           "jede Zieladresse bringt ihren eigenen Flow-Control-Kopf mit")

    verify("function multiframe" in core,
           "lange Antworten werden aus mehreren CAN-Rahmen zusammengesetzt - "
           "ohne das landen Köpfe und Steuerbytes als Nutzdaten im Ergebnis")
    verify("hex.slice(3) : hex.slice(8)" in core,
           "und zwar für beide Rahmenbreiten: acht Kopfzeichen bei 29 Bit, "
           "drei bei 11 - der Klimakompressor sitzt auf der 11-Bit-Seite")
    verify("min: 10, max: 200" in entry("battery_kwh"),
           "die Akkukapazität wird gegen eine Plausibilitätsgrenze gehalten - "
           "die Umrechnung ist nicht belegt, also lieber leer als erfunden")
    verify("K.num(z.actual_soc, 1)" in live,
           "der Ladestand steht mit einer Nachkommastelle da - der Dongle "
           "liefert ihn in Schritten von 0,4 pp, auf ganze Prozent gerundet "
           "steht die Zahl minutenlang still")
    verify("drawConsumption" in live and "verbrauchsabschnitte" in live,
           "es gibt einen Balkenplot des Verbrauchs je Zeitabschnitt")
    # The plausibility check while parked. The cross-check is the
    # sharper part: discharge counter divided by odometer must
    # yield a sensible lifetime consumption, and that checks both
    # byte layouts at once - without a single minute driven.
    obd_js = open(os.path.join(FRONTEND, "obd.js"), encoding="utf-8").read()
    obd_html = open(os.path.join(FRONTEND, "obd.html"), encoding="utf-8").read()
    verify('id="pruefen"' in obd_html and "valuesCall" in obd_js,
           "die Diagnoseseite kann alle Werte im Stand prüfen")
    verify("RANGES" in obd_js and "Kreuzvergleich" in obd_js,
           "gegen Bereiche und über einen Kreuzvergleich - der prüft zwei "
           "Formeln auf einmal, ohne dass gefahren werden muss")
    verify('id="klima-a"' in obd_html and 'id="klima-b"' in obd_html
           and "showClimate" in obd_js,
           "und der Klimakompressor über eine Differenzmessung statt über "
           "eine geratene Formel")
    verify("payload_bytes," in core,
           "dafür gibt der Baustein die rohen Nutzbytes heraus")

    verify("downhill: 5, len_total: 2" in entry("compressor_w"),
           "die Kompressorleistung steht drin - aus einer Differenzmessung "
           "abgeleitet, weil keine der drei Quellen eine Formel nennt")
    verify("i += 2" in obd_js,
           "die Differenzanzeige richtet die Byte-Paare aus, statt ein "
           "Fenster byteweise zu schieben - eine Mehrbyte-Zahl fängt nicht "
           "an jedem Byte an")

    verify("sign: true" in entry("discharge_kwh")
           and "Math.pow(2, len_total * 8 - 1)" in core,
           "der Entladezähler wird vorzeichenbehaftet gelesen - unsigned "
           "ergab am Fahrzeug 482 961 statt 17 439 kWh")
    verify("discharge_kwh: [100, 100000" in obd_js,
           "und seine Plausibilitätsschranke fängt genau diesen Fehler - "
           "die alte [1, 999999] liess ihn durch")
    verify("if (inside) good += 1; else bad += 1;" in obd_js,
           "der Kreuzvergleich zählt in die Zusammenfassung - rot in der "
           "Tabelle und \"0 auffällig\" darüber ist schlimmer als nichts")

    verify("divider: 8583.07" in entry("discharge_kwh")
           and "divider: 8583.07" in entry("discharge_kwh").split("auch:")[-1],
           "die Energiezähler des Fahrzeugs werden gelesen - ihre Differenz "
           "ist die verbrauchte Energie, 0,117 Wh statt 339 Wh Auflösung")
    verify('name: "charged_kwh"' in entry("discharge_kwh")
           and "Object.assign(raw, val.further)" in core,
           "und Lade- wie Entladezähler kommen aus **einer** Abfrage - eine "
           "Mehrrahmen-Antwort zweimal zu holen kostet Zeit")
    verify("...(m.also || []).map" in core,
           "auch der mitgelieferte Wert steht in der Feldliste, sonst zeigt "
           "die Tabelle weniger, als gemessen wird")
    verify("SECTION_WITH_COUNTER_S = 60" in live
           and "SECTION_FROM_SOC_S = 300" in live,
           "die Balkenbreite folgt der Quelle: eine Minute mit Zähler, "
           "fünf ohne - nicht dem Wunsch")
    verify("final.net - first.net" in live,
           "und die Balken rechnen mit der Zählerdifferenz, wenn es sie gibt")
    verify("final.gps - first.gps" in live,
           "die Strecke je Balken kommt dagegen aus dem GPS - der "
           "Kilometerstand löst in ganzen Kilometern auf, und eine Minute "
           "sind rund 1,2 km")
    # The raw values belong behind a flap: seventeen lines in the middle of the
    # driving view are noise. And the tiles you read belong above
    # the charts, not below them.
    verify('<details id="live-roh"' in html,
           "die Rohwerte stehen hinter einer Klappe, nicht im Fahrbild")
    verify(html.index('id="live-werte"') < html.index('id="live-verlauf"'),
           "und die Kacheln über den Diagrammen - was man im Fahren liest, "
           "steht oben")
    verify('id="live-auto-stand"' in html.split("<summary>")[1].split("</summary>")[0],
           "das Alter steht in der zugeklappten Zeile - man soll ohne "
           "Aufklappen sehen, ob es lebt")

    verify('fillText("kWh/100"' in live,
           "der Balkenplot hat eine beschriftete Achse - ohne sie sieht man "
           "Unterschiede, aber keine Grössenordnung")
    html_obd = open(os.path.join(FRONTEND, "obd.js"), encoding="utf-8").read()
    verify("btn.disabled = true" in html_obd and "läuft …" in html_obd,
           "der Senden-Knopf sperrt sich, solange eine Befehlsreihe läuft - "
           "sonst fällt ein zweiter Start dem ersten in den Rücken")

    verify("runningConsumption" in live and "CONSUMPTION_FROM_KM" in live,
           "der Verbrauch der laufenden Fahrt wird aus Ladestand und "
           "Kilometerstand gerechnet, erst ab einer Mindeststrecke")
    verify("recVehicle" in live,
           "und kennt dafür das Fahrzeug der Aufzeichnung - ohne Akkugrösse "
           "wird aus einem Ladestand keine Kilowattstunde")

    verify("_empty" in core and "raw._empty = raw._empty" in core,
           "ein Messwert, der antwortet aber nichts liefert, wird vermerkt - "
           "vorher fiel er stumm durch, und vier von dreizehn Werten fehlten "
           "eine ganze Fahrt lang ohne Spur")
    verify("valuesAsOf" in live and "unanswered" in live,
           "das Dashboard hält den letzten bekannten Wert je Messgrösse fest, "
           "statt die Zeile leer zu lassen")
    verify("ageText" in live and 'class="wann"' in live,
           "und schreibt sein Alter daneben - ein alter Wert ist nützlich, "
           "solange man ihm ansieht, dass er alt ist")
    verify("latestRawValuesTime" in live,
           "das Dashboard zeigt, wie alt der letzte Satz aus dem Auto ist - "
           "eine eingefrorene Anzeige sieht sonst aus wie eine laufende")

    # The operations scripts must also run in the container. There the
    # package sits as /srv/app next to /srv/tools, locally under backend/app -
    # whoever knows only one layout fails in the other with
    # `ModuleNotFoundError: No module named 'app'`. That is exactly what happened:
    # `examine.py` was hard-wired to `../backend`, so not a single check script
    # ran via `docker exec` - the very route for which `tools/`
    # was included in the image at all.
    tools = sorted(fs_path for fs_path in os.listdir(TOOLS)
                       if fs_path.endswith(".py"))
    without_both = []
    for name in tools:
        source = open(os.path.join(TOOLS, name), encoding="utf-8").read()
        # Only whoever really imports the package needs the search path - the
        # mere word "app" also appears in tools without an application.
        if not re.search(r"^\s*(from|import) app\b", source, re.M):
            continue
        # Either the script knows both layouts itself, or it leaves it to
        # `pruefen.application_provide`.
        if re.search(r'os\.path\.join\(_?(?:HERE|here|_here), "\.\."\)', source) \
                or "application_provide" in source \
                or name == "examine.py":
            continue
        without_both.append(name)
    verify(not without_both,
           "jedes Werkzeug in tools/ findet das Paket in beiden Layouten - "
           "im Repo unter backend/app, im Image daneben als app",
           str(without_both))
    verify('os.path.join(here, "..")' in
           open(os.path.join(TOOLS, "examine.py"), encoding="utf-8").read(),
           "und examine.py selbst auch - sonst läuft per docker exec kein "
           "einziges Prüfskript")
    verify("Content-Security-Policy" in page.headers,
           "die Security-Header sitzen")

    return verify.balance()


if __name__ == "__main__":
    sys.exit(main())
