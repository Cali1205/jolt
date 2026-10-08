#!/usr/bin/env python3
"""Prüft die Live-Umplanung - Stufe 3.

Zwei Teile, und der zweite ist der wichtige:

1. Die **Auslöser** aus Abschnitt 2.3 des Konzepts, einzeln und ohne
   Datenbank. Jeder muss über seiner Schwelle greifen und darunter schweigen.
   Ein Auslöser, der immer feuert, ist so nutzlos wie einer, der es nie tut.
2. Die **Umplanung** selbst, über die ganze Kette: Fahrt rechnen, Ladepunkte
   entlang der Strecke anlegen, Live-Sitzung starten, mit Mehrverbrauch und
   mit Stau abspielen - und nachsehen, ob der Plan sich ändert, ob er dabei
   gültig bleibt und ob er sich *nicht* bei jeder Messung ändert.

Ohne Netz, ohne Postgres, ohne API-Schlüssel:

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

verify = Check()


class VehicleStubB:
    reserve_soc = 10.0


def trigger(**deviations):
    """`_examine_replanning` mit lauter unauffälligen Vorgaben aufrufen.

    So steht in jedem Testfall nur die eine Grösse, um die es geht - und man
    sieht sofort, welche den Auslöser bewegt hat.
    """
    vals = dict(vehicle=VehicleStubB(), deviation=0.0, spacing_m=10.0,
                 detour_since=None, now_ts=datetime(2026, 1, 1, 12, 0),
                 forecast=50.0, reserve_at=None, total_km=600.0,
                 shift=0.0, upcoming=None, arrival_soc=None)
    vals.update(deviations)
    return live_session._examine_replanning(**vals)


# ---------------------------------------------------------------------------
# Teil 1: die Auslöser
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

    # Der nächste Stopp ist als belegt gemeldet.
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

    # Ankunfts-SoC am nächsten Stopp: die Schwelle sind 5 Prozentpunkte.
    required, reason, _ = trigger(upcoming=stop, arrival_soc=9.0)
    verify(required, "6 pp weniger am nächsten Stopp: neu planen", reason)
    required, reason, _ = trigger(upcoming=stop, arrival_soc=12.0)
    verify(not required, "3 pp weniger bleiben unter der Schwelle", reason)

    # Abweg braucht Dauer, nicht nur Abstand.
    now_ts = datetime(2026, 1, 1, 12, 0)
    required, reason, _ = trigger(spacing_m=900.0, detour_since=now_ts, now_ts=now_ts)
    verify(not required,
           "ein einzelner Ausreisser neben der Route löst nichts aus", reason)
    required, reason, urgent = trigger(
        spacing_m=900.0, detour_since=now_ts, now_ts=now_ts + timedelta(seconds=90))
    verify(required and urgent,
           "90 Sekunden neben der Route dagegen schon", reason)

    # Stau.
    required, reason, _ = trigger(shift=14.0)
    verify(required, "14 min spätere Ankunft: neu planen", reason)
    required, reason, _ = trigger(shift=6.0)
    verify(not required, "6 min bleiben unter der Schwelle", reason)

    # Ohne Plan bleibt die Abweichung hier die beste verfügbare Aussage.
    required, reason, _ = trigger(deviation=-7.0)
    verify(required, "ohne Plan zählt die Abweichung an der aktuellen Position",
           reason)


class PointStub:
    """Ein Messpunkt, so viel davon wie `_charge_pauses_minutes` anfasst."""

    NULL = datetime(2026, 1, 1, 8, 0)

    def __init__(self, km: float, soc: float, mins: float):
        self.km_on_route = km
        self.soc = soc
        self.timestamp = self.NULL + timedelta(minutes=mins)


def part_charge_pauses():
    """Was von der verstrichenen Zeit eine Ladepause war - und was nicht.

    Das Energieprofil führt ausschliesslich Fahrzeit; die Ladezeit steht im
    Plan. Wer die Wanduhr ungefiltert dagegen hält, hat nach dem ersten
    Ladestopp eine Verspätung in Höhe der Ladedauer - dauerhaft, denn
    aufgeholt wird sie nie.
    """
    print("\nLadepause von Verspätung unterscheiden")

    # 100 km/h, 0,1 Prozentpunkte je Kilometer.
    profile = [{"km": k, "mins": k * 0.6, "soc": 80 - k * 0.1}
              for k in range(0, 401, 10)]

    # Der Logger sendet während des Ladens weiter: dreissig Minuten am selben
    # Ort, der Ladestand steigt.
    charging = [PointStub(150.0, 30.0, 90.0), PointStub(150.0, 45.0, 100.0),
             PointStub(150.0, 60.0, 110.0), PointStub(150.0, 70.0, 120.0),
             PointStub(160.0, 68.0, 126.0)]
    measured = live_session._charge_pauses_minutes(charging, profile)
    verify(abs(measured - 30.0) < 0.1,
           "dreissig Minuten an der Säule werden als Ladepause erkannt",
           f"{measured:.1f} min")

    # Derselbe Ladestopp, aber der Logger hat geschlafen und meldet sich erst
    # zwanzig Kilometer später wieder. Auch dann darf nur die Standzeit
    # zählen, nicht die Fahrzeit für die zwanzig Kilometer.
    slept = [PointStub(150.0, 30.0, 90.0), PointStub(170.0, 65.0, 132.0)]
    measured = live_session._charge_pauses_minutes(slept, profile)
    verify(abs(measured - 30.0) < 0.1,
           "auch wenn der Logger die Pause verschlafen hat",
           f"{measured:.1f} min")

    # Rekuperation auf langer Talfahrt hebt den Ladestand ebenfalls - kostet
    # aber keine zusätzliche Zeit, also auch keine Gutschrift.
    downhill = [PointStub(150.0, 30.0, 90.0), PointStub(160.0, 31.0, 96.0)]
    measured = live_session._charge_pauses_minutes(downhill, profile)
    verify(measured < 0.5,
           "Rekuperation bergab ist keine Ladepause - das Auto fährt ja",
           f"{measured:.1f} min")

    # Mittagessen: eine Dreiviertelstunde Stillstand ohne Ladung. Die
    # verschiebt die Ankunft wirklich und muss stehen bleiben.
    pause = [PointStub(150.0, 30.0, 90.0), PointStub(150.0, 29.8, 135.0)]
    measured = live_session._charge_pauses_minutes(pause, profile)
    verify(measured < 0.5,
           "eine Pause ohne Ladung bleibt Verspätung - Mittagessen verschiebt "
           "die Ankunft wirklich", f"{measured:.1f} min")


# ---------------------------------------------------------------------------
# Teil 2: Rest-Strecke
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

    # Geometrie und Profil müssen denselben Nullpunkt bekommen, sonst ist
    # jede Etappenrechnung um diesen Versatz falsch.
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
    """Die Reststrecke mit dem gemessenen Tempo neu rechnen statt skalieren.

    Das Tempo wurde bisher **geraten**: Der Regler in der Planen-Ansicht
    steht auf 120 %, und niemand weiss, ob das stimmt. Gleichzeitig schrieb
    die PWA die gemessene Geschwindigkeit in eine Spalte, die nie jemand las.

    Warum dafür nicht ein Faktor genügt, ist der ganze Punkt: Der
    Luftwiderstand geht mit v², der Rollwiderstand nahezu linear, die
    Nebenverbraucher gar nicht mit dem Tempo, sondern mit der Zeit - und die
    sinkt, wenn man schneller fährt. Ein pauschaler Aufschlag trifft keinen
    dieser drei.
    """
    print("\nReststrecke mit gemessenem Tempo neu rechnen")

    # Zweihundert Kilometer eben, hundert km/h nach Plan, zehn Grad.
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

    # Die Schranke nach oben ist der reine v²-Anteil. Läge der Zuwachs
    # darüber, wäre mehr als der Luftwiderstand skaliert worden; läge er bei
    # null, wäre das Tempo gar nicht angekommen.
    gain = fast.kwh[-1] / past_plan.kwh[-1]
    verify(1.02 < gain < 1.44,
           "der Mehrverbrauch liegt zwischen spürbar und dem reinen "
           "v²-Faktor - Rollwiderstand und Nebenverbraucher skalieren nicht "
           "mit dem Quadrat", f"×{gain:.3f}")

    verify(slow.kwh[-1] < past_plan.kwh[-1]
           and slow.mins[-1] > past_plan.mins[-1],
           "langsamer fahren dreht beides um",
           f"{slow.kwh[-1]:.1f} kWh in {slow.mins[-1]:.0f} min")

    # Fahrten aus der Zeit vor diesen Profilfeldern müssen aufs Skalieren
    # zurückfallen und nicht abstürzen.
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
# Teil 3: die ganze Kette
# ---------------------------------------------------------------------------

def prepare_trip(client) -> dict:
    """Eine Fahrt rechnen und Ladepunkte entlang der Route anlegen."""
    vehicles = client.get("/api/fahrzeuge").json()
    route = client.post("/api/route", json={
        "vehicle_id": vehicles[0]["id"],
        "start": {"lat": 53.5511, "lon": 9.9937, "text": "Hamburg"},
        "destination": {"lat": 48.1351, "lon": 11.5820, "text": "München"},
        # Der Demo-Adapter kennt keine drei unterschiedlichen Vorgaben - hier
        # reicht deshalb die erste (einzige) Variante.
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
    # Abgespielt wird von Hand statt über /simulieren: Die eingebaute
    # Simulation läuft als Hintergrundaufgabe, und ein Prüfskript, das auf
    # eine solche wartet, prüft irgendwann die Wartezeit statt die Sache.
    #
    # Bis km 100 und nicht weiter: Der Simulator lädt unterwegs nicht nach -
    # er spielt das Energieprofil ab. Wer ihn weiter laufen lässt, prüft ein
    # Auto, das den eigenen Plan ignoriert hat und irgendwann zwischen zwei
    # Ladepunkten steht; dessen Ladeplan ist zu Recht keiner mehr. Genau
    # dieser Fall kommt gleich darunter eigens dran.
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

    client.post(f"/api/live/{session_id}/ende")

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
    client.post(f"/api/live/{sitzung2}/ende")

    print("\nEine Belegt-Meldung wirft den Stopp aus dem Plan")
    start3 = client.post(f"/api/live/start/{trip_id}",
                         params={"min_kw": 100, "radius_km": 10}).json()
    sitzung3 = start3["session_id"]
    _replay(client, sitzung3, trip_id, extra_consumption=1.0, time_factor=1.0,
               until_km=20.0)

    # Gemeldet wird der Stopp, der **jetzt** gilt - nicht der aus dem
    # Startplan. Bis km 20 ist meist schon einmal umgeplant, und dann steht
    # dort ein anderer. Vorher stand hier `start3["plan"]["stopps"][0]`, und
    # der Fall prüfte unbemerkt nichts mehr: Der gemeldete Stopp war gar
    # nicht der nächste, der Auslöser griff zu Recht nicht, und die Prüfung
    # "der belegte Stopp steht nicht mehr im Plan" bestand aus dem falschen
    # Grund - er fehlte, weil längst umgeplant war.
    ongoing = client.get(f"/api/live/{sitzung3}").json().get("plan") or {}
    earlier = (ongoing.get("stops") or [None])[0]
    verify(earlier is not None,
           "vor der Meldung steht ein nächster Stopp im laufenden Plan",
           str(ongoing.get("stops")))
    client.post(f"/api/saeulen/{earlier['id']}/belegt")
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
    client.delete(f"/api/saeulen/{earlier['id']}/belegt")
    client.post(f"/api/live/{sitzung3}/ende")

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
    client.post(f"/api/live/{sitzung4}/ende")


def _start_without_point(client, content):
    """Eine Aufzeichnung starten, **ohne** den Startpunkt, den der Server anlegt.

    Der Server nimmt den Startladestand als ersten Messpunkt auf (`Zeit =
    jetzt`). Die Tests unten bauen ihre Fahrt dagegen aus rueckdatierten
    Messpunkten: Der Startpunkt laege dann hinter allen anderen, mit der
    Startposition am Ende der Strecke - in der Wirklichkeit ist er der
    fruehste. Sie pruefen auch etwas anderes (Aufraeumen, Kilometerstand,
    Lernen); der Startpunkt selbst steht in check_backend.py.
    """
    response = client.post("/api/live/aufzeichnung", json=content)
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
    """Eine Aufzeichnung mit Messpunkten, deren letzter `minutes_her` alt ist.

    Achtung beim Lesen: `/api/live/aufzeichnung` beendet beim Start **alle**
    anderen laufenden Sitzungen. Zwei Sitzungen nebeneinander aufzubauen geht
    deshalb nicht - jeder Fall wird einzeln geprüft.
    """
    response = _start_without_point(client, {
        "vehicle_id": vehicle["id"], "lat": 48.0, "lon": 11.0,
        "soc": 90.0, "name": name}).json()
    db = SessionLocal()
    try:
        session = db.get(models.LiveSession, response["session_id"])
        onset = datetime.utcnow() - timedelta(minutes=minutes_her + 40)
        for i in range(41):
            # Beim Laden steht das Auto und der Ladestand steigt; sonst fährt
            # es und der Ladestand fällt.
            soc = (65.0 + i * 0.6) if charges else (90.0 - i * 0.25)
            live_session.record_sample(
                db, session, 48.0 + (0.0 if charges else i * 0.009), 11.0,
                soc=soc, outside_temp_c=12.0, timestamp=onset + timedelta(minutes=i))
        return response["session_id"], cleanup.end_orphaned(db)
    finally:
        db.close()


def part_orphaned_trip():
    """Vergessene Fahrten beendet jolt selbst - aber nicht die Ladepause.

    Für eine geplante Fahrt ist das Vergessen halb so schlimm: Die Messpunkte
    liegen in der Datenbank. Für eine **Aufzeichnung** ist es der
    Totalverlust - Strecke und Energieprofil entstehen erst beim Beenden aus
    den Messpunkten.

    Der teurere Fehler ist aber der umgekehrte: eine Fahrt abschneiden, die
    nur gerade lädt. Die zweite Hälfte wäre unwiederbringlich weg.
    """
    from app.live import cleanup as _a
    globals()["cleanup"] = _a

    client = TestClient(app)
    print("\nVergessene Fahrt selbst beenden")
    vehicle = client.get("/api/fahrzeuge").json()[0]

    # 1. Seit Stunden still, zuletzt gefahren - die wird beendet.
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

    # 2. Genauso lange still, aber zuletzt wurde geladen. Eine Ladepause kann
    #    eine Stunde dauern, das Telefon liegt derweil gesperrt im Auto - und
    #    danach geht die Fahrt weiter.
    charge_id, ended_at = _create_trip(client, vehicle, "Ladepause", 200,
                                      charges=True)
    verify(charge_id not in [b["session_id"] for b in ended_at],
           "eine Fahrt, deren Ladestand zuletzt stieg, wird nach derselben "
           "Stille noch nicht beendet - sie lädt und fährt gleich weiter",
           str(ended_at))
    verify(client.get(f"/api/live/{charge_id}").json()["running"] is True,
           "sie läuft weiter")

    # 3. Aber auch die Ladepause ist irgendwann vorbei.
    long_id, ended_at = _create_trip(client, vehicle, "Lange Pause", 400,
                                       charges=True)
    verify(long_id in [b["session_id"] for b in ended_at],
           "nach sieben Stunden wird auch sie beendet - sonst bliebe sie "
           "ewig offen", str(ended_at))

    # 4. Eine Sitzung, die gerade eben gemeldet hat, bleibt unangetastet.
    fresh_id, ended_at = _create_trip(client, vehicle, "Läuft noch", 0)
    verify(fresh_id not in [b["session_id"] for b in ended_at],
           "eine, die gerade gemeldet hat, bleibt in Ruhe - Aufräumen darf "
           "keine laufende Fahrt abschneiden", str(ended_at))


def part_measured_capacity():
    """Die vom Auto gemeldete Kapazitaet schlaegt die Prospektangabe.

    An dieser Zahl haengt **jede** Umrechnung zwischen Ladestand und
    Kilowattstunden: der gemessene Verbrauch, der daraus gelernte
    Korrekturfaktor, die Ladehuebe im Plan, die Restreichweite. Im Profil
    steht, was der Hersteller fuer ein neues Fahrzeug angibt; am Fahrzeug
    gemessen wurden 73,8 statt 77 kWh - vier Prozent, die sonst
    durchgaengig in dieselbe Richtung falsch liegen.
    """
    from app.energy.model import VehicleValues

    client = TestClient(app)
    print("\nGemessene Akkukapazität")
    vehicle = client.get("/api/fahrzeuge").json()[0]
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

        # Unsinn darf nicht durchschlagen.
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
    client.post(f"/api/live/{start['session_id']}/ende")


def part_odometer_distance():
    """Der Kilometerstand des Autos bestimmt die Strecke, nicht das GPS.

    Die Strecke einer Aufzeichnung entsteht aus den Messpunkten. Kommen die
    nur alle dreissig Sekunden, liegen bei Landstrassentempo vierhundert
    Meter dazwischen - und die Luftlinie schneidet jede Kurve ab. Bei einer
    Funkloch-Luecke fehlt gleich ein ganzes Stueck. Beides macht die Strecke
    zu kurz, und weil der Verbrauch in kWh **pro hundert Kilometer** gerechnet
    wird, wandert der Fehler direkt in den Korrekturfaktor des Fahrzeugs.

    Der Zaehler im Auto kennt weder Kurven noch Funkloecher.
    """
    import math

    from app.live import recording as uphill

    client = TestClient(app)
    print("\nStrecke aus dem Kilometerstand")
    vehicle = client.get("/api/fahrzeuge").json()[0]

    def trip(name, with_counter):
        start = _start_without_point(client, {
            "vehicle_id": vehicle["id"], "lat": 48.0, "lon": 11.0,
            "soc": 90.0, "name": name}).json()
        db = SessionLocal()
        try:
            session = db.get(models.LiveSession, start["session_id"])
            onset = datetime.utcnow() - timedelta(minutes=60)
            # Eine Serpentinenstrasse, grob abgetastet: Die Messpunkte liegen
            # so weit auseinander, dass die Luftlinie die Kurven abschneidet -
            # genau wie bei dreissig Sekunden Meldeabstand.
            for i in range(60):
                lat = 48.0 + i * 0.004
                lon = 11.0 + 0.02 * math.sin(i * 1.1)
                raw = {"soc_raw": 200}
                if with_counter:
                    # Der Zaehler laeuft mit der *wirklichen* Strecke. Die
                    # Luftlinien zwischen den Messpunkten ergeben rund
                    # 1,1 km je Schritt; gefahren wurden 1,55 - die Kurven
                    # dazwischen, die kein Messpunkt gesehen hat.
                    raw["odometer_km"] = 59500 + round(i * 1.55)
                live_session.record_sample(
                    db, session, lat, lon, soc=90.0 - i * 0.4,
                    outside_temp_c=12.0, timestamp=onset + timedelta(minutes=i),
                    raw_values=raw)
        finally:
            db.close()
        return client.post(f"/api/live/{start['session_id']}/ende").json()

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

    # Der Zaehler muss auch zum Zaehlerstand passen.
    odo = (using.get("odometer") or {}).get("odometer_km")
    verify(odo and abs((using.get("distance_km") or 0) - odo) < 1.5,
           "die gebaute Strecke trifft den Zählerstand",
           f"{using.get('distance_km')} km gegen {odo} km laut Zähler")

    # Unsinnige Werte duerfen nicht durchschlagen.
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
    """`/ladeplan` und die Umplanung rechnen ueber denselben Weg.

    Beide bauten den Ladeplan vorher vollstaendig fuer sich: dieselbe
    Uebersetzung von Korridor-Kandidaten in Ladeoptionen (byteweise dieselben
    elf Zeilen) und derselbe Aufruf des Optimierers mit dreizehn Argumenten.
    Zweimal gepflegt heisst frueher oder later einmal vergessen - beim zuletzt
    ergaenzten `km_offset` ist genau das passiert.

    Geprueft wird die Eigenschaft, auf die es ankommt: Eine Planung ab km 0
    mit dem Start-Ladestand ist derselbe Vorgang wie eine Umplanung ohne
    zurueckgelegte Strecke, also muss auch dasselbe herauskommen.
    """
    from app.live import replanning as _u

    client = TestClient(app)
    route = prepare_trip(client)
    trip_id = route["trip_id"]
    print("\nLadeplan und Umplanung: ein Weg")

    over_router = client.post(f"/api/fahrten/{trip_id}/ladeplan",
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

    # Die Felder, die die Oberflaeche liest, muessen weiter da sein.
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
    """Ein Ladestopp darf weder das Lernen noch die Abweichung verderben.

    Beide Groessen verglichen den Ladestand gegen ein Energieprofil, das
    **keine Ladestopps kennt**: Es rechnet vom Start an ununterbrochen
    herunter und geht auf einer Langstrecke tief ins Negative. Wer unterwegs
    laedt, liegt danach weit ueber diesem Profil - und das schlug in beide
    Richtungen durch:

    * Die Kalibrierung nahm `erster.soc - letzter.soc`. Nachgeladene
      Prozentpunkte fehlten in dieser Differenz, der gelernte Faktor fiel zu
      niedrig aus, und weil er in den Plausibilitaetsgrenzen blieb, fiel es
      nicht auf. Das Fahrzeug lernte bei jeder Fahrt mit Ladestopp, es sei
      sparsamer als es ist.
    * Die Abweichung verglich direkt gegen das Profil und meldete nach dem
      ersten Ladestopp dreistellige Prozentpunkte.
    """
    from app.energy import calibration

    client = TestClient(app)
    print("\nEin Ladestopp verfälscht weder Lernen noch Abweichung")
    vehicle = client.get("/api/fahrzeuge").json()[0]
    start = _start_without_point(client, {
        "vehicle_id": vehicle["id"], "lat": 48.0, "lon": 11.0,
        "soc": 80.0, "name": "Mit Ladestopp"}).json()

    # Erst fahren, dann laden, dann weiterfahren - und zwar so, dass beide
    # Fahrtstuecke denselben Verbrauch je Kilometer haben.
    db = SessionLocal()
    try:
        session = db.get(models.LiveSession, start["session_id"])
        onset = datetime.utcnow() - timedelta(minutes=150)
        soc, lat = 80.0, 48.0
        for i in range(51):
            if 25 <= i < 35:                 # Ladepause, das Auto steht
                soc = min(80.0, soc + 2.5)
            else:
                lat += 0.030
                soc -= 1.0
            live_session.record_sample(
                db, session, lat, 11.0, soc=round(soc, 1), outside_temp_c=10.0,
                timestamp=onset + timedelta(minutes=i * 2))
    finally:
        db.close()

    end = client.post(f"/api/live/{start['session_id']}/ende").json()
    learned = end.get("learned")

    # Was der naive Weg geliefert haette - aus denselben Punkten gerechnet,
    # damit der Prüffall sich nicht an ausgerechneten Zahlen festmacht.
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

    # Und die Abweichung waehrend der Fahrt.
    state = client.post(f"/api/live/{start['session_id']}/punkt",
                          json={"lat": 48.5, "lon": 11.0, "soc": 50.0})
    # Die Sitzung ist beendet; eine zweite fuer die Abweichung.
    start2 = _start_without_point(client, {
        "vehicle_id": vehicle["id"], "lat": 48.0, "lon": 11.0,
        "soc": 80.0, "name": "Abweichung"}).json()
    tail = None
    for i in range(30):
        soc = 80.0 - i * 0.8 if i < 15 else 80.0 - (i - 15) * 0.8
        tail = client.post(f"/api/live/{start2['session_id']}/punkt", json={
            "lat": 48.0 + i * 0.012, "lon": 11.0, "soc": round(soc, 1)}).json()
    dev = tail.get("deviation_pp")
    verify(dev is None or abs(dev) < 40.0,
           "die Abweichung bleibt nach einem Ladesprung im lesbaren Bereich - "
           "vorher standen dort dreistellige Prozentpunkte", f"{dev} pp")


def part_gap_kilometers():
    """Die Begründung einer Umplanung nennt Kilometer der ganzen Fahrt.

    Der Optimierer rechnet unterwegs auf der **Reststrecke**, die bei null
    beginnt. Die Stopps werden hinterher zurueckgerechnet, der Begruendungs-
    satz aber nicht - er nannte Kilometer, die es auf der Strecke nicht gibt.
    In einem Probelauf stand bei km 137 die Meldung "zwischen km 0 und km 44".
    """
    from app.charging import optimizer

    print("\nBegründung nennt Kilometer der ganzen Fahrt")
    profile = optimizer.RouteProfile(
        km=[0.0, 100.0, 200.0], kwh=[0.0, 40.0, 80.0],
        mins=[0.0, 60.0, 120.0])

    class FZ:
        battery_net_kwh = 50.0
        reserve_soc = 10.0

    # Ein einziger Ladepunkt weit hinten - von vorn nicht erreichbar.
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
    """Der Zustand muss sagen, **wo** gemessen wurde.

    Ohne das rechnete die Oberflaeche die Position aus dem geplanten Profil
    zurueck. Bei einer Aufzeichnung gibt es das nicht - es entsteht erst beim
    Abschliessen -, und die Rueckrechnung lieferte stumm (0, 0): Karte im
    Golf von Guinea, gefahrene Spur aus einem einzigen Punkt, und die
    Verlaufskurve, die ihre x-Achse entlang dieser Spur misst, ein
    senkrechter Strich. Ausgerechnet bei der Betriebsart, in der die Kurve
    das Einzige ist, was es zu sehen gibt.
    """
    client = TestClient(app)
    print("\nZustand trägt die Koordinate")
    vehicle = client.get("/api/fahrzeuge").json()[0]
    start = _start_without_point(client, {
        "vehicle_id": vehicle["id"], "lat": 48.0, "lon": 11.0,
        "soc": 90.0, "name": "Koordinate"}).json()

    state = client.post(f"/api/live/{start['session_id']}/punkt",
                          json={"lat": 48.21, "lon": 11.34, "soc": 88.0}).json()
    verify(abs((state.get("lat") or 0) - 48.21) < 1e-6
           and abs((state.get("lon") or 0) - 11.34) < 1e-6,
           "der gemeldete Punkt kommt mit seiner Koordinate zurück",
           f"lat={state.get('lat')} lon={state.get('lon')}")

    two = client.post(f"/api/live/{start['session_id']}/punkt",
                       json={"lat": 48.30, "lon": 11.40, "soc": 87.0}).json()
    verify(two.get("lat") != state.get("lat"),
           "und sie wandert mit - sonst bestünde die Spur aus einem Punkt",
           f"{state.get('lat')} → {two.get('lat')}")


def part_import_lengths():
    """Ein zu langes Feld darf nicht den ganzen Import kosten.

    Passiert ist genau das: OCM liefert fuer Standorte mit mehreren
    Postleitzahlen eine Semikolon-Liste, und eine davon hat einen Lauf
    abgebrochen. Die Spalte wurde daraufhin verbreitert - was denselben
    Fehler nur hinausschiebt, denn der naechste Standort hat eine
    Postleitzahl mehr.
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
    """Geglättete GPS-Höhen - und eine ehrliche Auskunft, woher sie stammen.

    Das Verbrauchsmodell summiert die *positiven* Höhenunterschiede auf.
    Diese Gleichrichtung macht aus mittelwertfreiem Rauschen einen
    systematischen Zuschlag, der sich linear über die Punkte aufaddiert -
    aus einer Fahrt durch die Ebene wird eine Alpenetappe, und die geht
    ungebremst in den Korrekturfaktor.
    """
    import random

    from app.geo import haversine_m as _haversine
    from app.live import recording as uphill

    print("\nHöhen: Quelle und Glättung")

    def gradient(elevations):
        return sum(max(0.0, b - a) for a, b in zip(elevations, elevations[1:]))

    # Eine Fahrt geradeaus durch die Ebene, rund 800 Punkte im Abstand von
    # etwa 25 m. Die wahre Steigung ist null.
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

    # Aber ein echter Hügel muss stehen bleiben - eine Glättung, die auch
    # das Gelände wegnimmt, wäre nur eine umständliche Art, flach zu rechnen.
    high = 300.0
    mountain = [high * (i / 400.0 if i < 400 else (800 - i) / 400.0)
            for i in range(800)]
    mountain_smooth = uphill.gps_elevations_smooth(distance, mountain)
    verify(gradient(mountain_smooth) > 0.9 * high,
           "ein echter Anstieg über 10 km übersteht die Glättung",
           f"{gradient(mountain_smooth):.0f} von {high:.0f} m")

    # Und die Auskunft muss stimmen. Ohne ORS-Schlüssel liefert das
    # Demo-Routing bewusst keine Höhen; dann ist die Quelle "gps" - und
    # nicht, wie vorher, immer "karte".
    _, source = uphill.complete_elevations(distance, noisy)
    verify(source == "gps",
           "fällt die Kartenabfrage aus, heisst die Quelle auch 'gps'", source)
    _, source = uphill.complete_elevations(distance, None)
    verify(source == "flach",
           "und ohne jede Höhe 'flach' - man muss einer Fahrt ansehen "
           "können, ob ihre Höhen etwas taugen", source)


