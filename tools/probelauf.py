"""Eine Planung und eine Fahrt durchspielen - so nah am Ernstfall wie möglich.

Kein Prüfskript, sondern ein **Probelauf**: Er behauptet nichts, er fährt und
zeigt, was dabei herauskommt. Der Unterschied ist der Zweck. Ein Prüffall
sichert, was man schon weiss; dieser Lauf soll finden, woran noch niemand
gedacht hat - und dafür muss er nah genug am Ernstfall sein, dass die Zahlen
sprechen: echte Route über ORS, Ladepunkte entlang der Strecke, der ID.Buzz
mit seinen wirklichen Werten, und Messpunkte, die so hereinkommen wie der
Dongle sie schickt - samt Ladepause, Funkloch und Werten, die einzeln
ausfallen.

Er hat sich gelohnt. Vier Fehler kamen dabei heraus, die keiner der sechs
Prüfläufe gesehen hatte, weil sie alle mit kurzen Fahrten ohne Ladestopp
arbeiten: der verfälschte Lernfaktor, die dreistellige Abweichung, die
Lückenmeldung mit Kilometern der Reststrecke und das Ziel "unterwegs".

    ORS_API_KEY=... python tools/probelauf.py

Ohne Schlüssel läuft er auch, dann aber gegen eine Luftlinie - und eine
Luftlinie hat keine Tunnel, keine Ausfahrten und keine Höhen. Der Lauf sagt
das oben selbst.

**Wegwerf-Datenbank.** Die echte bleibt unangetastet; das Skript legt sich
eine eigene SQLite-Datei an.

Zu lesen ist die Ausgabe von unten nach oben: Was unter "Befunde" steht, ist
das, was nicht stimmt. Alles darüber ist Beleg.
"""
import os
import sys
import tempfile

# Lokal liegt das Paket unter backend/app, im Docker-Image direkt neben
# tools/ als app/ - dasselbe Muster wie in den Import-Werkzeugen daneben.
_HIER = os.path.dirname(os.path.abspath(__file__))
for _kandidat in (os.path.join(_HIER, "..", "backend"),
                  os.path.join(_HIER, "..")):
    if os.path.isdir(os.path.join(_kandidat, "app")):
        sys.path.insert(0, _kandidat)
        break
_db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
os.environ["DATABASE_URL"] = f"sqlite:///{_db.name}"
os.environ.pop("APP_PASSWORT", None)
# ORS_API_KEY bleibt bewusst stehen: Eine Luftlinie hat keine Tunnel, keine
# Ausfahrten und keine Höhen - genau das, woran sich Fehler zeigen.

from datetime import UTC, datetime, timedelta     # noqa: E402

from fastapi.testclient import TestClient          # noqa: E402

from app import models                             # noqa: E402
from app.database import SessionLocal              # noqa: E402
from app.geo import haversine_m                    # noqa: E402
from app.main import app                           # noqa: E402

BEFUNDE = []


def befund(text, schwere="?"):
    BEFUNDE.append((schwere, text))
    print(f"  [{schwere}] {text}")


def sagen(text):
    print(f"\n=== {text} ===")


client = TestClient(app)


def buzz_anlegen():
    """Der ID.Buzz mit den korrigierten Werten."""
    antwort = client.post("/api/fahrzeuge", json={
        "name": "ID.Buzz Pro (Sim)", "leermasse_kg": 2400.0,
        "zuladung_kg": 150.0, "c_w": 0.29,
        "stirnflaeche_m2": 2.90, "c_rr": 0.010, "eta_antrieb": 0.88,
        "eta_rekup": 0.70, "p_neben_w": 500.0, "waermepumpe": True,
        "akku_brutto_kwh": 86.0, "akku_netto_kwh": 79.0,
        "reserve_soc": 10.0, "ziel_soc": 80.0,
        "max_ladeleistung_kw": 200.0, "steckertyp": "CCS",
        "bevorzugte_betreiber": ["Ionity"]})
    if antwort.status_code >= 400:
        befund(f"Fahrzeug anlegen scheitert: HTTP {antwort.status_code} "
               f"{antwort.text[:200]}", "FEHLER")
        sys.exit(1)
    return antwort.json()


