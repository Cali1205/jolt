#!/usr/bin/env python3
"""Checks live replanning - stage 3.

Two parts, and the second is the important one:

1. The **triggers** from section 2.3 of the concept, one by one and without
   a database. Each must fire above its threshold and stay quiet below it.
   A trigger that always fires is as useless as one that never does.
2. The **replanning** itself, across the whole chain: compute a trip, create
   charge points along the route, start a live session, replay it with excess
   consumption and with traffic - and check whether the plan changes, whether
   it stays valid in the process, and whether it does *not* change on every
   measurement.

No network, no Postgres, no API keys:

    ./tools/check_replanning.py
"""
import os
import sys
from types import SimpleNamespace
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from examine import Check, application_provide  # noqa: E402

application_provide("umplan")

from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.energy.model import (VehicleValues, Environment,  # noqa: E402
                                haversine_m)
from app.energy.profile import entry_at as _profile_at  # noqa: E402
from app.charging import availability  # noqa: E402
from app.live import session as live_session  # noqa: E402
from app.live import replanning  # noqa: E402
from app.main import app  # noqa: E402

# The weather service is a live network call; the real weather shifts the
# consumption factor this check measures. Fail it so the model always
# calculates with its fixed fallback weather.
import requests  # noqa: E402
from app.energy import weather  # noqa: E402


def _no_network(*args, **kwargs):
    raise requests.ConnectionError("network disabled in this check")


weather.requests.get = _no_network

verify = Check()


class VehicleStubB:
    reserve_soc = 10.0


def trigger(**deviations):
    """Call `_examine_replanning` with entirely unremarkable defaults.

    That way each test case contains only the one quantity it is about - and
    you see at once which one moved the trigger.
    """
    vals = dict(vehicle=VehicleStubB(), deviation=0.0, spacing_m=10.0,
                 detour_since=None, now_ts=datetime(2026, 1, 1, 12, 0),
                 forecast=50.0, reserve_at=None, total_km=600.0,
                 shift=0.0, upcoming=None, arrival_soc=None)
    vals.update(deviations)
    return live_session._examine_replanning(**vals)


# ---------------------------------------------------------------------------
# Part 1: the triggers
# ---------------------------------------------------------------------------

def part_trigger():
    print("\nAuslöser einzeln (ohne Datenbank)")

    required, reason, _ = trigger()
    verify(not required, "ohne Abweichung wird nicht neu geplant", reason)

    required, reason, urgent = trigger(reserve_at=420.0)
    verify(required and urgent,
           "Reserve vor dem Ziel: sofort neu planen", reason)

    required, reason, urgent = trigger(forecast=4.0)
    verify(required and urgent,
           "Prognose unter der Reserve: sofort neu planen", reason)

    # The next stop has been reported as occupied.
    stop = {"id": 4242, "name": "Rasthof Nord", "km_on_route": 200.0,
             "planned_soc": 15.0, "expected_soc": 15.0}
    availability.REPORTS.report(4242)
    try:
        required, reason, urgent = trigger(upcoming=stop, arrival_soc=15.0)
        verify(required and urgent,
               "belegt gemeldeter nächster Stopp: sofort neu planen", reason)
        verify("belegt" in reason, "und der Grund benennt es", reason)
    finally:
        availability.REPORTS.release(4242)

    required, reason, _ = trigger(upcoming=stop, arrival_soc=15.0)
    verify(not required,
           "nach der Freigabe greift derselbe Stopp nicht mehr", reason)

    # Arrival SoC at the next stop: the threshold is 5 percentage points.
    required, reason, _ = trigger(upcoming=stop, arrival_soc=9.0)
    verify(required, "6 pp weniger am nächsten Stopp: neu planen", reason)
    required, reason, _ = trigger(upcoming=stop, arrival_soc=12.0)
    verify(not required, "3 pp weniger bleiben unter der Schwelle", reason)

    # A detour needs duration, not just distance.
    now_ts = datetime(2026, 1, 1, 12, 0)
    required, reason, _ = trigger(spacing_m=900.0, detour_since=now_ts, now_ts=now_ts)
    verify(not required,
           "ein einzelner Ausreisser neben der Route löst nichts aus", reason)
    required, reason, urgent = trigger(
        spacing_m=900.0, detour_since=now_ts, now_ts=now_ts + timedelta(seconds=90))
    verify(required and urgent,
           "90 Sekunden neben der Route dagegen schon", reason)

    # Traffic jam.
    required, reason, _ = trigger(shift=14.0)
    verify(required, "14 min spätere Ankunft: neu planen", reason)
    required, reason, _ = trigger(shift=6.0)
    verify(not required, "6 min bleiben unter der Schwelle", reason)

    # Without a plan, the deviation is the best available statement here.
    required, reason, _ = trigger(deviation=-7.0)
    verify(required, "ohne Plan zählt die Abweichung an der aktuellen Position",
           reason)


class PointStub:
    """A measurement point, as much of it as `_charge_pauses_minutes` touches."""

    NULL = datetime(2026, 1, 1, 8, 0)

    def __init__(self, km: float, soc: float, mins: float):
        self.km_on_route = km
        self.soc = soc
        self.timestamp = self.NULL + timedelta(minutes=mins)


def part_charge_pauses():
    """How much of the elapsed time was a charging pause - and how much not.

    The energy profile contains driving time only; the charging time is in
    the plan. Anyone who holds the wall clock against it unfiltered has a
    delay equal to the charging duration after the first charging stop -
    permanently, because it is never made up.
    """
    print("\nLadepause von Verspätung unterscheiden")

    # 100 km/h, 0.1 percentage points per kilometer.
    profile = [{"km": k, "mins": k * 0.6, "soc": 80 - k * 0.1}
              for k in range(0, 401, 10)]

    # The logger keeps transmitting while charging: thirty minutes at the same
    # place, the charge level rises.
    charging = [PointStub(150.0, 30.0, 90.0), PointStub(150.0, 45.0, 100.0),
             PointStub(150.0, 60.0, 110.0), PointStub(150.0, 70.0, 120.0),
             PointStub(160.0, 68.0, 126.0)]
    measured = live_session._charge_pauses_minutes(charging, profile)
    verify(abs(measured - 30.0) < 0.1,
           "dreissig Minuten an der Säule werden als Ladepause erkannt",
           f"{measured:.1f} min")

    # The same charging stop, but the logger slept and only reports again
    # twenty kilometers later. Even then only the standstill time may count,
    # not the driving time for the twenty kilometers.
    slept = [PointStub(150.0, 30.0, 90.0), PointStub(170.0, 65.0, 132.0)]
    measured = live_session._charge_pauses_minutes(slept, profile)
    verify(abs(measured - 30.0) < 0.1,
           "auch wenn der Logger die Pause verschlafen hat",
           f"{measured:.1f} min")

    # Regeneration on a long descent also raises the charge level - but costs
    # no additional time, so no credit either.
    downhill = [PointStub(150.0, 30.0, 90.0), PointStub(160.0, 31.0, 96.0)]
    measured = live_session._charge_pauses_minutes(downhill, profile)
    verify(measured < 0.5,
           "Rekuperation bergab ist keine Ladepause - das Auto fährt ja",
           f"{measured:.1f} min")

    # Lunch: three quarters of an hour standing still without charging. That
    # really shifts the arrival and has to stay.
    pause = [PointStub(150.0, 30.0, 90.0), PointStub(150.0, 29.8, 135.0)]
    measured = live_session._charge_pauses_minutes(pause, profile)
    verify(measured < 0.5,
           "eine Pause ohne Ladung bleibt Verspätung - Mittagessen verschiebt "
           "die Ankunft wirklich", f"{measured:.1f} min")


# ---------------------------------------------------------------------------
# Part 2: remaining distance
# ---------------------------------------------------------------------------

def part_remaining_distance():
    print("\nReststrecke abschneiden und skalieren")

    profile = [{"km": k, "kwh": k * 0.18, "mins": k * 0.5, "soc": 80 - k * 0.1}
              for k in range(0, 301, 10)]
    rest = replanning.remaining_profile(profile, 100.0)
    verify(rest.km[0] == 0.0, "die Reststrecke beginnt bei km 0",
           f"{rest.km[0]}")
    verify(abs(rest.km[-1] - 200.0) < 0.01, "und endet 200 km später",
           f"{rest.km[-1]}")
    verify(abs(rest.kwh[0]) < 1e-9 and abs(rest.mins[0]) < 1e-9,
           "Energie und Zeit starten ebenfalls bei null")

    scaled = replanning.remaining_profile(profile, 100.0, consumption_factor=1.25,
                                    time_factor=1.4)
    verify(abs(scaled.kwh[-1] - rest.kwh[-1] * 1.25) < 1e-6,
           "der Verbrauchsfaktor skaliert die Energie")
    verify(abs(scaled.mins[-1] - rest.mins[-1] * 1.4) < 1e-6,
           "der Zeitfaktor skaliert die Zeit")
    verify(scaled.km[-1] == rest.km[-1],
           "die Strecke bleibt, was sie ist - gefahren wird nicht weniger")

    # Geometry and profile must get the same zero point, otherwise every leg
    # calculation is off by that offset.
    geo = [[9.0 + i * 0.01, 53.0, 0.0] for i in range(200)]
    segment, km0 = replanning.rest_from(geo, 40.0)
    measured = 0.0
    for i in range(1, len(geo)):
        measured += haversine_m(geo[i - 1][1], geo[i - 1][0],
                                geo[i][1], geo[i][0]) / 1000.0
        if geo[i] == segment[1]:
            break
    verify(km0 <= 40.0, "der Schnittpunkt liegt vor dem gesuchten Kilometer",
           f"km0={km0:.1f}")
    verify(len(segment) > 2 and segment[0] in geo,
           "und die Restgeometrie ist ein echtes Teilstück der Route")


