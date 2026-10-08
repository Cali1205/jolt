"""Play through a planning and a trip - as close to the real thing as possible.

Not a check script but a **trial run**: it asserts nothing, it drives and
shows what comes out. The difference is the purpose. A check case secures
what is already known; this run is meant to find what nobody has thought of
yet - and for that it must be close enough to the real thing for the numbers
to speak: a real route via ORS, charging points along the route, the ID.Buzz
with its actual values, and measurement points that arrive the way the
dongle sends them - including charging pause, dead zone and values that
drop out individually.

It has paid off. Four errors turned up that none of the six check runs had
seen, because they all work with short trips without a charging stop: the
distorted learning factor, the three-digit deviation, the gap message with
kilometres of the remaining route and the destination "unterwegs".

    ORS_API_KEY=... python tools/probelauf.py

Without a key it also runs, but then against a straight line - and a
straight line has no tunnels, no exits and no elevation. The run says so
itself at the top.

**Throwaway database.** The real one stays untouched; the script creates
its own SQLite file.

The output is to be read from bottom to top: what is under "Befunde"
(findings) is what is wrong. Everything above it is evidence.
"""
import os
import sys
import tempfile

# Locally the package lives under backend/app, in the Docker image directly
# next to tools/ as app/ - the same pattern as in the import tools beside it.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (os.path.join(_HERE, "..", "backend"),
                  os.path.join(_HERE, "..")):
    if os.path.isdir(os.path.join(_candidate, "app")):
        sys.path.insert(0, _candidate)
        break
_db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
os.environ["DATABASE_URL"] = f"sqlite:///{_db.name}"
os.environ.pop("APP_PASSWORT", None)
# ORS_API_KEY deliberately stays: a straight line has no tunnels, no exits
# and no elevation - exactly what errors show up against.

from datetime import UTC, datetime, timedelta     # noqa: E402

from fastapi.testclient import TestClient          # noqa: E402

from app import models                             # noqa: E402
from app.database import SessionLocal              # noqa: E402
from app.geo import haversine_m                    # noqa: E402
from app.main import app                           # noqa: E402

FINDINGS = []


def finding(text, heavy="?"):
    FINDINGS.append((heavy, text))
    print(f"  [{heavy}] {text}")


def say(text):
    print(f"\n=== {text} ===")


client = TestClient(app)


def create_buzz():
    """The ID.Buzz with the corrected values."""
    response = client.post("/api/vehicles", json={
        "name": "ID.Buzz Pro (Sim)", "curb_mass_kg": 2400.0,
        "payload_kg": 150.0, "c_w": 0.29,
        "frontal_area_m2": 2.90, "c_rr": 0.010, "eta_drive": 0.88,
        "eta_regen": 0.70, "p_aux_w": 500.0, "heat_pump": True,
        "battery_gross_kwh": 86.0, "battery_net_kwh": 79.0,
        "reserve_soc": 10.0, "target_soc": 80.0,
        "max_charge_power_kw": 200.0, "connector_type": "CCS",
        "preferred_operators": ["Ionity"]})
    if response.status_code >= 400:
        finding(f"Fahrzeug anlegen scheitert: HTTP {response.status_code} "
               f"{response.text[:200]}", "FEHLER")
        sys.exit(1)
    return response.json()


def charge_points_scatter(geo, spacing_km=45.0):
    """Charging points along the real route, with different parks."""
    db = SessionLocal()
    try:
        km, upcoming, i = 0.0, spacing_km, 0
        for n in range(1, len(geo)):
            km += haversine_m(geo[n - 1][1], geo[n - 1][0],
                              geo[n][1], geo[n][0]) / 1000.0
            if km < upcoming:
                continue
            db.add(models.ChargePoint(
                source="ocm", foreign_id=f"sim-{i}",
                name=f"Park km {km:.0f}",
                operator=["Ionity", "EnBW", "Aral pulse", "Tesla"][i % 4],
                lat=geo[n][1] + 0.004, lon=geo[n][0],
                city=f"Ort {i}", country="DE", connectors=[],
                max_kw=[150.0, 300.0, 350.0, 250.0][i % 4],
                point_count=[2, 4, 8, 16][i % 4], connector_types="CCS"))
            i += 1
            upcoming = km + spacing_km
        db.commit()
        return i
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1. Planning
# ---------------------------------------------------------------------------