def ladepunkte_streuen(geo, abstand_km=45.0):
    """Ladepunkte entlang der echten Route, mit unterschiedlichen Parks."""
    db = SessionLocal()
    try:
        km, naechster, i = 0.0, abstand_km, 0
        for n in range(1, len(geo)):
            km += haversine_m(geo[n - 1][1], geo[n - 1][0],
                              geo[n][1], geo[n][0]) / 1000.0
            if km < naechster:
                continue
            db.add(models.Ladepunkt(
                quelle="ocm", fremd_id=f"sim-{i}",
                name=f"Park km {km:.0f}",
                betreiber=["Ionity", "EnBW", "Aral pulse", "Tesla"][i % 4],
                lat=geo[n][1] + 0.004, lon=geo[n][0],
                ort=f"Ort {i}", land="DE", anschluesse=[],
                max_kw=[150.0, 300.0, 350.0, 250.0][i % 4],
                anzahl_punkte=[2, 4, 8, 16][i % 4], steckertypen="CCS"))
            i += 1
            naechster = km + abstand_km
        db.commit()
        return i
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1. Planung
# ---------------------------------------------------------------------------

sagen("1. Planung: Hamburg → München mit dem ID.Buzz")
fahrzeug = buzz_anlegen()

antwort = client.post("/api/route", json={
    "fahrzeug_id": fahrzeug["id"],
    "start": {"lat": 53.5511, "lon": 9.9937, "text": "Hamburg"},
    "ziel": {"lat": 48.1351, "lon": 11.5820, "text": "München"},
    "start_soc": 90.0, "tempo_faktor": 1.2})
if antwort.status_code >= 400:
    befund(f"Route rechnen scheitert: HTTP {antwort.status_code} "
           f"{antwort.text[:300]}", "FEHLER")
    sys.exit(1)
daten = antwort.json()
demo = daten.get("demo") or daten.get("ist_demo")
varianten = daten["varianten"]
route = varianten[0]
print(f"  Varianten: {[v.get('etiketten') or v.get('art') for v in varianten]}")
print(f"  {route['strecke_km']:.0f} km, {route['fahrzeit_minuten']:.0f} min, "
      f"{route['kwh_gesamt']:.1f} kWh, Demo={bool(demo)}")
if demo:
    befund("Routing läuft im Demo-Modus - Luftlinie statt Strasse. Ohne "
           "ORS_API_KEY sagt der Probelauf wenig über die Wirklichkeit.",
           "HINWEIS")

anzahl = ladepunkte_streuen(route["geometrie"])
print(f"  {anzahl} Ladepunkte entlang der Strecke angelegt")

fahrt_id = route["fahrt_id"]
plan_antwort = client.post(f"/api/fahrten/{fahrt_id}/ladeplan",
                           params={"min_kw": 100, "radius_km": 10})
if plan_antwort.status_code >= 400:
    befund(f"Ladeplan scheitert: HTTP {plan_antwort.status_code} "
           f"{plan_antwort.text[:300]}", "FEHLER")
    plan = {}
else:
    plan = plan_antwort.json()
    stopps = plan.get("stopps") or []
    print(f"  Plan: machbar={plan.get('machbar')}, {len(stopps)} Stopps")
    for s in stopps:
        print(f"    km {s['km_auf_route']:>5} {s.get('betreiber',''):<12} "
              f"{s.get('ankunft_soc')}% → {s.get('abfahrt_soc')}%  "
              f"{s.get('ladezeit_minuten')} min  "
              f"{s.get('anzahl_punkte')} Punkte")
    if not stopps:
        befund("Kein einziger Ladestopp auf 800 km mit 79 kWh - das kann "
               "nicht stimmen.", "FEHLER")
    for s in stopps:
        if (s.get("ladezeit_minuten") or 0) < 5:
            befund(f"Ladestopp von nur {s['ladezeit_minuten']} min bei "
                   f"km {s['km_auf_route']} - Halte unter 5 min sind der "
                   f"Fehler, der schon einmal da war.", "FEHLER")
        if (s.get("abfahrt_soc") or 0) <= (s.get("ankunft_soc") or 0):
            befund(f"Stopp bei km {s['km_auf_route']} lädt nicht "
                   f"({s.get('ankunft_soc')} → {s.get('abfahrt_soc')} %)",
                   "FEHLER")