class VehicleStub:
    mass_kg = 2500.0
    c_w = 0.29
    frontal_area_m2 = 2.9
    c_rr = 0.010
    eta_drive = 0.88
    eta_regen = 0.70
    p_aux_w = 350.0
    heat_pump = True
    battery_net_kwh = 77.0
    reserve_soc = 10.0
    correction_factor = 1.0


class TripStub:
    outside_temp_c = 10.0
    vehicle = VehicleStub()


def recompute_part_speed():
    """Recompute the remaining distance with the measured speed instead of scaling.

    The speed used to be **guessed**: the slider in the planning view is set
    to 120 %, and nobody knows whether that is right. Meanwhile the PWA wrote
    the measured speed into a column that nobody ever read.

    Why a single factor does not suffice is the whole point: air drag goes
    with v², rolling resistance nearly linearly, and the auxiliary consumers
    not with speed at all but with time - which decreases when you drive
    faster. A flat surcharge hits none of these three.
    """
    print("\nReststrecke mit gemessenem Tempo neu rechnen")

    # Two hundred kilometers of flat road, a hundred km/h as planned, ten degrees.
    rest = []
    for i in range(41):
        km = i * 5.0
        rest.append({"km": km, "lat": 48.0 + i * 0.045, "lon": 11.0,
                     "elevation": 100.0, "speed_kmh": 100.0,
                     "mins": km * 0.6, "soc": 80 - km * 0.1,
                     "kwh": km * 0.18})
    environment = lambda lat, lon: Environment(temp_c=10.0)      # noqa: E731
    trip = TripStub()

    past_plan = replanning.remaining_profile_physics(trip, rest, 1.0, environment)
    fast = replanning.remaining_profile_physics(trip, rest, 1.2, environment)
    slow = replanning.remaining_profile_physics(trip, rest, 0.8, environment)

    verify(past_plan is not None and len(past_plan.km) > 30,
           "das gespeicherte Profil reicht zum Neurechnen aus - Position, "
           "Höhe und Tempo je Stützstelle stehen darin")
    verify(abs(past_plan.km[-1] - 200.0) < 3.0,
           "und die Strecke kommt dabei heraus, die hineinging",
           f"{past_plan.km[-1]:.1f} km")

    verify(fast.kwh[-1] > past_plan.kwh[-1],
           "zwanzig Prozent schneller kostet mehr Energie",
           f"{fast.kwh[-1]:.1f} gegen {past_plan.kwh[-1]:.1f} kWh")
    verify(fast.mins[-1] < past_plan.mins[-1],
           "und weniger Zeit - beides zugleich, das kann kein Energiefaktor",
           f"{fast.mins[-1]:.0f} gegen {past_plan.mins[-1]:.0f} min")

    # The upper bound is the pure v² share. If the increase were above it,
    # more than just the air drag would have been scaled; if it were zero,
    # the speed would not have arrived at all.
    gain = fast.kwh[-1] / past_plan.kwh[-1]
    verify(1.02 < gain < 1.44,
           "der Mehrverbrauch liegt zwischen spürbar und dem reinen "
           "v²-Faktor - Rollwiderstand und Nebenverbraucher skalieren nicht "
           "mit dem Quadrat", f"×{gain:.3f}")

    verify(slow.kwh[-1] < past_plan.kwh[-1]
           and slow.mins[-1] > past_plan.mins[-1],
           "langsamer fahren dreht beides um",
           f"{slow.kwh[-1]:.1f} kWh in {slow.mins[-1]:.0f} min")

    # Trips from before these profile fields existed must fall back to
    # scaling and not crash.
    without_city = [{k: v for k, v in e.items() if k not in ("lat", "lon")}
                for e in rest]
    verify(replanning.remaining_profile_physics(trip, without_city, 1.2, environment) is None,
           "ohne Position im Profil wird nicht gerechnet, sondern None "
           "gemeldet - der Aufrufer skaliert dann wie bisher")
    verify(replanning.remaining_profile_physics(trip, rest[:1], 1.2, environment) is None,
           "und ein Profil mit einem einzigen Punkt ebenso")


def part_plan_comparison():
    print("\nWann ist ein Plan ein anderer Plan?")

    def plan(*stops):
        return {"feasible": True,
                "stops": [{"id": i, "departure_soc": s, "name": f"LP{i}",
                            "km_on_route": 100.0 * i} for i, s in stops]}

    a = plan((1, 50.0), (2, 60.0))
    verify(replanning.stops_same(a, plan((1, 50.0), (2, 60.0))),
           "derselbe Plan ist derselbe Plan")
    verify(replanning.stops_same(a, plan((1, 51.5), (2, 60.0))),
           "anderthalb Prozentpunkte mehr Ladung sind keine Änderung - "
           "darüber will am Steuer niemand unterrichtet werden")
    verify(not replanning.stops_same(a, plan((1, 58.0), (2, 60.0))),
           "acht Prozentpunkte dagegen schon")
    verify(not replanning.stops_same(a, plan((3, 50.0), (2, 60.0))),
           "ein anderer Standort ist immer eine Änderung")
    verify(not replanning.stops_same(a, plan((1, 50.0))),
           "ein Stopp weniger auch")
    verify(not replanning.stops_same(a, {"feasible": False, "stops": []}),
           "und ein Plan, der nicht mehr aufgeht, erst recht")

    text = replanning.describe_change(a, plan((1, 50.0)))
    verify("1 Ladestopps statt 2" in text or "weniger" in text,
           "die Änderung wird in einem Satz beschrieben", text)


# ---------------------------------------------------------------------------
# Part 3: the whole chain
# ---------------------------------------------------------------------------

def prepare_trip(client) -> dict:
    """Compute a trip and create charge points along the route."""
    vehicles = client.get("/api/vehicles").json()
    route = client.post("/api/route", json={
        "vehicle_id": vehicles[0]["id"],
        "start": {"lat": 53.5511, "lon": 9.9937, "text": "Hamburg"},
        "destination": {"lat": 48.1351, "lon": 11.5820, "text": "München"},
        # The demo adapter does not know three different presets - so the
        # first (only) variant is enough here.
        "start_soc": 80.0}).json()["variants"][0]

    geo = route["geometry"]
    db = SessionLocal()
    try:
        db.query(models.ChargePoint).delete()
        km, upcoming, i = 0.0, 30.0, 0
        for n in range(1, len(geo)):
            km += haversine_m(geo[n - 1][1], geo[n - 1][0],
                              geo[n][1], geo[n][0]) / 1000.0
            if km < upcoming:
                continue
            db.add(models.ChargePoint(
                source="ocm", foreign_id=f"pruef-{i}", name=f"Lader km {km:.0f}",
                operator="Prüfbetrieb", lat=geo[n][1] + 0.0036, lon=geo[n][0],
                city=f"Ort {i}", country="DE", connectors=[],
                max_kw=150.0 if i % 2 else 300.0, point_count=4 + (i % 5),
                connector_types="CCS"))
            i += 1
            upcoming = km + 30.0
        db.commit()
    finally:
        db.close()
    return route