def part_superseded_trip():
    """Eine neue Aufzeichnung loest die alte ab - aber verschluckt sie nicht.

    `/api/live/aufzeichnung` beendet laufende Sitzungen desselben Fahrzeugs.
    Das ist richtig; nur wurde dabei bloss `laeuft = False` gesetzt. Fuer eine
    Aufzeichnung war das der Totalverlust: Strecke und Energieprofil entstehen
    erst beim Abschliessen aus den Messpunkten, und mit `laeuft = False` sieht
    auch das Aufraeumen sie nie wieder.

    Der Fall ist nicht konstruiert - er ist der wahrscheinlichste ueberhaupt.
    Wer das Beenden vergessen hat, merkt es beim naechsten Losfahren, und
    genau dieser Griff loeschte dann die Fahrt, die er retten wollte.
    """
    client = TestClient(app)
    print("\nAbgelöste Aufzeichnung")
    vehicle = client.get("/api/fahrzeuge").json()[0]

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

    # Und jetzt faehrt jemand los, ohne die alte Fahrt beendet zu haben.
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
    """Fahrradträger und Dachbox - Zuschlag auf den Luftwiderstand.

    Zwei Dinge müssen gelten, und das zweite ist das wichtigere.
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

    # Und die Wirkung muss beim Verbrauch ankommen, mit v².
    profile = [{"km": k, "lat": 48.0 + k * 0.009, "lon": 11.0, "elevation": 100.0,
               "speed_kmh": 120.0, "mins": k * 0.5, "soc": 80 - k * 0.1,
               "kwh": k * 0.2} for k in range(0, 201, 5)]
    environment = lambda lat, lon: Environment(temp_c=15.0)      # noqa: E731
    a = replanning.remaining_profile_physics(TripStub2(1.0), profile, 1.0, environment)
    b = replanning.remaining_profile_physics(TripStub2(1.25), profile, 1.0, environment)
    verify(b.kwh[-1] > a.kwh[-1] * 1.05,
           "mit Träger braucht dieselbe Strecke spürbar mehr Energie",
           f"{b.kwh[-1]:.1f} gegen {a.kwh[-1]:.1f} kWh")

    # Die Zuladung wirkt weiter unabhängig davon.
    heavy = VehicleValues.from_trip(TripStub2(1.0, payload=500.0))
    verify(abs(heavy.mass_kg - 3050.0) < 1e-9,
           "die Zuladung der Fahrt zählt unabhängig vom Anbau",
           str(heavy.mass_kg))


def part_recording():
    """Eine gefahrene Strecke ohne Planung - und was daraus entsteht.

    Der umgekehrte Weg zur geplanten Fahrt: losfahren, mitschreiben, und die
    Strecke hinterher aus den Messpunkten bauen. Gedacht für die
    Kalibrierung, wo eine bekannte kurze Strecke die sauberste Messung ist -
    und wo eine Route vorher zu planen umständlich genug wäre, dass man es
    bleiben lässt.
    """
    client = TestClient(app)
    print("\nFahrt aufzeichnen statt planen")

    vehicle = client.get("/api/fahrzeuge").json()[0]
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

    # Sechzig Kilometer nach Norden, eine Stunde lang, 12 Prozentpunkte
    # Verbrauch. Bei 0,009 Grad je Punkt sind das rund einen Kilometer.
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

    end = client.post(f"/api/live/{session_id}/ende").json()
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

    trip = client.get(f"/api/fahrten/{start['trip_id']}").json()
    verify(len(trip.get("profile") or []) > 5,
           "die Fahrt hat hinterher ein Energieprofil",
           f"{len(trip.get('profile') or [])} Stützstellen")

    # Der Zweck der ganzen Betriebsart: Aus der Aufzeichnung muss sich der
    # Korrekturfaktor lernen lassen. Ohne Kilometerstand und Sollwert an den
    # Messpunkten findet die Kalibrierung nichts - und beides steht erst
    # fest, seit die Strecke gebaut wurde.
    verify(end.get("learned") is not None,
           "und jolt lernt daraus einen Korrekturfaktor - genau dafür ist "
           "die Aufzeichnung da", str(end.get("learned")))


def part_chain_with_charge_stop():
    """Die ganze Kette, aber diesmal wird unterwegs wirklich geladen.

    Der Simulator lädt nie - sein Ladestand fällt monoton bis null. Damit
    bleibt der Normalfall jeder echten Langstrecke ungeprüft: anhalten,
    laden, weiterfahren. Genau dort lag der Fehler, den dieser Teil festhält.
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

        # Erster Abschnitt: exakt nach Plan, damit keine andere Abweichung die
        # Aussage verwässert. Der Ladestand ist der des Profils, die Uhr die
        # des Profils.
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

        # Der Ladestopp: dreissig Minuten am selben Ort, der Ladestand steigt
        # um 45 Prozentpunkte.
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

        # Weiterfahren. Der Ladestand liegt jetzt um den Ladehub über dem
        # Profil, die Uhr um die Ladedauer dahinter - beides muss die
        # Nachführung auseinanderhalten können.
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

    client.post(f"/api/live/{session_id}/ende")