say("1. Planung: Hamburg → München mit dem ID.Buzz")
vehicle = create_buzz()

response = client.post("/api/route", json={
    "vehicle_id": vehicle["id"],
    "start": {"lat": 53.5511, "lon": 9.9937, "text": "Hamburg"},
    "destination": {"lat": 48.1351, "lon": 11.5820, "text": "München"},
    "start_soc": 90.0, "speed_factor": 1.2})
if response.status_code >= 400:
    finding(f"Route rechnen scheitert: HTTP {response.status_code} "
           f"{response.text[:300]}", "FEHLER")
    sys.exit(1)
records = response.json()
demo = records.get("demo") or records.get("is_demo")
variants = records["variants"]
route = variants[0]
print(f"  Varianten: {[v.get('labels') or v.get('variety') for v in variants]}")
print(f"  {route['distance_km']:.0f} km, {route['drive_time_minutes']:.0f} min, "
      f"{route['kwh_total']:.1f} kWh, Demo={bool(demo)}")
if demo:
    finding("Routing läuft im Demo-Modus - Luftlinie statt Strasse. Ohne "
           "ORS_API_KEY sagt der Probelauf wenig über die Wirklichkeit.",
           "HINWEIS")

count = charge_points_scatter(route["geometry"])
print(f"  {count} Ladepunkte entlang der Strecke angelegt")

trip_id = route["trip_id"]
plan_response = client.post(f"/api/trips/{trip_id}/charge-plan",
                           params={"min_kw": 100, "radius_km": 10})
if plan_response.status_code >= 400:
    finding(f"Ladeplan scheitert: HTTP {plan_response.status_code} "
           f"{plan_response.text[:300]}", "FEHLER")
    plan = {}
else:
    plan = plan_response.json()
    stops = plan.get("stops") or []
    print(f"  Plan: machbar={plan.get('feasible')}, {len(stops)} Stopps")
    for s in stops:
        print(f"    km {s['km_on_route']:>5} {s.get('operator',''):<12} "
              f"{s.get('arrival_soc')}% → {s.get('departure_soc')}%  "
              f"{s.get('charge_time_minutes')} min  "
              f"{s.get('point_count')} Punkte")
    if not stops:
        finding("Kein einziger Ladestopp auf 800 km mit 79 kWh - das kann "
               "nicht stimmen.", "FEHLER")
    for s in stops:
        if (s.get("charge_time_minutes") or 0) < 5:
            finding(f"Ladestopp von nur {s['charge_time_minutes']} min bei "
                   f"km {s['km_on_route']} - Halte unter 5 min sind der "
                   f"Fehler, der schon einmal da war.", "FEHLER")
        if (s.get("departure_soc") or 0) <= (s.get("arrival_soc") or 0):
            finding(f"Stopp bei km {s['km_on_route']} lädt nicht "
                   f"({s.get('arrival_soc')} → {s.get('departure_soc')} %)",
                   "FEHLER")


# ---------------------------------------------------------------------------
# 2. Drive the planned trip
# ---------------------------------------------------------------------------

say("2. Live-Fahrt: die geplante Strecke abfahren")
start = client.post(f"/api/live/start/{trip_id}",
                    params={"min_kw": 100, "radius_km": 10})
if start.status_code >= 400:
    finding(f"Live-Start scheitert: HTTP {start.status_code} "
           f"{start.text[:300]}", "FEHLER")
    sys.exit(1)
start = start.json()
session_id = start["session_id"]
start_plan = start.get("plan") or {}
print(f"  Sitzung {session_id}, Startplan: {len(start_plan.get('stops') or [])} "
      f"Stopps")

geo = route["geometry"]
profile = route["profile"]


def point_at_km(target_km):
    for p in profile:
        if p["km"] >= target_km:
            return p
    return profile[-1]


total_km = profile[-1]["km"]
states = []
extra_consumption = 1.18          # 18 % more than calculated - winter, loaded
last_km = 0.0
report_error_at = 0