def part_chain():
    client = TestClient(app)
    route = prepare_trip(client)
    trip_id = route["trip_id"]

    print("\nLive-Sitzung mit Startplan")
    start = client.post(f"/api/live/start/{trip_id}",
                        params={"min_kw": 100, "radius_km": 10}).json()
    session_id = start["session_id"]
    start_plan = start.get("plan")
    verify(start_plan is not None and start_plan.get("feasible"),
           "beim Start wird sofort ein Ladeplan gerechnet",
           (start_plan or {}).get("reason", "kein Plan"))
    verify(len(start_plan.get("stops") or []) >= 1,
           "und er enthält Ladestopps",
           f"{len(start_plan.get('stops') or [])}")

    print("\nMehrverbrauch führt zu einem neuen Plan")
    # Replayed by hand instead of via /simulieren: the built-in simulation
    # runs as a background task, and a check script that waits for one ends
    # up checking the waiting time instead of the thing itself.
    #
    # Up to km 100 and no further: the simulator does not recharge along the
    # way - it replays the energy profile. Letting it run on means checking a
    # car that ignored its own plan and ends up somewhere between two charge
    # points; its charging plan rightly no longer is one. This very case
    # gets its own check right below.
    _replay(client, session_id, trip_id, extra_consumption=1.3, time_factor=1.0,
               until_km=100.0)

    state = client.get(f"/api/live/{session_id}").json()
    plan = state.get("plan")
    verify(state["consumption_factor"] > 1.15,
           "der Mehrverbrauch wird als Faktor erkannt",
           f"×{state['consumption_factor']}")
    verify(plan is not None, "es liegt ein Plan vor")
    verify((plan or {}).get("reading_km", 0) > 0,
           "und er wurde unterwegs neu gerechnet, nicht beim Start",
           f"reading_km={(plan or {}).get('reading_km')}")
    _examine_plan(plan, route, "Umplanung")
    verify(plan.get("stops") and plan["stops"][0]["arrival_soc"]
           >= route["vehicle"]["reserve_soc"] - 0.5,
           "der neue erste Stopp wird noch über der Reserve erreicht",
           str([s["arrival_soc"] for s in plan.get("stops") or []]))
    verify(not replanning.stops_same(start_plan, plan),
           "und der Plan ist ein anderer als der beim Losfahren - genau "
           "dafür gibt es die Nachführung",
           f"vorher {[s['id'] for s in start_plan['stops']]}, "
           f"jetzt {[s['id'] for s in plan['stops']]}")

    print("\nWeitergefahren, bis nichts mehr geht")
    _replay(client, session_id, trip_id, extra_consumption=1.3, time_factor=1.0)
    empty = client.get(f"/api/live/{session_id}").json().get("plan") or {}
    verify(empty.get("feasible") is False,
           "mit leerem Akku gibt es keinen Plan mehr - und jolt behauptet "
           "auch keinen", str(empty.get("reason"))[:80])
    verify(bool(empty.get("reason")),
           "der Grund steht dabei", str(empty.get("reason"))[:80])

    client.post(f"/api/live/{session_id}/end")

    print("\nStau verschiebt die Ankunft, ohne den Verbrauch zu verbiegen")
    start2 = client.post(f"/api/live/start/{trip_id}",
                         params={"min_kw": 100, "radius_km": 10}).json()
    sitzung2 = start2["session_id"]
    last = _replay(client, sitzung2, trip_id, extra_consumption=1.0,
                         time_factor=1.5, until_km=200.0)
    verify(abs(last["consumption_factor"] - 1.0) < 0.08,
           "der Verbrauchsfaktor bleibt bei rund 1 - es wird ja nicht mehr "
           "verbraucht, nur langsamer gefahren",
           f"×{last['consumption_factor']}")
    verify(last["time_factor"] > 1.3,
           "der Zeitfaktor erkennt den Stau", f"×{last['time_factor']}")
    verify(last["arrival_shift_min"] is not None
           and last["arrival_shift_min"] > 10,
           "und die Ankunft verschiebt sich deutlich",
           f"{last.get('arrival_shift_min')} min")
    client.post(f"/api/live/{sitzung2}/end")

    print("\nEine Belegt-Meldung wirft den Stopp aus dem Plan")
    start3 = client.post(f"/api/live/start/{trip_id}",
                         params={"min_kw": 100, "radius_km": 10}).json()
    sitzung3 = start3["session_id"]
    _replay(client, sitzung3, trip_id, extra_consumption=1.0, time_factor=1.0,
               until_km=20.0)

    # What gets reported is the stop that applies **now** - not the one from
    # the start plan. By km 20 the plan has usually been replanned once, and
    # then a different stop is there. This used to be
    # `start3["plan"]["stopps"][0]`, and the case silently stopped checking
    # anything: the reported stop was not the next one at all, the trigger
    # rightly did not fire, and the check "the occupied stop is no longer in
    # the plan" passed for the wrong reason - it was missing because the plan
    # had long since been replanned.
    ongoing = client.get(f"/api/live/{sitzung3}").json().get("plan") or {}
    earlier = (ongoing.get("stops") or [None])[0]
    verify(earlier is not None,
           "vor der Meldung steht ein nächster Stopp im laufenden Plan",
           str(ongoing.get("stops")))
    client.post(f"/api/chargers/{earlier['id']}/occupied")
    after = _measure(client, sitzung3, trip_id, km=25.0, extra_consumption=1.0,
                      time_factor=1.0)
    plan3 = client.get(f"/api/live/{sitzung3}").json().get("plan") or {}
    ids = [s["id"] for s in plan3.get("stops") or []]
    verify(earlier["id"] not in ids,
           "der belegte Stopp steht nicht mehr im Plan",
           f"geplant war {earlier['id']}, jetzt {ids}")
    verify(after.get("plan_changed") is True,
           "und die Änderung wird als Änderung gemeldet",
           after.get("change", ""))
    client.delete(f"/api/chargers/{earlier['id']}/occupied")
    client.post(f"/api/live/{sitzung3}/end")

    print("\nDer Plan ändert sich nicht bei jeder Messung")
    start4 = client.post(f"/api/live/start/{trip_id}",
                         params={"min_kw": 100, "radius_km": 10}).json()
    sitzung4 = start4["session_id"]
    changes = 0
    measurements = 0
    for km in range(5, 300, 5):
        response = _measure(client, sitzung4, trip_id, km=float(km),
                          extra_consumption=1.3, time_factor=1.0)
        measurements += 1
        if response.get("plan_changed"):
            changes += 1
    verify(changes <= measurements / 3,
           "über 300 km wird nicht bei jeder Messung umgeplant",
           f"{changes} Änderungen bei {measurements} Messungen")
    verify(changes >= 1,
           "aber mindestens einmal - sonst wäre die Sperre eine Blockade",
           f"{changes} Änderungen")
    client.post(f"/api/live/{sitzung4}/end")


def _start_without_point(client, content):
    """Start a recording **without** the start point that the server creates.

    The server records the starting charge level as the first measurement
    point (`time = now`). The tests below, however, build their trip from
    backdated measurement points: the start point would then lie after all
    the others, with the start position at the end of the route - in reality
    it is the earliest. They also check something else (cleanup, odometer,
    learning); the start point itself is covered in check_backend.py.
    """
    response = client.post("/api/live/recording", json=content)
    if response.status_code == 200:
        db = SessionLocal()
        try:
            db.query(models.LivePoint).filter_by(
                session_id=response.json()["session_id"]).delete()
            db.commit()
        finally:
            db.close()
    return response


def _create_trip(client, vehicle, name, minutes_her, charges=False):
    """A recording with measurement points, the last of which is `minutes_her` old.

    Note when reading: `/api/live/recording` ends **all** other running
    sessions on start. Building two sessions side by side is therefore not
    possible - each case is checked individually.
    """
    response = _start_without_point(client, {
        "vehicle_id": vehicle["id"], "lat": 48.0, "lon": 11.0,
        "soc": 90.0, "name": name}).json()
    db = SessionLocal()
    try:
        session = db.get(models.LiveSession, response["session_id"])
        onset = datetime.utcnow() - timedelta(minutes=minutes_her + 40)
        for i in range(41):
            # While charging the car stands still and the charge level rises;
            # otherwise it drives and the charge level falls.
            soc = (65.0 + i * 0.6) if charges else (90.0 - i * 0.25)
            live_session.record_sample(
                db, session, 48.0 + (0.0 if charges else i * 0.009), 11.0,
                soc=soc, outside_temp_c=12.0, timestamp=onset + timedelta(minutes=i))
        return response["session_id"], cleanup.end_orphaned(db)
    finally:
        db.close()