def part_position_without_charge_level():
    """Position dauernd, Ladestand gelegentlich.

    Der Normalfall, solange das Auto seinen Ladestand nicht selbst meldet:
    Das Telefon liefert die Position im Sekundentakt, der Ladestand wird an
    der Säule eingetippt. Dazwischen muss jolt ihn aus dem Energieprofil
    hochrechnen - und darf dabei vor allem eines nicht: die eigene Schätzung
    für eine Messung halten. Täte es das, käme der Verbrauchsfaktor immer auf
    1,0 heraus und behauptete, die Prognose stimme - umso überzeugter, je
    länger niemand nachgesehen hat.
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

        # Ein Anker beim Losfahren, danach nur noch Position.
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

        # Das ist der eigentliche Gewinn: Ohne diese Punkte gäbe es unterwegs
        # weder Zeitfaktor noch Ankunftsprognose.
        verify(last["arrival_shift_min"] is not None,
               "die Ankunftsprognose kommt allein aus Positionsmeldungen "
               "zustande", str(last["arrival_shift_min"]))

        # Jetzt der Anker an der Säule: acht Prozentpunkte weniger als gedacht.
        soc_start = _profile_at(profile, 0.0)["soc"]
        anchor = report(100.0, soc=plan_100 - 8.0)
        verify(anchor["soc_reported"] is True,
               "ein eingetippter Ladestand ist eine Meldung, keine Schätzung")

        # Und zwar auf den richtigen Wert. Die Zahl ist hier der ganze Punkt:
        # Die acht Prozentpunkte sind über hundert Kilometer entstanden, nicht
        # über die letzten zwanzig. Wer die Schätzungen dazwischen für
        # Messungen hält, misst die Abweichung gegen die kurze Basis des
        # gleitenden Fensters und kommt auf ×2,08 statt ×1,23 - er verdoppelt
        # den gemessenen Mehrverbrauch und plant den Rest der Fahrt danach.
        # Eine Schranke wie "grösser als 1" fiele darauf herein.
        expected = ((soc_start - (plan_100 - 8.0))
                    / (soc_start - plan_100))
        verify(abs(anchor["consumption_factor"] - expected) < 0.03,
               "und der Verbrauchsfaktor misst gegen den letzten *gemeldeten* "
               "Ladestand, nicht gegen die eigene Schätzung",
               f"×{anchor['consumption_factor']} statt ×{expected:.3f}")

        # Und ab da rechnet die Schätzung mit dem neuen Faktor weiter.
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

    client.post(f"/api/live/{session_id}/ende")


def part_speed_in_the_chain():
    """Kommt das gemessene Tempo in der laufenden Fahrt tatsächlich an?

    Die Physik dafür steht in `recompute_part_speed`. Hier geht es nur um
    die Verdrahtung: Solange niemand einen Ladestand gemeldet hat, gibt es
    keinen Verbrauchsfaktor - und dann muss der Plan auf dem gemessenen Tempo
    beruhen statt auf dem Reglerwert von vor der Abfahrt.
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
        # Achtzehn Prozent schneller als geplant: Die Uhr läuft langsamer als
        # das Profil vorsah. Der Ladestand bleibt unbekannt - genau der Fall,
        # für den das gemessene Tempo gedacht ist.
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
    client.post(f"/api/live/{session_id}/ende")



def _measure(client, session_id, trip_id, km, extra_consumption, time_factor):
    """Einen einzelnen Messpunkt bei Kilometer `km` melden."""
    trip = client.get(f"/api/fahrten/{trip_id}").json()
    profile = trip["profile"]
    entry = _profile_at(profile, km)
    consumed = trip["start_soc"] - (entry.get("soc") or trip["start_soc"])
    soc = max(0.0, trip["start_soc"] - consumed * extra_consumption)
    return client.post(f"/api/live/{session_id}/punkt", json={
        "lat": entry["lat"], "lon": entry["lon"], "soc": round(soc, 2)}).json()


def _replay(client, session_id, trip_id, extra_consumption, time_factor,
               until_km=None, step_km=10.0):
    """Die Fahrt von Hand abspielen - deterministisch und ohne Warten.

    Der eingebaute Simulator läuft asynchron; für ein Prüfskript ist eine
    Schleife, deren Ende feststeht, die bessere Wahl.
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