# ---------------------------------------------------------------------------
# 2. Die geplante Fahrt abfahren
# ---------------------------------------------------------------------------

sagen("2. Live-Fahrt: die geplante Strecke abfahren")
start = client.post(f"/api/live/start/{fahrt_id}",
                    params={"min_kw": 100, "radius_km": 10})
if start.status_code >= 400:
    befund(f"Live-Start scheitert: HTTP {start.status_code} "
           f"{start.text[:300]}", "FEHLER")
    sys.exit(1)
start = start.json()
sitzung_id = start["sitzung_id"]
startplan = start.get("plan") or {}
print(f"  Sitzung {sitzung_id}, Startplan: {len(startplan.get('stopps') or [])} "
      f"Stopps")

geo = route["geometrie"]
profil = route["profil"]


def punkt_bei_km(ziel_km):
    for p in profil:
        if p["km"] >= ziel_km:
            return p
    return profil[-1]


gesamt_km = profil[-1]["km"]
zustaende = []
mehrverbrauch = 1.18          # 18 % mehr als gerechnet - Winter, beladen
letzter_km = 0.0
fehler_beim_melden = 0

# **Dem Plan folgen, also auch laden.** Ohne das faellt der Ladestand
# ungebremst bis zum Anschlag, und was dann herauskommt, sagt etwas ueber die
# Simulation und nichts ueber jolt. Gefahren wird zwischen den Stopps mit dem
# Mehrverbrauch; an jedem Stopp steigt der Ladestand auf den geplanten
# Abfahrtswert.
stopps = sorted(startplan.get("stopps") or [],
                key=lambda x: x["km_auf_route"])
naechster_stopp = 0
soc = 90.0
voriges_soll = profil[0]["soc"]

for schritt in range(1, 41):
    ziel_km = gesamt_km * schritt / 40.0
    p = punkt_bei_km(ziel_km)
    # Verbrauch seit dem letzten Messpunkt, um den Mehrverbrauch gestreckt.
    soc -= (voriges_soll - p["soc"]) * mehrverbrauch
    voriges_soll = p["soc"]
    # Ladestopps, die auf diesem Stueck lagen.
    while (naechster_stopp < len(stopps)
           and stopps[naechster_stopp]["km_auf_route"] <= ziel_km):
        soc = stopps[naechster_stopp]["abfahrt_soc"]
        naechster_stopp += 1
    soc = max(3.0, min(100.0, soc))
    antwort = client.post(f"/api/live/{sitzung_id}/punkt", json={
        "lat": p["lat"], "lon": p["lon"], "soc": round(soc, 1),
        "tempo_kmh": 118.0, "aussentemp_c": 3.0,
        "rohwerte": {"soc_roh": int(soc * 2.5), "spannung_v": 390.0,
                     "strom_a": 45.0, "tempo_kmh": 118.0,
                     "aussentemp_c": 3.0, "innentemp_c": 21.0,
                     "nebenverbrauch_kw": 2.4, "km_stand": 12000 + ziel_km}})
    if antwort.status_code >= 400:
        fehler_beim_melden += 1
        if fehler_beim_melden <= 2:
            befund(f"Messpunkt bei km {ziel_km:.0f} abgelehnt: HTTP "
                   f"{antwort.status_code} {antwort.text[:200]}", "FEHLER")
        continue
    z = antwort.json()
    zustaende.append(z)
    letzter_km = ziel_km