def part_orphaned_trip():
    """jolt ends forgotten trips by itself - but not a charging pause.

    For a planned trip, forgetting is only half as bad: the measurement
    points are in the database. For a **recording** it is a total loss -
    route and energy profile are only built from the measurement points when
    it ends.

    The more expensive mistake is the opposite one, though: cutting off a
    trip that is merely charging. The second half would be irretrievably lost.
    """
    from app.live import cleanup as _a
    globals()["cleanup"] = _a

    client = TestClient(app)
    print("\nVergessene Fahrt selbst beenden")
    vehicle = client.get("/api/vehicles").json()[0]

    # 1. Silent for hours, last driving - this one gets ended.
    still_id, ended_at = _create_trip(client, vehicle, "Vergessen", 200)
    hit = next((b for b in ended_at if b["session_id"] == still_id), None)
    verify(hit is not None,
           "eine Fahrt, die seit Stunden schweigt, wird beendet",
           str([b["session_id"] for b in ended_at]))
    if hit:
        built = hit.get("recording") or {}
        verify(built.get("ok") is True,
               "und dabei entsteht die Strecke aus den Messpunkten - genau "
               "das, was beim Vergessen sonst verloren geht",
               str(built.get("reason")))
        verify(hit.get("learned") is not None,
               "auch gelernt wird - eine vergessene Fahrt ist keine "
               "schlechtere Messung als eine ordentlich beendete")
    verify(client.get(f"/api/live/{still_id}").json()["running"] is False,
           "die Sitzung ist danach beendet")

    # 2. Silent just as long, but charging was the last thing. A charging
    #    pause can take an hour, the phone lies locked in the car meanwhile -
    #    and then the trip continues.
    charge_id, ended_at = _create_trip(client, vehicle, "Ladepause", 200,
                                      charges=True)
    verify(charge_id not in [b["session_id"] for b in ended_at],
           "eine Fahrt, deren Ladestand zuletzt stieg, wird nach derselben "
           "Stille noch nicht beendet - sie lädt und fährt gleich weiter",
           str(ended_at))
    verify(client.get(f"/api/live/{charge_id}").json()["running"] is True,
           "sie läuft weiter")

    # 3. But even the charging pause is over at some point.
    long_id, ended_at = _create_trip(client, vehicle, "Lange Pause", 400,
                                       charges=True)
    verify(long_id in [b["session_id"] for b in ended_at],
           "nach sieben Stunden wird auch sie beendet - sonst bliebe sie "
           "ewig offen", str(ended_at))

    # 4. A session that has just reported stays untouched.
    fresh_id, ended_at = _create_trip(client, vehicle, "Läuft noch", 0)
    verify(fresh_id not in [b["session_id"] for b in ended_at],
           "eine, die gerade gemeldet hat, bleibt in Ruhe - Aufräumen darf "
           "keine laufende Fahrt abschneiden", str(ended_at))


def part_measured_capacity():
    """The capacity reported by the car beats the brochure figure.

    **Every** conversion between charge level and kilowatt hours depends on
    this number: the measured consumption, the correction factor learned
    from it, the charge swings in the plan, the remaining range. The profile
    holds what the manufacturer states for a new vehicle; what was measured
    on the vehicle was 73.8 instead of 77 kWh - four percent that would
    otherwise be wrong throughout, in the same direction.
    """
    from app.energy.model import VehicleValues

    client = TestClient(app)
    print("\nGemessene Akkukapazität")
    vehicle = client.get("/api/vehicles").json()[0]
    start = _start_without_point(client, {
        "vehicle_id": vehicle["id"], "lat": 48.0, "lon": 11.0,
        "soc": 90.0, "name": "Kapazität"}).json()

    db = SessionLocal()
    try:
        fz = db.get(models.Vehicle, vehicle["id"])
        profile_kwh = fz.battery_net_kwh
        verify(fz.measured_capacity_kwh is None,
               "vor der ersten Messung gilt der Profilwert",
               str(fz.measured_capacity_kwh))
        verify(fz.capacity_kwh == profile_kwh,
               "und `capacity_kwh` gibt genau ihn zurück")

        session = db.get(models.LiveSession, start["session_id"])
        measured = round(profile_kwh * 0.96, 1)
        live_session.record_sample(
            db, session, 48.0, 11.0, soc=90.0,
            raw_values={"soc_raw": 225, "battery_kwh": measured})
        db.refresh(fz)
        verify(fz.measured_capacity_kwh == measured,
               "ein gemeldeter Wert wird am Fahrzeug festgehalten",
               str(fz.measured_capacity_kwh))
        verify(fz.capacity_measured_at is not None,
               "mit Zeitpunkt - ohne den weiss niemand, ob er von gestern "
               "oder von vorletztem Jahr stammt")
        verify(fz.capacity_kwh == measured,
               "und ab jetzt wird mit ihm gerechnet")
        verify(VehicleValues.from_model(fz).battery_net_kwh == measured,
               "auch im Verbrauchsmodell - damit zieht die ganze Kette mit")

        # Nonsense must not get through.
        live_session.record_sample(
            db, session, 48.01, 11.0, soc=89.5,
            raw_values={"soc_raw": 224, "battery_kwh": profile_kwh * 3})
        db.refresh(fz)
        verify(fz.measured_capacity_kwh == measured,
               "ein Wert weit über dem Profil wird verworfen - das ist keine "
               "Alterung, sondern ein Lesefehler",
               str(fz.measured_capacity_kwh))
        live_session.record_sample(
            db, session, 48.02, 11.0, soc=89.0,
            raw_values={"soc_raw": 223, "battery_kwh": profile_kwh * 0.2})
        db.refresh(fz)
        verify(fz.measured_capacity_kwh == measured,
               "und einer weit darunter ebenso")
    finally:
        db.close()
    client.post(f"/api/live/{start['session_id']}/end")


def part_odometer_distance():
    """The car's odometer determines the distance, not the GPS.

    The distance of a recording is built from the measurement points. If
    they only arrive every thirty seconds, there are four hundred meters
    between them at country-road speed - and the straight line cuts off every
    curve. With a dead-zone gap, a whole stretch is missing. Both make the
    distance too short, and because consumption is calculated in kWh **per
    hundred kilometers**, the error goes straight into the vehicle's
    correction factor.

    The counter in the car knows neither curves nor dead zones.
    """
    import math

    from app.live import recording as uphill

    client = TestClient(app)
    print("\nStrecke aus dem Kilometerstand")
    vehicle = client.get("/api/vehicles").json()[0]

    def trip(name, with_counter):
        start = _start_without_point(client, {
            "vehicle_id": vehicle["id"], "lat": 48.0, "lon": 11.0,
            "soc": 90.0, "name": name}).json()
        db = SessionLocal()
        try:
            session = db.get(models.LiveSession, start["session_id"])
            onset = datetime.utcnow() - timedelta(minutes=60)
            # A winding road, sampled coarsely: the measurement points are so
            # far apart that the straight line cuts off the curves - just like
            # with a thirty-second reporting interval.
            for i in range(60):
                lat = 48.0 + i * 0.004
                lon = 11.0 + 0.02 * math.sin(i * 1.1)
                raw = {"soc_raw": 200}
                if with_counter:
                    # The counter runs with the *actual* distance. The straight
                    # lines between the measurement points add up to about
                    # 1.1 km per step; 1.55 were driven - the curves in
                    # between, which no measurement point saw.
                    raw["odometer_km"] = 59500 + round(i * 1.55)
                live_session.record_sample(
                    db, session, lat, lon, soc=90.0 - i * 0.4,
                    outside_temp_c=12.0, timestamp=onset + timedelta(minutes=i),
                    raw_values=raw)
        finally:
            db.close()
        return client.post(f"/api/live/{start['session_id']}/end").json()

    without = (trip("Nur GPS", False).get("recording") or {})
    using = (trip("Mit Zähler", True).get("recording") or {})

    verify(without.get("ok") and using.get("ok"),
           "beide Aufzeichnungen lassen sich abschliessen",
           f"{without.get('reason')} / {using.get('reason')}")
    verify(without.get("distance_source") == "gps",
           "ohne Kilometerstand bleibt es bei der GPS-Spur",
           str(without.get("distance_source")))
    verify(using.get("distance_source") == "kilometerstand",
           "mit Kilometerstand wird der genommen",
           f"{using.get('distance_source')} - {using.get('odometer')}")
    verify((using.get("distance_km") or 0) > (without.get("distance_km") or 0) * 1.2,
           "und die Strecke wird spürbar länger - die abgeschnittenen Kurven "
           "kommen zurück",
           f"{without.get('distance_km')} km → {using.get('distance_km')} km")

    # The counter must also match the odometer reading.
    odo = (using.get("odometer") or {}).get("odometer_km")
    verify(odo and abs((using.get("distance_km") or 0) - odo) < 1.5,
           "die gebaute Strecke trifft den Zählerstand",
           f"{using.get('distance_km')} km gegen {odo} km laut Zähler")

    # Nonsensical values must not get through.
    factor, reason = uphill.odometer_factor(
        [SimpleNamespace(raw_values={"odometer_km": 1000}),
         SimpleNamespace(raw_values={"odometer_km": 1900})], gps_km=10.0)
    verify(factor == 1.0,
           "ein Zählerstand, der die Strecke verneunfachen würde, wird "
           "verworfen statt geglaubt", str(reason))
    factor, _ = uphill.odometer_factor(
        [SimpleNamespace(raw_values={"odometer_km": 1000}),
         SimpleNamespace(raw_values={"odometer_km": 1002})], gps_km=2.5)
    verify(factor == 1.0,
           "und unter fünf Kilometern gar nicht erst benutzt - bei einem "
           "Kilometer Auflösung wäre das geraten")