# **Follow the plan, which includes charging.** Without it the state of
# charge falls unchecked to the stop, and what then comes out says something
# about the simulation and nothing about jolt. Between the stops the trip is
# driven with the excess consumption; at each stop the state of charge rises
# to the planned departure value.
stops = sorted(start_plan.get("stops") or [],
                key=lambda x: x["km_on_route"])
next_stop = 0
soc = 90.0
previous_plan = profile[0]["soc"]

for step in range(1, 41):
    target_km = total_km * step / 40.0
    p = point_at_km(target_km)
    # Consumption since the last measurement point, stretched by the excess consumption.
    soc -= (previous_plan - p["soc"]) * extra_consumption
    previous_plan = p["soc"]
    # Charging stops that lay on this stretch.
    while (next_stop < len(stops)
           and stops[next_stop]["km_on_route"] <= target_km):
        soc = stops[next_stop]["departure_soc"]
        next_stop += 1
    soc = max(3.0, min(100.0, soc))
    response = client.post(f"/api/live/{session_id}/point", json={
        "lat": p["lat"], "lon": p["lon"], "soc": round(soc, 1),
        "speed_kmh": 118.0, "outside_temp_c": 3.0,
        "raw_values": {"soc_raw": int(soc * 2.5), "voltage_v": 390.0,
                     "current_a": 45.0, "speed_kmh": 118.0,
                     "outside_temp_c": 3.0, "inside_temp_c": 21.0,
                     "aux_load_kw": 2.4, "odometer_km": 12000 + target_km}})
    if response.status_code >= 400:
        report_error_at += 1
        if report_error_at <= 2:
            finding(f"Messpunkt bei km {target_km:.0f} abgelehnt: HTTP "
                   f"{response.status_code} {response.text[:200]}", "FEHLER")
        continue
    z = response.json()
    states.append(z)
    last_km = target_km

if states:
    at_first, final = states[0], states[-1]
    print(f"  {len(states)} Messpunkte angekommen")
    print(f"  Verbrauchsfaktor: {at_first.get('consumption_factor')} → "
          f"{final.get('consumption_factor')}")
    print(f"  Abweichung am Ende: {final.get('deviation_pp')} pp")
    print(f"  Prognose am Ziel:   {final.get('forecast_soc_at_target')} %")
    print(f"  Rest:               {final.get('remaining_km')} km")
    replanned = [z for z in states if z.get("plan_changed")]
    print(f"  Umplanungen:        {len(replanned)}")
    for z in replanned[:4]:
        print(f"    km {z['km_on_route']:>6}: {z.get('change')}")

    if final.get("lat") in (None, 0) or final.get("lon") in (None, 0):
        finding("Der Zustand trägt keine brauchbare Koordinate", "FEHLER")
    print(f"  Ladestand am Ziel:  {states[-1].get('actual_soc')} % "
          f"(geladen wurde an {next_stop} Stopps)")
    if abs((final.get("consumption_factor") or 1.0) - extra_consumption) > 0.08:
        finding(f"Verbrauchsfaktor {final.get('consumption_factor')} statt "
               f"~{extra_consumption} - die Messung kommt nicht an", "FEHLER")
    if abs(final.get("deviation_pp") or 0.0) > 40.0:
        finding(f"Abweichung {final.get('deviation_pp')} pp - so viel kann "
               f"ein Ladestand gar nicht abweichen; das Profil kennt die "
               f"Ladestopps nicht", "FEHLER")
    if not replanned:
        finding("18 % Mehrverbrauch über 800 km und kein einziger neuer Plan",
               "FEHLER")
    latest_km = [z["km_on_route"] for z in states]
    if latest_km != sorted(latest_km):
        finding("Die Kilometerstände laufen nicht monoton", "FEHLER")


# ---------------------------------------------------------------------------
# 3. Recording, the way the dongle sends it
# ---------------------------------------------------------------------------

say("3. Aufzeichnung: wie obd.js sie fährt - mit Ladepause und Funkloch")
uphill = client.post("/api/live/recording", json={
    "vehicle_id": vehicle["id"], "lat": 48.10, "lon": 11.50,
    "soc": 82.0, "name": ""}).json()
on_id = uphill["session_id"]
print(f"  Sitzung {on_id}, Fahrt {uphill['trip_id']}")