if zustaende:
    erst, letzt = zustaende[0], zustaende[-1]
    print(f"  {len(zustaende)} Messpunkte angekommen")
    print(f"  Verbrauchsfaktor: {erst.get('verbrauchsfaktor')} → "
          f"{letzt.get('verbrauchsfaktor')}")
    print(f"  Abweichung am Ende: {letzt.get('abweichung_pp')} pp")
    print(f"  Prognose am Ziel:   {letzt.get('prognose_soc_am_ziel')} %")
    print(f"  Rest:               {letzt.get('rest_km')} km")
    umgeplant = [z for z in zustaende if z.get("plan_geaendert")]
    print(f"  Umplanungen:        {len(umgeplant)}")
    for z in umgeplant[:4]:
        print(f"    km {z['km_auf_route']:>6}: {z.get('aenderung')}")

    if letzt.get("lat") in (None, 0) or letzt.get("lon") in (None, 0):
        befund("Der Zustand trägt keine brauchbare Koordinate", "FEHLER")
    print(f"  Ladestand am Ziel:  {zustaende[-1].get('ist_soc')} % "
          f"(geladen wurde an {naechster_stopp} Stopps)")
    if abs((letzt.get("verbrauchsfaktor") or 1.0) - mehrverbrauch) > 0.08:
        befund(f"Verbrauchsfaktor {letzt.get('verbrauchsfaktor')} statt "
               f"~{mehrverbrauch} - die Messung kommt nicht an", "FEHLER")
    if abs(letzt.get("abweichung_pp") or 0.0) > 40.0:
        befund(f"Abweichung {letzt.get('abweichung_pp')} pp - so viel kann "
               f"ein Ladestand gar nicht abweichen; das Profil kennt die "
               f"Ladestopps nicht", "FEHLER")
    if not umgeplant:
        befund("18 % Mehrverbrauch über 800 km und kein einziger neuer Plan",
               "FEHLER")
    letzte_km = [z["km_auf_route"] for z in zustaende]
    if letzte_km != sorted(letzte_km):
        befund("Die Kilometerstände laufen nicht monoton", "FEHLER")


# ---------------------------------------------------------------------------
# 3. Aufzeichnung, so wie der Dongle sie schickt
# ---------------------------------------------------------------------------

sagen("3. Aufzeichnung: wie obd.js sie fährt - mit Ladepause und Funkloch")
auf = client.post("/api/live/aufzeichnung", json={
    "fahrzeug_id": fahrzeug["id"], "lat": 48.10, "lon": 11.50,
    "soc": 82.0, "name": ""}).json()
auf_id = auf["sitzung_id"]
print(f"  Sitzung {auf_id}, Fahrt {auf['fahrt_id']}")

db = SessionLocal()
try:
    fahrt = db.get(models.Fahrt, auf["fahrt_id"])
    print(f"  Name: {fahrt.start_text!r} → {fahrt.ziel_text!r}")
    if not (fahrt.start_text or "").strip():
        befund("Die Aufzeichnung hat keinen Namen", "FEHLER")
finally:
    db.close()

# 90 Minuten fahren, dann 45 Minuten laden, dann weiter - im 30-Sekunden-Takt
# wären das tausende Punkte; hier alle zwei Minuten, das reicht fürs Verhalten.
lat, lon = 48.10, 11.50
soc = 82.0
# Der Kilometerstand des Fahrzeugs. Er zaehlt die **gefahrene** Strecke, also
# 0,0135 Grad Breite je Schritt (rund 1,5 km) plus einen Zuschlag fuer die
# Kurven, die zwischen zwei Messpunkten liegen und die kein Punkt sieht.
km_stand = 12800.0
KURVENZUSCHLAG = 1.25
jetzt = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=200)
punkte_ok, punkte_fehler = 0, 0
phase_log = []