def part_charge_plan_one_path():
    """`/charge-plan` and replanning compute via the same path.

    Both used to build the charging plan completely on their own: the same
    translation of corridor candidates into charging options (byte for byte
    the same eleven lines) and the same call of the optimizer with thirteen
    arguments. Maintaining it twice means that sooner or later something is
    forgotten - that is exactly what happened with the most recently added
    `km_offset`.

    What gets checked is the property that matters: planning from km 0 with
    the starting charge level is the same operation as replanning with no
    distance covered, so the same result has to come out.
    """
    from app.live import replanning as _u

    client = TestClient(app)
    route = prepare_trip(client)
    trip_id = route["trip_id"]
    print("\nLadeplan und Umplanung: ein Weg")

    over_router = client.post(f"/api/trips/{trip_id}/charge-plan",
                               params={"min_kw": 100, "radius_km": 10}).json()
    db = SessionLocal()
    try:
        trip = db.get(models.Trip, trip_id)
        over_replanning = _u.schedule(db, trip, 0.0, trip.start_soc, {
            "radius_km": 10.0, "min_kw": 100.0, "connector_type": "",
            "detour_limit_min": _u.DEFAULTS["detour_limit_min"],
            "stop_fixed_cost_min": _u.DEFAULTS["stop_fixed_cost_min"],
            "charge_park_bonus_min": _u.DEFAULTS["charge_park_bonus_min"],
            "time_value_eur_h": _u.DEFAULTS["time_value_eur_h"]})
    finally:
        db.close()

    verify(over_router.get("feasible") is True,
           "der Endpunkt liefert weiterhin einen machbaren Plan",
           over_router.get("reason", ""))
    verify(bool(over_router.get("stops")),
           "mit Ladestopps", str(len(over_router.get("stops") or [])))

    for field in ("feasible", "stop_count", "total_minutes",
                 "charge_time_minutes", "cost_eur", "soc_at_target"):
        verify(over_router.get(field) == over_replanning.get(field),
               f"'{field}' stimmt zwischen beiden Wegen überein",
               f"Router {over_router.get(field)} / "
               f"Umplanung {over_replanning.get(field)}")

    a = [(s["id"], s["km_on_route"], s["departure_soc"])
         for s in over_router.get("stops") or []]
    b = [(s["id"], s["km_on_route"], s["departure_soc"])
         for s in over_replanning.get("stops") or []]
    verify(a == b, "und die Stopps selbst sind dieselben - Standort, "
           "Kilometer und Abfahrts-Ladestand", f"{a[:2]} vs {b[:2]}")

    # The fields that the UI reads must still be there.
    missing = [f for f in ("feasible", "reason", "stop_count", "stops",
                         "total_minutes", "charge_time_minutes",
                         "detour_time_minutes", "holding_cost_minutes",
                         "cost_eur", "soc_at_target", "trip_id", "demo",
                         "connector_type", "min_kw", "radius_km")
             if f not in over_router]
    verify(not missing,
           "und die Antwort trägt weiter alles, was die Oberfläche liest",
           str(missing))


def part_charging_distorted_not():
    """A charging stop must spoil neither the learning nor the deviation.

    Both quantities compared the charge level against an energy profile that
    **knows no charging stops**: it counts down uninterrupted from the start
    and goes deep into the negative on a long trip. Anyone who charges along
    the way ends up far above this profile - and that affected both in
    different directions:

    * The calibration took `first.soc - last.soc`. Percentage points added by
      charging were missing from this difference, the learned factor came out
      too low, and because it stayed within the plausibility limits, nobody
      noticed. On every trip with a charging stop the vehicle learned that it
      is more economical than it is.
    * The deviation compared directly against the profile and reported
      three-digit percentage points after the first charging stop.
    """
    from app.energy import calibration

    client = TestClient(app)
    print("\nEin Ladestopp verfälscht weder Lernen noch Abweichung")
    vehicle = client.get("/api/vehicles").json()[0]
    start = _start_without_point(client, {
        "vehicle_id": vehicle["id"], "lat": 48.0, "lon": 11.0,
        "soc": 80.0, "name": "Mit Ladestopp"}).json()

    # First drive, then charge, then drive on - in such a way that both
    # driving sections have the same consumption per kilometer.
    db = SessionLocal()
    try:
        session = db.get(models.LiveSession, start["session_id"])
        onset = datetime.utcnow() - timedelta(minutes=150)
        soc, lat = 80.0, 48.0
        for i in range(51):
            if 25 <= i < 35:                 # charging pause, the car stands
                soc = min(80.0, soc + 2.5)
            else:
                lat += 0.030
                soc -= 1.0
            live_session.record_sample(
                db, session, lat, 11.0, soc=round(soc, 1), outside_temp_c=10.0,
                timestamp=onset + timedelta(minutes=i * 2))
    finally:
        db.close()

    end = client.post(f"/api/live/{start['session_id']}/end").json()
    learned = end.get("learned")

    # What the naive way would have delivered - computed from the same
    # points, so that the test case does not hang on pre-computed numbers.
    db = SessionLocal()
    try:
        session = db.get(models.LiveSession, start["session_id"])
        battery = session.trip.vehicle.battery_net_kwh
        points = [p for p in session.points if p.plan_soc is not None
                  and p.km_on_route is not None and p.soc is not None]
        first, last = points[0], points[-1]
        naiv_pp = first.soc - last.soc
        real_pp = sum(max(0.0, v.soc - n.soc)
                      for v, n in zip(points, points[1:]))
        naiv = calibration.factor_from_trip(
            ((first.plan_soc or 0.0) - (last.plan_soc or 0.0)) / 100.0 * battery,
            naiv_pp / 100.0 * battery,
            (last.km_on_route or 0.0) - (first.km_on_route or 0.0))
    finally:
        db.close()

    verify(real_pp > naiv_pp * 1.5,
           "der Aufbau prüft wirklich einen Ladestopp: tatsächlich verbraucht "
           f"wurden {real_pp:.0f} pp, die nackte Differenz von Anfang bis "
           f"Ende zeigt nur {naiv_pp:.0f}",
           f"{real_pp:.1f} vs {naiv_pp:.1f}")
    verify(learned is not None,
           "aus einer Fahrt mit Ladestopp wird überhaupt gelernt",
           str(end.get("not_learned")))
    if learned:
        verify(naiv is None or learned["raw_factor"] > naiv * 1.5,
               "und der gelernte Faktor rechnet den Ladestopp heraus - der "
               "naive Weg (Anfang minus Ende) läge deutlich darunter",
               f"gelernt={learned['raw_factor']} naiv={naiv}")

    # And the deviation during the trip.
    state = client.post(f"/api/live/{start['session_id']}/point",
                          json={"lat": 48.5, "lon": 11.0, "soc": 50.0})
    # The session has ended; a second one for the deviation.
    start2 = _start_without_point(client, {
        "vehicle_id": vehicle["id"], "lat": 48.0, "lon": 11.0,
        "soc": 80.0, "name": "Abweichung"}).json()
    tail = None
    for i in range(30):
        soc = 80.0 - i * 0.8 if i < 15 else 80.0 - (i - 15) * 0.8
        tail = client.post(f"/api/live/{start2['session_id']}/point", json={
            "lat": 48.0 + i * 0.012, "lon": 11.0, "soc": round(soc, 1)}).json()
    dev = tail.get("deviation_pp")
    verify(dev is None or abs(dev) < 40.0,
           "die Abweichung bleibt nach einem Ladesprung im lesbaren Bereich - "
           "vorher standen dort dreistellige Prozentpunkte", f"{dev} pp")


def part_gap_kilometers():
    """The reason given for a replan names kilometers of the whole trip.

    On the road the optimizer calculates on the **remaining distance**, which
    starts at zero. The stops are converted back afterwards, but the
    explanatory sentence was not - it named kilometers that do not exist on
    the route. In a trial run, at km 137 the message read "zwischen km 0 und
    km 44" (between km 0 and km 44).
    """
    from app.charging import optimizer

    print("\nBegründung nennt Kilometer der ganzen Fahrt")
    profile = optimizer.RouteProfile(
        km=[0.0, 100.0, 200.0], kwh=[0.0, 40.0, 80.0],
        mins=[0.0, 60.0, 120.0])

    class FZ:
        battery_net_kwh = 50.0
        reserve_soc = 10.0

    # A single charge point far ahead - not reachable from the front.
    options = [optimizer.ChargeOption(
        id=1, km_on_route=180.0, detour_minutes=1.0, max_kw=150.0,
        point_count=4, name="Weit weg", operator="X", city="", lat=0.0,
        lon=0.0)]
    plan = optimizer.schedule(profile, options, FZ(), [(0.0, 150.0), (100.0, 20.0)],
                             start_soc=30.0, km_offset=250.0)
    verify(not plan.feasible, "der Plan geht nicht auf", plan.reason)
    numbers = [int(t) for t in plan.reason.replace(".", " ").split()
              if t.isdigit()]
    verify(numbers and min(numbers) >= 250,
           "und die genannten Kilometer liegen hinter dem Versatz, nicht bei "
           "null - sonst sucht man am Steuer eine Stelle, die es nicht gibt",
           plan.reason)