db = SessionLocal()
try:
    trip = db.get(models.Trip, uphill["trip_id"])
    print(f"  Name: {trip.start_text!r} → {trip.target_text!r}")
    if not (trip.start_text or "").strip():
        finding("Die Aufzeichnung hat keinen Namen", "FEHLER")
finally:
    db.close()

# Driving 90 minutes, then charging 45 minutes, then on - at a 30-second
# interval that would be thousands of points; here every two minutes, which
# is enough for the behaviour.
lat, lon = 48.10, 11.50
soc = 82.0
# The vehicle odometer. It counts the **driven** distance, so
# 0.0135 degrees of latitude per step (about 1.5 km) plus a surcharge for the
# curves that lie between two measurement points and that no point sees.
odometer_km = 12800.0
CURVE_SURCHARGE = 1.25
now_ts = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=200)
points_ok, points_error = 0, 0
phase_log = []

for minute in range(0, 200, 2):
    timestamp = now_ts + timedelta(minutes=minute)
    if minute < 90:                    # driving
        lat += 0.0135
        odometer_km += 1.5 * CURVE_SURCHARGE
        soc -= 0.55
        phase = "fahrt"
        velocity = 115.0
    elif minute < 135:                 # charging, the car stands still
        soc = min(80.0, soc + 1.5)
        phase = "laden"
        velocity = 0.0
    elif minute < 150:                 # dead zone - nothing comes in
        # Driving continues regardless: the car moves, the counter runs, only the
        # message does not go out. Exactly the gap that the odometer closes
        # afterwards.
        lat += 0.0135
        odometer_km += 1.5 * CURVE_SURCHARGE
        soc -= 0.55
        continue
    else:                              # keep driving
        lat += 0.0135
        odometer_km += 1.5 * CURVE_SURCHARGE
        soc -= 0.55
        phase = "fahrt2"
        velocity = 115.0

    raw = {"soc_raw": int(soc * 2.5), "voltage_v": 392.0,
           "current_a": -120.0 if phase == "laden" else 48.0,
           "speed_kmh": velocity, "outside_temp_c": 4.0, "inside_temp_c": 21.0}
    if minute % 10 == 0:               # not every round answers everything
        raw["aux_load_kw"] = 2.1
        raw["odometer_km"] = round(odometer_km)
    else:
        raw["_fehlend"] = ["aux_load_kw", "odometer_km"]

    response = client.post(f"/api/live/{on_id}/point", json={
        "lat": round(lat, 6), "lon": round(lon, 6), "soc": round(soc, 1),
        "speed_kmh": velocity, "outside_temp_c": 4.0, "raw_values": raw})
    if response.status_code >= 400:
        points_error += 1
        if points_error <= 2:
            finding(f"Aufzeichnungspunkt ({phase}, Minute {minute}) abgelehnt: "
                   f"HTTP {response.status_code} {response.text[:200]}", "FEHLER")
    else:
        points_ok += 1
        phase_log.append((phase, response.json()))

    # The timestamp is set by the server; for the charging-pause detection it
    # must be right, so adjust it afterwards.
    db = SessionLocal()
    try:
        s = db.get(models.LiveSession, on_id)
        if s.points:
            s.points[-1].timestamp = timestamp
            db.commit()
    finally:
        db.close()

print(f"  {points_ok} Punkte angenommen, {points_error} abgelehnt")

# Does the clean-up recognise the charging pause? The last point is 200 min old.
from app.energy import charge_phases                 # noqa: E402
from app.live import cleanup                    # noqa: E402

db = SessionLocal()
try:
    s = db.get(models.LiveSession, on_id)
    charges = charge_phases.charges_at_end(s.points, cleanup.CHARGE_WINDOW_MINUTES,
                                     cleanup.CHARGE_SWING_PERCENT)
    print(f"  Aufräumen sieht Ladevorgang am Ende: {charges} "
          f"(letzter Punkt war 'fahrt2', also erwartet: False)")
    if charges:
        finding("Das Aufräumen hält eine beendete Fahrt für eine Ladepause - "
               "die Erkennung schaut zu weit zurück", "FEHLER")
finally:
    db.close()

end = client.post(f"/api/live/{on_id}/end")
if end.status_code >= 400:
    finding(f"Beenden scheitert: HTTP {end.status_code} {end.text[:300]}",
           "FEHLER")