for minute in range(0, 200, 2):
    zeit = jetzt + timedelta(minutes=minute)
    if minute < 90:                    # fahren
        lat += 0.0135
        km_stand += 1.5 * KURVENZUSCHLAG
        soc -= 0.55
        phase = "fahrt"
        tempo = 115.0
    elif minute < 135:                 # laden, das Auto steht
        soc = min(80.0, soc + 1.5)
        phase = "laden"
        tempo = 0.0
    elif minute < 150:                 # Funkloch - es kommt nichts herein
        # Gefahren wird trotzdem: Das Auto bewegt sich, der Zaehler laeuft,
        # nur die Meldung geht nicht raus. Genau die Luecke, die der
        # Kilometerstand hinterher wieder schliesst.
        lat += 0.0135
        km_stand += 1.5 * KURVENZUSCHLAG
        soc -= 0.55
        continue
    else:                              # weiterfahren
        lat += 0.0135
        km_stand += 1.5 * KURVENZUSCHLAG
        soc -= 0.55
        phase = "fahrt2"
        tempo = 115.0

    roh = {"soc_roh": int(soc * 2.5), "spannung_v": 392.0,
           "strom_a": -120.0 if phase == "laden" else 48.0,
           "tempo_kmh": tempo, "aussentemp_c": 4.0, "innentemp_c": 21.0}
    if minute % 10 == 0:               # nicht jede Runde antwortet alles
        roh["nebenverbrauch_kw"] = 2.1
        roh["km_stand"] = round(km_stand)
    else:
        roh["_fehlend"] = ["nebenverbrauch_kw", "km_stand"]

    antwort = client.post(f"/api/live/{auf_id}/punkt", json={
        "lat": round(lat, 6), "lon": round(lon, 6), "soc": round(soc, 1),
        "tempo_kmh": tempo, "aussentemp_c": 4.0, "rohwerte": roh})
    if antwort.status_code >= 400:
        punkte_fehler += 1
        if punkte_fehler <= 2:
            befund(f"Aufzeichnungspunkt ({phase}, Minute {minute}) abgelehnt: "
                   f"HTTP {antwort.status_code} {antwort.text[:200]}", "FEHLER")
    else:
        punkte_ok += 1
        phase_log.append((phase, antwort.json()))

    # Der Zeitstempel wird vom Server gesetzt; für die Ladepausen-Erkennung
    # muss er stimmen, also nachziehen.
    db = SessionLocal()
    try:
        s = db.get(models.LiveSitzung, auf_id)
        if s.punkte:
            s.punkte[-1].zeit = zeit
            db.commit()
    finally:
        db.close()

print(f"  {punkte_ok} Punkte angenommen, {punkte_fehler} abgelehnt")

# Erkennt das Aufräumen die Ladepause? Der letzte Punkt ist 200 min alt.
from app.energie import ladephasen                 # noqa: E402
from app.live import aufraeumen                    # noqa: E402

db = SessionLocal()
try:
    s = db.get(models.LiveSitzung, auf_id)
    laedt = ladephasen.laedt_am_ende(s.punkte, aufraeumen.LADEFENSTER_MINUTEN,
                                     aufraeumen.LADEHUB_PROZENT)
    print(f"  Aufräumen sieht Ladevorgang am Ende: {laedt} "
          f"(letzter Punkt war 'fahrt2', also erwartet: False)")
    if laedt:
        befund("Das Aufräumen hält eine beendete Fahrt für eine Ladepause - "
               "die Erkennung schaut zu weit zurück", "FEHLER")
finally:
    db.close()

ende = client.post(f"/api/live/{auf_id}/ende")
if ende.status_code >= 400:
    befund(f"Beenden scheitert: HTTP {ende.status_code} {ende.text[:300]}",
           "FEHLER")