def part_state_coordinate():
    """The state must say **where** it was measured.

    Without that, the UI computed the position back from the planned
    profile. A recording has none - it only comes into being on completion -
    and the back-calculation silently returned (0, 0): map in the Gulf of
    Guinea, driven track made of a single point, and the history curve, which
    measures its x axis along that track, a vertical line. Of all things in
    the operating mode where the curve is the only thing to see.
    """
    client = TestClient(app)
    print("\nZustand trägt die Koordinate")
    vehicle = client.get("/api/vehicles").json()[0]
    start = _start_without_point(client, {
        "vehicle_id": vehicle["id"], "lat": 48.0, "lon": 11.0,
        "soc": 90.0, "name": "Koordinate"}).json()

    state = client.post(f"/api/live/{start['session_id']}/point",
                          json={"lat": 48.21, "lon": 11.34, "soc": 88.0}).json()
    verify(abs((state.get("lat") or 0) - 48.21) < 1e-6
           and abs((state.get("lon") or 0) - 11.34) < 1e-6,
           "der gemeldete Punkt kommt mit seiner Koordinate zurück",
           f"lat={state.get('lat')} lon={state.get('lon')}")

    two = client.post(f"/api/live/{start['session_id']}/point",
                       json={"lat": 48.30, "lon": 11.40, "soc": 87.0}).json()
    verify(two.get("lat") != state.get("lat"),
           "und sie wandert mit - sonst bestünde die Spur aus einem Punkt",
           f"{state.get('lat')} → {two.get('lat')}")


def part_import_lengths():
    """A field that is too long must not cost the entire import.

    That is exactly what happened: for locations with several postal codes,
    OCM delivers a semicolon-separated list, and one of them aborted a run.
    The column was then widened - which only postpones the same error,
    because the next location has one more postal code.
    """
    from app.charging import chargers_import

    print("\nImport: zu lange Felder")
    db = SessionLocal()
    try:
        long = ";".join(f"{33000 + i * 100}" for i in range(12))
        verify(len(long) > 40, "der Testwert ist länger als die Spalte",
               f"{len(long)} Zeichen")
        variety = chargers_import._save(db, "test", "lang-1", {
            "name": "Auchan " + "x" * 400, "operator": "Test",
            "lat": 44.9, "lon": -0.6, "postcode": long, "city": "Bordeaux",
            "country": "FR", "connectors": [], "max_kw": 150.0,
            "point_count": 4, "connector_types": "CCS"})
        db.commit()
        verify(variety == "neu", "der Datensatz geht durch, statt den Lauf "
               "abzubrechen", variety)
        saved = (db.query(models.ChargePoint)
                       .filter_by(source="test", foreign_id="lang-1").one())
        verify(len(saved.postcode) <= 40,
               "die zu lange Postleitzahl ist gestutzt, nicht der Ladepunkt "
               "verworfen - eine halbe PLZ ist ein Schönheitsfehler, ein "
               "fehlender Ladepunkt auf der Route nicht",
               f"{len(saved.postcode)} Zeichen")
        verify(saved.max_kw == 150.0 and saved.point_count == 4,
               "und die Zahlen bleiben unangetastet")
    finally:
        db.close()


def part_altitude_source():
    """Smoothed GPS altitudes - and an honest statement of where they come from.

    The consumption model sums up the *positive* elevation differences. This
    rectification turns zero-mean noise into a systematic surcharge that
    accumulates linearly over the points - a drive across flat land becomes an
    Alpine stage, and it goes unchecked into the correction factor.
    """
    import random

    from app.geo import haversine_m as _haversine
    from app.live import recording as uphill

    print("\nHöhen: Quelle und Glättung")

    def gradient(elevations):
        return sum(max(0.0, b - a) for a, b in zip(elevations, elevations[1:]))

    # A drive straight ahead across flat land, about 800 points roughly 25 m
    # apart. The true climb is zero.
    distance = [[11.0 + i * 0.00032, 48.0] for i in range(800)]
    spacing = _haversine(48.0, distance[0][0], 48.0, distance[1][0])
    verify(15.0 < spacing < 40.0,
           "die Testpunkte liegen etwa so dicht wie echte Messpunkte",
           f"{spacing:.0f} m")

    random.seed(7)
    noisy = [random.gauss(0.0, 10.0) for _ in distance]
    raw_gradient = gradient(noisy)
    verify(raw_gradient > 2000.0,
           "rohes GPS macht aus der Ebene mehrere Kilometer Steigung - "
           "genau der Grund für die Glättung", f"{raw_gradient:.0f} m")

    smoothed = uphill.gps_elevations_smooth(distance, noisy)
    smooth_gradient = gradient(smoothed)
    verify(smooth_gradient < raw_gradient / 5.0,
           "geglättet bleibt davon weniger als ein Fünftel",
           f"{raw_gradient:.0f} m → {smooth_gradient:.0f} m")

    # But a real hill has to remain - smoothing that also removes the terrain
    # would just be a roundabout way of calculating flat.
    high = 300.0
    mountain = [high * (i / 400.0 if i < 400 else (800 - i) / 400.0)
            for i in range(800)]
    mountain_smooth = uphill.gps_elevations_smooth(distance, mountain)
    verify(gradient(mountain_smooth) > 0.9 * high,
           "ein echter Anstieg über 10 km übersteht die Glättung",
           f"{gradient(mountain_smooth):.0f} von {high:.0f} m")

    # And the statement must be correct. Without an ORS key the demo routing
    # deliberately returns no altitudes; then the source is "gps" - and not,
    # as before, always "karte" (map).
    _, source = uphill.complete_elevations(distance, noisy)
    verify(source == "gps",
           "fällt die Kartenabfrage aus, heisst die Quelle auch 'gps'", source)
    _, source = uphill.complete_elevations(distance, None)
    verify(source == "flach",
           "und ohne jede Höhe 'flach' - man muss einer Fahrt ansehen "
           "können, ob ihre Höhen etwas taugen", source)


def part_superseded_trip():
    """A new recording supersedes the old one - but does not swallow it.

    `/api/live/recording` ends running sessions of the same vehicle. That
    is right; but all it did was set `laeuft = False`. For a recording that
    was a total loss: route and energy profile are only built from the
    measurement points on completion, and with `laeuft = False` even the
    cleanup never sees it again.

    The case is not contrived - it is the most likely one of all. Whoever
    forgot to end the trip notices at the next departure, and precisely that
    action would then delete the trip it was meant to rescue.
    """
    client = TestClient(app)
    print("\nAbgelöste Aufzeichnung")
    vehicle = client.get("/api/vehicles").json()[0]

    first_item = _start_without_point(client, {
        "vehicle_id": vehicle["id"], "lat": 48.0, "lon": 11.0,
        "soc": 90.0, "name": "Vergessen"}).json()
    db = SessionLocal()
    try:
        session = db.get(models.LiveSession, first_item["session_id"])
        onset = datetime.utcnow() - timedelta(minutes=40)
        for i in range(41):
            live_session.record_sample(
                db, session, 48.0 + i * 0.009, 11.0, soc=90.0 - i * 0.25,
                outside_temp_c=12.0, timestamp=onset + timedelta(minutes=i))
    finally:
        db.close()

    # And now someone sets off without having ended the old trip.
    _start_without_point(client, {
        "vehicle_id": vehicle["id"], "lat": 49.0, "lon": 9.0,
        "soc": 80.0, "name": "Die neue"})

    db = SessionLocal()
    try:
        old = db.get(models.LiveSession, first_item["session_id"])
        verify(old.running is False, "die alte Sitzung weicht der neuen")
        trip = old.trip
        verify(bool(trip.geometry),
               "aber sie wird dabei abgeschlossen - die Strecke entsteht "
               "noch, statt mit der Sitzung zu verschwinden",
               f"geometrie={len(trip.geometry or [])} Punkte")
        verify(bool(trip.energy_profile) and (trip.distance_m or 0) > 1000,
               "mit Energieprofil und Strecke, also auswertbar",
               f"distance_m={trip.distance_m}")
    finally:
        db.close()