else:
    e = end.json()
    built = e.get("recording") or {}
    print(f"  Abgeschlossen: {built}")
    print(f"  Gelernt: {e.get('learned')}  /  nicht: {e.get('not_learned')}")
    if not built.get("ok"):
        finding(f"Die Aufzeichnung liess sich nicht abschliessen: "
               f"{built.get('reason')}", "FEHLER")
    else:
        # Driven were about 2 x 45 minutes at 115 km/h ~ 170 km as the crow flies.
        if not (80.0 < built["distance_km"] < 400.0):
            finding(f"Rekonstruierte Strecke {built['distance_km']} km passt "
                   f"nicht zum Gefahrenen", "FEHLER")
        if built.get("elevations") == "flach":
            finding("Höhen 'flach' trotz gesetztem ORS-Schlüssel", "HINWEIS")
        if built.get("outside_temp_c") not in (4.0, 4):
            finding(f"Gemessene Aussentemperatur ging verloren: "
                   f"{built.get('outside_temp_c')} statt 4.0", "FEHLER")

    # The charging pause must not spoil the learned factor: 45 minutes of
    # standstill with a rising state of charge are not consumption.
    if e.get("learned"):
        raw_factor = e["learned"]["raw_factor"]
        print(f"  Rohfaktor der Fahrt: {raw_factor}")
        if not (0.5 < raw_factor < 2.0):
            finding(f"Gelernter Rohfaktor {raw_factor} ist unplausibel - die "
                   f"Ladepause wird vermutlich als Verbrauch gerechnet",
                   "FEHLER")

# Re-calculate the merged charge detection on the real measurement series.
# **After** closing: in a recording the measurement points only then carry an
# odometer reading - before that there is no distance on which they could be
# placed.
db = SessionLocal()
try:
    s = db.get(models.LiveSession, on_id)
    every = charge_phases.sections(s.points)
    charge = [a for a in every if a.charges]
    consumed_pp, driven_km = charge_phases.consumption(s.points)
    print(f"  Abschnitte: {len(every)}, davon ladend: {len(charge)} "
          f"({charge_phases.charged_pp(s.points):.1f} pp nachgeladen)")
    print(f"  Verbrauch ohne Ladeabschnitte: {consumed_pp:.1f} pp "
          f"über {driven_km:.1f} km")
    if not charge:
        finding("Die Ladepause wird in den Abschnitten nicht erkannt", "FEHLER")
    if consumed_pp <= 0:
        finding(f"Verbrauch über die Fahrabschnitte ist {consumed_pp:.1f} pp "
               f"- da stimmt das Vorzeichen nicht", "FEHLER")
finally:
    db.close()

# The trip in the history
db = SessionLocal()
try:
    trip = db.get(models.Trip, uphill["trip_id"])
    print(f"  Historie: {trip.start_text!r} → {trip.target_text!r}, "
          f"{(trip.distance_m or 0)/1000:.1f} km, "
          f"{trip.outside_temp_c} °C, Geometrie {len(trip.geometry or [])}")
    if (trip.target_text or "") == "unterwegs":
        finding("Das Ziel der Aufzeichnung heisst hinterher immer noch "
               "'unterwegs' - in der Fahrtenliste steht dann 'X → unterwegs'",
               "FEHLER")
finally:
    db.close()

lst = client.get("/api/trips").json()
print(f"  /api/trips liefert {len(lst)} Fahrten")
for f in lst:
    print(f"    {f.get('start')} → {f.get('destination')}  "
          f"{f.get('distance_km')} km  {f.get('consumption_kwh_100km')} kWh/100  "
          f"aufz={f.get('recording')}")
    if f.get("distance_km") in (None, 0):
        finding(f"Fahrt {f.get('id')} hat keine Strecke in der Liste",
               "HINWEIS")


# ---------------------------------------------------------------------------

say("Befunde")
if not FINDINGS:
    print("  Keine.")
for heavy, text in FINDINGS:
    print(f"  [{heavy}] {text}")
print(f"\n{sum(1 for s, _ in FINDINGS if s == 'FEHLER')} Fehler, "
      f"{sum(1 for s, _ in FINDINGS if s == 'HINWEIS')} Hinweise")