else:
    e = ende.json()
    gebaut = e.get("aufzeichnung") or {}
    print(f"  Abgeschlossen: {gebaut}")
    print(f"  Gelernt: {e.get('gelernt')}  /  nicht: {e.get('nicht_gelernt')}")
    if not gebaut.get("ok"):
        befund(f"Die Aufzeichnung liess sich nicht abschliessen: "
               f"{gebaut.get('grund')}", "FEHLER")
    else:
        # Gefahren wurden rund 2 x 45 Minuten a 115 km/h ~ 170 km Luftlinie.
        if not (80.0 < gebaut["strecke_km"] < 400.0):
            befund(f"Rekonstruierte Strecke {gebaut['strecke_km']} km passt "
                   f"nicht zum Gefahrenen", "FEHLER")
        if gebaut.get("hoehen") == "flach":
            befund("Höhen 'flach' trotz gesetztem ORS-Schlüssel", "HINWEIS")
        if gebaut.get("aussentemp_c") not in (4.0, 4):
            befund(f"Gemessene Aussentemperatur ging verloren: "
                   f"{gebaut.get('aussentemp_c')} statt 4.0", "FEHLER")

    # Die Ladepause darf den gelernten Faktor nicht verderben: 45 Minuten
    # Stillstand mit steigendem Ladestand sind kein Verbrauch.
    if e.get("gelernt"):
        roh_faktor = e["gelernt"]["rohfaktor"]
        print(f"  Rohfaktor der Fahrt: {roh_faktor}")
        if not (0.5 < roh_faktor < 2.0):
            befund(f"Gelernter Rohfaktor {roh_faktor} ist unplausibel - die "
                   f"Ladepause wird vermutlich als Verbrauch gerechnet",
                   "FEHLER")

# Die zusammengefuehrte Ladeerkennung an der echten Messreihe nachrechnen.
# **Nach** dem Abschliessen: Bei einer Aufzeichnung tragen die Messpunkte
# erst dann einen Kilometerstand - vorher gibt es keine Strecke, auf die
# man sie legen koennte.
db = SessionLocal()
try:
    s = db.get(models.LiveSitzung, auf_id)
    alle = ladephasen.abschnitte(s.punkte)
    lade = [a for a in alle if a.laedt]
    verbraucht_pp, gefahren_km = ladephasen.verbrauch(s.punkte)
    print(f"  Abschnitte: {len(alle)}, davon ladend: {len(lade)} "
          f"({ladephasen.geladen_pp(s.punkte):.1f} pp nachgeladen)")
    print(f"  Verbrauch ohne Ladeabschnitte: {verbraucht_pp:.1f} pp "
          f"über {gefahren_km:.1f} km")
    if not lade:
        befund("Die Ladepause wird in den Abschnitten nicht erkannt", "FEHLER")
    if verbraucht_pp <= 0:
        befund(f"Verbrauch über die Fahrabschnitte ist {verbraucht_pp:.1f} pp "
               f"- da stimmt das Vorzeichen nicht", "FEHLER")
finally:
    db.close()

# Die Fahrt in der Historie
db = SessionLocal()
try:
    fahrt = db.get(models.Fahrt, auf["fahrt_id"])
    print(f"  Historie: {fahrt.start_text!r} → {fahrt.ziel_text!r}, "
          f"{(fahrt.strecke_m or 0)/1000:.1f} km, "
          f"{fahrt.aussentemp_c} °C, Geometrie {len(fahrt.geometrie or [])}")
    if (fahrt.ziel_text or "") == "unterwegs":
        befund("Das Ziel der Aufzeichnung heisst hinterher immer noch "
               "'unterwegs' - in der Fahrtenliste steht dann 'X → unterwegs'",
               "FEHLER")
finally:
    db.close()

liste = client.get("/api/fahrten").json()
print(f"  /api/fahrten liefert {len(liste)} Fahrten")
for f in liste:
    print(f"    {f.get('start')} → {f.get('ziel')}  "
          f"{f.get('strecke_km')} km  {f.get('verbrauch_kwh_100km')} kWh/100  "
          f"aufz={f.get('aufzeichnung')}")
    if f.get("strecke_km") in (None, 0):
        befund(f"Fahrt {f.get('id')} hat keine Strecke in der Liste",
               "HINWEIS")


# ---------------------------------------------------------------------------

sagen("Befunde")
if not BEFUNDE:
    print("  Keine.")
for schwere, text in BEFUNDE:
    print(f"  [{schwere}] {text}")
print(f"\n{sum(1 for s, _ in BEFUNDE if s == 'FEHLER')} Fehler, "
      f"{sum(1 for s, _ in BEFUNDE if s == 'HINWEIS')} Hinweise")