def part_attachments():
    """Bike rack and roof box - surcharge on the air drag.

    Two things must hold, and the second is the more important one.
    """
    print("\nAussen am Auto: Fahrradträger und Dachbox")

    class FzStub:
        curb_mass_kg = 2550.0
        payload_kg = 150.0
        mass_kg = 2700.0
        c_w = 0.29
        frontal_area_m2 = 2.90
        c_rr = 0.011
        eta_drive = 0.87
        eta_regen = 0.68
        p_aux_w = 450.0
        heat_pump = True
        battery_net_kwh = 77.0
        reserve_soc = 10.0
        correction_factor = 1.0

    class TripStub2:
        def __init__(self, factor, payload=None):
            self.vehicle = FzStub()
            self.payload_kg = payload
            self.air_drag_factor = factor

    without = VehicleValues.from_trip(TripStub2(1.0))
    using = VehicleValues.from_trip(TripStub2(1.25))
    verify(abs(without.c_w - 0.29) < 1e-9,
           "ohne Anbau bleibt der Beiwert unverändert", str(without.c_w))
    verify(abs(using.c_w - 0.29 * 1.25) < 1e-9,
           "ein Zuschlag von 25 % erhöht den Luftwiderstandsbeiwert",
           str(using.c_w))
    verify(abs(using.frontal_area_m2 - 2.90) < 1e-9,
           "die Stirnfläche bleibt, was sie ist - sie ist eine Abmessung des "
           "Autos und ändert sich nicht, wenn hinten Räder hängen")

    # And the effect must show up in consumption, with v².
    profile = [{"km": k, "lat": 48.0 + k * 0.009, "lon": 11.0, "elevation": 100.0,
               "speed_kmh": 120.0, "mins": k * 0.5, "soc": 80 - k * 0.1,
               "kwh": k * 0.2} for k in range(0, 201, 5)]
    environment = lambda lat, lon: Environment(temp_c=15.0)      # noqa: E731
    a = replanning.remaining_profile_physics(TripStub2(1.0), profile, 1.0, environment)
    b = replanning.remaining_profile_physics(TripStub2(1.25), profile, 1.0, environment)
    verify(b.kwh[-1] > a.kwh[-1] * 1.05,
           "mit Träger braucht dieselbe Strecke spürbar mehr Energie",
           f"{b.kwh[-1]:.1f} gegen {a.kwh[-1]:.1f} kWh")

    # The payload continues to act independently of that.
    heavy = VehicleValues.from_trip(TripStub2(1.0, payload=500.0))
    verify(abs(heavy.mass_kg - 3050.0) < 1e-9,
           "die Zuladung der Fahrt zählt unabhängig vom Anbau",
           str(heavy.mass_kg))


def part_recording():
    """A driven route without planning - and what comes out of it.

    The reverse of the planned trip: set off, record, and build the route
    afterwards from the measurement points. Intended for calibration, where a
    known short route is the cleanest measurement - and where planning a
    route beforehand would be cumbersome enough that one would skip it.
    """
    client = TestClient(app)
    print("\nFahrt aufzeichnen statt planen")

    vehicle = client.get("/api/vehicles").json()[0]
    response = _start_without_point(client, {
        "vehicle_id": vehicle["id"], "lat": 48.0, "lon": 11.0,
        "soc": 90.0, "name": "Runde um den Block"})
    verify(response.status_code == 200,
           "eine Aufzeichnung lässt sich ohne Route starten",
           f"HTTP {response.status_code}: {response.text[:120]}")
    start = response.json()
    session_id = start["session_id"]

    state = client.get(f"/api/live/{session_id}").json()
    verify(state["running"] is True,
           "und läuft, obwohl es weder Strecke noch Plan gibt")

    # Sixty kilometers north, for one hour, 12 percentage points of
    # consumption. At 0.009 degrees per point that is about one kilometer.
    db = SessionLocal()
    try:
        session = db.get(models.LiveSession, session_id)
        onset = datetime(2026, 1, 1, 8, 0)
        for i in range(61):
            live_session.record_sample(
                db, session, 48.0 + i * 0.009, 11.0,
                soc=90.0 - i * 0.2, outside_temp_c=7.0,
                timestamp=onset + timedelta(minutes=i),
                raw_values={"soc_raw": round((90.0 - i * 0.2) * 2.5),
                          "elevation_m": 500.0})
    finally:
        db.close()

    end = client.post(f"/api/live/{session_id}/end").json()
    built = end.get("recording") or {}
    verify(built.get("ok") is True,
           "beim Beenden entsteht aus den Messpunkten eine Strecke",
           str(built.get("reason")))
    verify(55 < (built.get("distance_km") or 0) < 65,
           "die Strecke stimmt mit dem überein, was gefahren wurde",
           f"{built.get('distance_km')} km statt rund 60")
    verify(abs((built.get("outside_temp_c") or 0) - 7.0) < 0.1,
           "die **gemessene** Aussentemperatur gilt, nicht eine Vorhersage",
           str(built.get("outside_temp_c")))

    trip = client.get(f"/api/trips/{start['trip_id']}").json()
    verify(len(trip.get("profile") or []) > 5,
           "die Fahrt hat hinterher ein Energieprofil",
           f"{len(trip.get('profile') or [])} Stützstellen")

    # The purpose of the whole operating mode: the correction factor must be
    # learnable from the recording. Without odometer and target value on the
    # measurement points, the calibration finds nothing - and both are only
    # fixed once the route has been built.
    verify(end.get("learned") is not None,
           "und jolt lernt daraus einen Korrekturfaktor - genau dafür ist "
           "die Aufzeichnung da", str(end.get("learned")))


def part_chain_with_charge_stop():
    """The whole chain, but this time charging actually happens along the way.

    The simulator never charges - its charge level falls monotonically to
    zero. That leaves the normal case of every real long trip unchecked:
    stop, charge, drive on. That is exactly where the bug lay that this part
    pins down.
    """
    client = TestClient(app)
    route = prepare_trip(client)
    trip_id = route["trip_id"]

    print("\nEin Ladestopp ist keine Verspätung")
    start = client.post(f"/api/live/start/{trip_id}",
                        params={"min_kw": 100, "radius_km": 10}).json()
    session_id = start["session_id"]

    CHARGE_DURATION_MIN = 30.0
    CHARGE_SWING_PP = 45.0

    db = SessionLocal()
    try:
        session = db.get(models.LiveSession, session_id)
        profile = session.trip.energy_profile or []
        total_km = profile[-1]["km"] if profile else 0.0
        onset = datetime(2026, 1, 1, 8, 0)

        def report(km, soc, mins):
            entry = _profile_at(profile, km)
            state = live_session.record_sample(
                db, session, entry["lat"], entry["lon"], round(soc, 2),
                timestamp=onset + timedelta(minutes=mins))
            return live_session.state_as_dict(state)

        # First section: exactly as planned, so that no other deviation dilutes
        # the statement. The charge level is the profile's, the clock is the
        # profile's.
        pause_km = min(150.0, total_km / 3)
        last = None
        km = 0.0
        while km <= pause_km:
            entry = _profile_at(profile, km)
            last = report(km, entry["soc"], entry.get("mins") or 0.0)
            km += 10.0

        prior_pause = last["arrival_shift_min"]
        verify(prior_pause is not None and abs(prior_pause) < 5.0,
               "vor der Pause liegt die Fahrt in der Zeit", f"{prior_pause} min")

        # The charging stop: thirty minutes at the same place, the charge level
        # rises by 45 percentage points.
        entry = _profile_at(profile, pause_km)
        soc_arrival = entry["soc"]
        clock = entry.get("mins") or 0.0
        for i in range(1, 4):
            last = report(pause_km, soc_arrival + CHARGE_SWING_PP * i / 3,
                             clock + CHARGE_DURATION_MIN * i / 3)

        past_pause = last["arrival_shift_min"]
        verify(past_pause is not None
               and abs(past_pause) < live_session.THRESHOLD_ARRIVAL_MIN,
               "und direkt nach dem Laden immer noch - die Ladezeit stand so "
               "im Plan und ist keine Verspätung",
               f"{past_pause} min (ohne Abzug wären es rund "
               f"{CHARGE_DURATION_MIN:.0f})")

        # Driving on. The charge level is now above the profile by the charge
        # swing, the clock behind it by the charging duration - the tracking
        # must be able to tell the two apart.
        km = pause_km + 10.0
        far = min(total_km, pause_km + 150.0)
        while km <= far:
            entry = _profile_at(profile, km)
            last = report(km, min(100.0, entry["soc"] + CHARGE_SWING_PP),
                             (entry.get("mins") or 0.0) + CHARGE_DURATION_MIN)
            km += 10.0

        later = last["arrival_shift_min"]
        verify(later is not None
               and abs(later) < live_session.THRESHOLD_ARRIVAL_MIN,
               "auch 150 km danach wird die Ladezeit nicht als Verspätung "
               "nachgetragen", f"{later} min")
        verify(abs(last["time_factor"] - 1.0) < 0.15,
               "und der Zeitfaktor bleibt bei rund 1 - gefahren wurde ja "
               "nach Plan", f"×{last['time_factor']}")
    finally:
        db.close()

    client.post(f"/api/live/{session_id}/end")


def part_position_without_charge_level():
    """Position all the time, charge level occasionally.

    The normal case as long as the car does not report its charge level
    itself: the phone delivers the position every second, the charge level
    is typed in at the charger. In between, jolt has to extrapolate it from
    the energy profile - and above all must not take its own estimate for a
    measurement. If it did, the consumption factor would always come out at
    1.0 and claim the forecast is right - the more convinced, the longer
    nobody has checked.
    """
    client = TestClient(app)
    route = prepare_trip(client)
    trip_id = route["trip_id"]

    print("\nPosition ohne Ladestand")
    start = client.post(f"/api/live/start/{trip_id}",
                        params={"min_kw": 100, "radius_km": 10}).json()
    session_id = start["session_id"]

    db = SessionLocal()
    try:
        session = db.get(models.LiveSession, session_id)
        profile = session.trip.energy_profile or []
        onset = datetime(2026, 1, 1, 8, 0)

        def report(km, soc=None):
            entry = _profile_at(profile, km)
            state = live_session.record_sample(
                db, session, entry["lat"], entry["lon"], soc,
                timestamp=onset + timedelta(minutes=entry.get("mins") or 0.0))
            return live_session.state_as_dict(state)

        # An anchor at departure, after that position only.
        report(0.0, soc=_profile_at(profile, 0.0)["soc"])
        last = None
        for km in range(10, 101, 10):
            last = report(float(km))

        verify(last["soc_reported"] is False,
               "ein Punkt ohne Ladestand wird als gerechnet gekennzeichnet")
        plan_100 = _profile_at(profile, 100.0)["soc"]
        verify(last["actual_soc"] is not None
               and abs(last["actual_soc"] - plan_100) < 1.0,
               "und der Ladestand wird aus dem Profil hochgerechnet",
               f"{last['actual_soc']} gegen Profil {plan_100}")
        verify(abs(last["consumption_factor"] - 1.0) < 1e-6,
               "die Schätzung selbst verändert den Verbrauchsfaktor nicht - "
               "sonst misst das Modell sich an sich selbst",
               f"×{last['consumption_factor']}")

        # This is the real gain: without these points there would be neither a
        # time factor nor an arrival forecast on the road.
        verify(last["arrival_shift_min"] is not None,
               "die Ankunftsprognose kommt allein aus Positionsmeldungen "
               "zustande", str(last["arrival_shift_min"]))

        # Now the anchor at the charger: eight percentage points less than expected.
        soc_start = _profile_at(profile, 0.0)["soc"]
        anchor = report(100.0, soc=plan_100 - 8.0)
        verify(anchor["soc_reported"] is True,
               "ein eingetippter Ladestand ist eine Meldung, keine Schätzung")

        # And on the right value. The number is the whole point here: the
        # eight percentage points accumulated over a hundred kilometers, not
        # over the last twenty. Anyone who takes the estimates in between for
        # measurements measures the deviation against the short base of the
        # sliding window and arrives at ×2.08 instead of ×1.23 - doubling the
        # measured excess consumption and planning the rest of the trip on
        # it. A bound like "greater than 1" would fall for that.
        expected = ((soc_start - (plan_100 - 8.0))
                    / (soc_start - plan_100))
        verify(abs(anchor["consumption_factor"] - expected) < 0.03,
               "und der Verbrauchsfaktor misst gegen den letzten *gemeldeten* "
               "Ladestand, nicht gegen die eigene Schätzung",
               f"×{anchor['consumption_factor']} statt ×{expected:.3f}")

        # And from there on the estimate continues with the new factor.
        onward = None
        for km in range(110, 161, 10):
            onward = report(float(km))
        plan_160 = _profile_at(profile, 160.0)["soc"]
        verify(onward["actual_soc"] < plan_160 - 8.0,
               "danach liegt die Schätzung unter dem Profil - der gemessene "
               "Mehrverbrauch wird fortgeschrieben, nicht vergessen",
               f"{onward['actual_soc']} gegen Profil {plan_160}")
    finally:
        db.close()

    client.post(f"/api/live/{session_id}/end")


def part_speed_in_the_chain():
    """Does the measured speed actually arrive in the running trip?

    The physics for that is in `recompute_part_speed`. This is only about
    the wiring: as long as nobody has reported a charge level, there is no
    consumption factor - and then the plan has to be based on the measured
    speed rather than on the slider value from before departure.
    """
    client = TestClient(app)
    route = prepare_trip(client)
    trip_id = route["trip_id"]

    print("\nGemessenes Tempo schlägt bis in den Plan durch")
    start = client.post(f"/api/live/start/{trip_id}",
                        params={"min_kw": 100, "radius_km": 10}).json()
    session_id = start["session_id"]
    verify((start.get("plan") or {}).get("basis") in (None, "planung"),
           "der Startplan beruht noch auf der Planung - gemessen ist da "
           "nichts", str((start.get("plan") or {}).get("basis")))

    db = SessionLocal()
    try:
        session = db.get(models.LiveSession, session_id)
        profile = session.trip.energy_profile or []
        onset = datetime(2026, 1, 1, 8, 0)
        # Eighteen percent faster than planned: the clock runs slower than the
        # profile anticipated. The charge level stays unknown - exactly the
        # case the measured speed is meant for.
        FASTER = 0.82
        for km in range(0, 141, 10):
            entry = _profile_at(profile, float(km))
            live_session.record_sample(
                db, session, entry["lat"], entry["lon"], None,
                timestamp=onset + timedelta(
                    minutes=(entry.get("mins") or 0.0) * FASTER))
        db.refresh(session)
        plan = session.plan or {}
    finally:
        db.close()

    verify(plan.get("basis") == "tempo gemessen",
           "unterwegs wird der Plan mit dem gemessenen Tempo neu gerechnet",
           str(plan.get("basis")))
    verify((plan.get("speed_factor") or 0) > 1.1,
           "und der Faktor entspricht dem, was tatsächlich gefahren wurde",
           f"×{plan.get('speed_factor')}")
    client.post(f"/api/live/{session_id}/end")



def _measure(client, session_id, trip_id, km, extra_consumption, time_factor):
    """Report a single measurement point at kilometer `km`."""
    trip = client.get(f"/api/trips/{trip_id}").json()
    profile = trip["profile"]
    entry = _profile_at(profile, km)
    consumed = trip["start_soc"] - (entry.get("soc") or trip["start_soc"])
    soc = max(0.0, trip["start_soc"] - consumed * extra_consumption)
    return client.post(f"/api/live/{session_id}/point", json={
        "lat": entry["lat"], "lon": entry["lon"], "soc": round(soc, 2)}).json()


def _replay(client, session_id, trip_id, extra_consumption, time_factor,
               until_km=None, step_km=10.0):
    """Replay the trip by hand - deterministic and without waiting.

    The built-in simulator runs asynchronously; for a check script a loop
    whose end is fixed is the better choice.
    """
    db = SessionLocal()
    try:
        session = db.get(models.LiveSession, session_id)
        trip = session.trip
        profile = trip.energy_profile or []
        total = profile[-1]["km"] if profile else 0.0
        end = min(total, until_km) if until_km else total
        onset = datetime(2026, 1, 1, 8, 0)

        last = None
        km = 0.0
        while km <= end:
            entry = _profile_at(profile, km)
            consumed = trip.start_soc - (entry.get("soc") or trip.start_soc)
            soc = max(0.0, trip.start_soc - consumed * extra_consumption)
            state = live_session.record_sample(
                db, session, entry["lat"], entry["lon"], round(soc, 2),
                timestamp=onset + timedelta(
                    minutes=(entry.get("mins") or 0.0) * time_factor))
            last = live_session.state_as_dict(state)
            if soc <= 0:
                break
            km += step_km
        return last
    finally:
        db.close()


def _examine_plan(plan, route, name):
    if not plan or not plan.get("feasible"):
        verify(False, f"{name}: es gibt einen gültigen Plan",
               (plan or {}).get("reason", "kein Plan"))
        return
    stops = plan.get("stops") or []
    km = [s["km_on_route"] for s in stops]
    verify(km == sorted(km), f"{name}: die Stopps stehen in Fahrtreihenfolge",
           str(km))
    verify(all(s["km_on_route"] >= plan["reading_km"] - 1 for s in stops),
           f"{name}: kein Stopp liegt hinter dem Fahrzeug",
           f"stand km {plan['reading_km']}, Stopps {km}")
    verify(all(s["km_on_route"] <= route["distance_km"] + 1 for s in stops),
           f"{name}: und keiner hinter dem Ziel", str(km))
    verify(all(s["departure_soc"] > s["arrival_soc"] for s in stops),
           f"{name}: an jedem Stopp wird tatsächlich geladen")


def main() -> int:
    part_trigger()
    part_charge_pauses()
    recompute_part_speed()
    part_remaining_distance()
    part_plan_comparison()
    part_chain()
    part_chain_with_charge_stop()
    part_attachments()
    part_orphaned_trip()
    part_superseded_trip()
    part_altitude_source()
    part_measured_capacity()
    part_odometer_distance()
    part_charge_plan_one_path()
    part_charging_distorted_not()
    part_gap_kilometers()
    part_state_coordinate()
    part_import_lengths()
    part_recording()
    part_position_without_charge_level()
    part_speed_in_the_chain()

    return verify.balance()


if __name__ == "__main__":
    sys.exit(main())
