#!/usr/bin/env python3
"""Die ganze Kette einmal durchspielen - ohne Netz, ohne Postgres, ohne Auto.

Geprüft wird gegen eine frische SQLite-Datei mit dem Demo-Routing: Schema,
Fahrzeuge, Route, Ladesäulen-Import, Korridor-Suche und die Live-Nachführung
inklusive Simulator.

Genau das ist der Punkt: Ohne diesen Lauf liesse sich die Live-Funktion erst
prüfen, wenn ein Auto, ein Datenlieferant und eine echte Langstrecke
zusammenkommen - also nie beim Entwickeln.

    ./tools/check_backend.py
"""
import os
import sys
import tempfile

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HIER, "..", "backend"))

# Vor jedem App-Import setzen: Die Engine wird beim Import gebaut.
_DB = os.path.join(tempfile.mkdtemp(prefix="jolt-check-"), "check.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.pop("ORS_API_KEY", None)      # Demo-Routing erzwingen
os.environ.pop("APP_PASSWORT", None)     # kein Login im Prüflauf

from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.laden.saeulen_import import aus_bnetza_csv  # noqa: E402
from app.live import simulator  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402

FEHLER: list[str] = []


def pruefe(bedingung, text: str, zusatz: str = "") -> None:
    if bedingung:
        print(f"  ok    {text}")
    else:
        print(f"  FEHLT {text}   {zusatz}")
        FEHLER.append(text)


# Ein Ausschnitt im Format des amtlichen Registers: Vorspann, Semikolon,
# Dezimalkomma, Steckerblöcke.
#
# Die Koordinaten liegen auf der Luftlinie Hamburg-München, weil das
# Demo-Routing genau diese Linie erzeugt. Die Ortsnamen sind deshalb nur
# Etiketten - ein echtes Routing führt über andere Punkte.
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
    pruefe(status["demo_routing"] is True, "Demo-Routing ist aktiv (kein Schlüssel)")
    pruefe(status["passwort_noetig"] is False, "ohne APP_PASSWORT offener Zugang")

    print("\nFahrzeuge")
    fahrzeuge = client.get("/api/fahrzeuge").json()
    pruefe(len(fahrzeuge) == 1, "beim ersten Start wird ein Fahrzeug angelegt",
           f"sind {len(fahrzeuge)}")
    pruefe(len(fahrzeuge[0]["ladekurve"]) >= 5,
           "und es hat eine Ladekurve mit mehreren Stützstellen")
    pruefe(len(client.get("/api/fahrzeuge/vorlagen").json()) >= 4,
           "es gibt mehrere Vorlagen zur Auswahl")

    neu = client.post("/api/fahrzeuge", json={
        "name": "Prüfwagen", "akku_brutto_kwh": 82.0, "akku_netto_kwh": 77.0,
        "max_ladeleistung_kw": 150.0,
        "ladekurve": [[0, 120], [20, 150], [50, 100], [80, 50], [100, 8]]}).json()
    pruefe(neu["id"] != fahrzeuge[0]["id"], "ein zweites Fahrzeug lässt sich anlegen")
    pruefe(len(neu["ladekurve"]) == 5, "mit eigener Ladekurve")
    fehler = client.post("/api/fahrzeuge", json={
        "name": "Unsinn", "akku_brutto_kwh": 50.0, "akku_netto_kwh": 60.0})
    pruefe(fehler.status_code == 400, "netto über brutto wird abgelehnt",
           f"HTTP {fehler.status_code}")

    print("\nLadesäulen-Import (Format der Bundesnetzagentur)")
    db = SessionLocal()
    try:
        zaehler = aus_bnetza_csv(db, CSV_PROBE.encode("utf-8"))
        nochmal = aus_bnetza_csv(db, CSV_PROBE.encode("utf-8"))
    finally:
        db.close()
    pruefe(zaehler["neu"] == 6, "sechs Ladepunkte eingelesen",
           f"sind {zaehler['neu']}")
    pruefe(zaehler["uebersprungen"] == 1,
           "die Zeile mit Koordinate 0/0 wird verworfen")
    pruefe(nochmal["neu"] == 0 and nochmal["aktualisiert"] == 6,
           "ein zweiter Lauf legt nichts doppelt an - der Import ist idempotent",
           f"{nochmal}")

    db = SessionLocal()
    try:
        harz = db.query(models.Ladepunkt).filter(
            models.Ladepunkt.ort == "Goslar").one()
        sylt = db.query(models.Ladepunkt).filter(
            models.Ladepunkt.ort == "Westerland").one()
        lueneburg = db.query(models.Ladepunkt).filter(
            models.Ladepunkt.ort == "Lüneburg").one()
    finally:
        db.close()
    pruefe(harz.max_kw == 350.0, "Leistung mit Dezimalkomma korrekt gelesen",
           f"ist {harz.max_kw}")
    pruefe("CCS" in harz.steckertypen and "CHAdeMO" in harz.steckertypen,
           "beide Steckertypen erkannt", harz.steckertypen)
    pruefe(lueneburg.steckertypen == "Typ2",
           "'AC Steckdose Typ 2' wird auf Typ2 abgebildet",
           lueneburg.steckertypen)
    pruefe(abs(harz.lat - 51.9) < 1e-6, "Koordinate mit Komma korrekt gelesen",
           f"ist {harz.lat}")

    print("\nRoute Hamburg - München")
    antwort = client.post("/api/route", json={
        "fahrzeug_id": fahrzeuge[0]["id"],
        "start": {"lat": 53.5511, "lon": 9.9937, "text": "Hamburg"},
        "ziel": {"lat": 48.1351, "lon": 11.5820, "text": "München"},
        "start_soc": 80.0})
    pruefe(antwort.status_code == 200, "Route wird gerechnet",
           f"HTTP {antwort.status_code}: {antwort.text[:120]}")
    varianten = antwort.json()["varianten"]
    # Der Demo-Adapter kennt keinen Unterschied zwischen den drei ORS-Vorgaben
    # und liefert für alle dieselbe Luftlinie - /api/route erkennt das und legt
    # sie zu einer einzigen Variante mit allen Etiketten zusammen.
    pruefe(len(varianten) == 1,
           "die drei Vorgaben ergeben im Demo-Modus dieselbe Route",
           f"{len(varianten)} Varianten")
    route = varianten[0]
    fahrt_id = route["fahrt_id"]
    pruefe(set(route["etiketten"]) == {"schnellste", "kürzeste", "empfohlene",
                                       "sparsamste"},
           "und trägt alle vier Etiketten", str(route["etiketten"]))
    pruefe(route["demo"] is True, "und ist als Demo gekennzeichnet")
    pruefe(500 < route["strecke_km"] < 900, "Strecke plausibel",
           f"{route['strecke_km']} km")
    pruefe(14 < route["verbrauch_kwh_100km"] < 30, "Verbrauch plausibel",
           f"{route['verbrauch_kwh_100km']} kWh/100 km")
    pruefe(route["reicht"] is False,
           "ein 60-kWh-Auto schafft die Strecke nicht ohne Nachladen")
    pruefe(route["reserve_punkt"] is not None,
           "und die Reserve-Marke hat eine Koordinate für die Karte")
    pruefe(route["reserve_bei_km"] < route["strecke_km"],
           "die Marke liegt vor dem Ziel")

    kurz = client.post("/api/route", json={
        "fahrzeug_id": fahrzeuge[0]["id"],
        "start": {"lat": 53.5511, "lon": 9.9937, "text": "Hamburg"},
        "ziel": {"lat": 53.0793, "lon": 8.8017, "text": "Bremen"},
        "start_soc": 80.0}).json()["varianten"][0]
    pruefe(kurz["reicht"] is True, "Hamburg-Bremen reicht dagegen locker",
           f"SoC am Ziel {kurz['soc_am_ziel']} %")

    print("\nLadepunkte im Korridor")
    korridor = client.get(f"/api/saeulen/entlang/{fahrt_id}",
                          params={"min_kw": 100, "radius_km": 25}).json()
    orte = {k["name"].split()[0] for k in korridor["kandidaten"]}
    pruefe(korridor["anzahl"] == 4,
           "alle vier Schnelllader entlang der Route gefunden",
           f"sind {korridor['anzahl']}: {orte}")
    pruefe(not any("Sylt" in k["name"] for k in korridor["kandidaten"]),
           "Sylt liegt nicht auf dem Weg und taucht nicht auf", str(orte))
    pruefe(not any(k["max_kw"] < 100 for k in korridor["kandidaten"]),
           "der 22-kW-Anschluss in Lüneburg fällt durch den Leistungsfilter")
    km = [k["km_auf_route"] for k in korridor["kandidaten"]]
    pruefe(km == sorted(km), "sortiert nach Fortschritt entlang der Route", str(km))
    pruefe(all(k["umweg_minuten"] > 0 for k in korridor["kandidaten"]),
           "jeder Kandidat hat einen bezifferten Umweg")

    erster = korridor["kandidaten"][0]
    client.post(f"/api/saeulen/{erster['id']}/belegt")
    nachher = client.get(f"/api/saeulen/entlang/{fahrt_id}",
                         params={"min_kw": 100, "radius_km": 25}).json()
    gemeldet = [k for k in nachher["kandidaten"] if k["id"] == erster["id"]]
    pruefe(gemeldet and gemeldet[0]["belegt_gemeldet"] is True,
           "eine Belegt-Meldung schlägt in der Korridor-Antwort durch")
    client.delete(f"/api/saeulen/{erster['id']}/belegt")

    print("\nLadeplan")
    # Die Demo-Route ist die Luftlinie, die Ladepunkte der Probe stehen an der
    # A7. Dadurch liegen sie weiter neben der Strecke, als sie es neben einer
    # echten Strasse täten - deshalb hier eine grosszügigere Umweg-Grenze als
    # die zehn Minuten, mit denen der Optimierer sonst arbeitet.
    LADEPLAN = {"min_kw": 100, "radius_km": 25, "umweg_grenze_min": 15}
    plan = client.post(f"/api/fahrten/{fahrt_id}/ladeplan",
                       params=LADEPLAN).json()
    pruefe(plan["machbar"] is True,
           "für die Strecke, die ohne Nachladen nicht reicht, entsteht ein Plan",
           plan.get("grund", ""))
    pruefe(plan["anzahl_stopps"] >= 1, "mit mindestens einem Ladestopp",
           f"{plan['anzahl_stopps']}")
    pruefe(plan["soc_am_ziel"] >= fahrzeuge[0]["ziel_soc"] - 0.5,
           "und der Ziel-Ladestand wird erreicht",
           f"{plan['soc_am_ziel']} % statt {fahrzeuge[0]['ziel_soc']} %")
    pruefe(plan["gesamt_minuten"] > plan["fahrzeit_minuten"],
           "die Gesamtzeit liegt über der reinen Fahrzeit - Laden kostet Zeit")
    km_stopps = [s["km_auf_route"] for s in plan["stopps"]]
    pruefe(km_stopps == sorted(km_stopps),
           "die Stopps stehen in Fahrtreihenfolge", str(km_stopps))
    pruefe(all(s["ankunft_soc"] >= fahrzeuge[0]["reserve_soc"] - 0.5
               for s in plan["stopps"]),
           "an keinem Stopp wird unter der Reserve angekommen",
           str([s["ankunft_soc"] for s in plan["stopps"]]))
    pruefe(all(s["abfahrt_soc"] > s["ankunft_soc"] for s in plan["stopps"]),
           "und an jedem Stopp wird tatsächlich geladen")
    pruefe(all(s["lat"] and s["lon"] for s in plan["stopps"]),
           "jeder Stopp hat eine Koordinate für die Karte")

    # Die Belegt-Meldung ist die einzige Verfügbarkeitsinformation, die stimmt -
    # sie muss den Plan verändern, nicht nur die Liste einfärben.
    if plan["stopps"]:
        geplant = plan["stopps"][0]["id"]
        client.post(f"/api/saeulen/{geplant}/belegt")
        danach = client.post(f"/api/fahrten/{fahrt_id}/ladeplan",
                             params=LADEPLAN).json()
        pruefe(geplant not in [s["id"] for s in danach["stopps"]],
               "ein als belegt gemeldeter Stopp verschwindet aus dem Plan")
        client.delete(f"/api/saeulen/{geplant}/belegt")

    eng = client.post(f"/api/fahrten/{fahrt_id}/ladeplan",
                      params={**LADEPLAN, "umweg_grenze_min": 0.5}).json()
    pruefe(eng["machbar"] is False,
           "mit einer Umweg-Grenze unter jedem Kandidaten bleibt nichts übrig")
    pruefe(bool(eng["grund"]),
           "und die Antwort sagt, warum - nicht nur, dass es nicht geht",
           eng["grund"])

    ohne_profil = client.post("/api/fahrten/999999/ladeplan")
    pruefe(ohne_profil.status_code == 404,
           "eine unbekannte Fahrt wird sauber abgelehnt",
           f"HTTP {ohne_profil.status_code}")

    print("\nLive-Nachführung")
    sitzung = client.post(f"/api/live/start/{fahrt_id}").json()
    sitzung_id = sitzung["sitzung_id"]

    db = SessionLocal()
    try:
        fahrt = db.get(models.Fahrt, fahrt_id)
        punkte_planmaessig = simulator.schritte(fahrt, mehrverbrauch=1.0)
        punkte_hungrig = simulator.schritte(fahrt, mehrverbrauch=1.25)
    finally:
        db.close()
    pruefe(len(punkte_planmaessig) > 12, "der Simulator erzeugt Messpunkte",
           f"sind {len(punkte_planmaessig)}")
    pruefe(len(punkte_hungrig) < len(punkte_planmaessig),
           "mit 25 % Mehrverbrauch kommt er sichtbar kürzer, bevor der Akku "
           "leer ist",
           f"{punkte_hungrig[-1]['km']} km gegen "
           f"{punkte_planmaessig[-1]['km']} km")
    pruefe(punkte_hungrig[8]["soc"] < punkte_planmaessig[8]["soc"] - 1.0,
           "und liegt auf halber Strecke deutlich tiefer",
           f"{punkte_hungrig[8]['soc']} gegen {punkte_planmaessig[8]['soc']} %")

    # Plangemäss fahren: die Nachführung darf nicht anschlagen.
    for messpunkt in punkte_planmaessig[:12]:
        zustand = client.post(f"/api/live/{sitzung_id}/punkt", json={
            "lat": messpunkt["lat"], "lon": messpunkt["lon"],
            "soc": messpunkt["soc"]}).json()
    pruefe(abs(zustand["abweichung_pp"]) < 1.0,
           "wer nach Plan fährt, weicht nicht ab",
           f"{zustand['abweichung_pp']} Prozentpunkte")
    pruefe(0.9 < zustand["verbrauchsfaktor"] < 1.1,
           "und der Verbrauchsfaktor bleibt bei 1",
           f"ist {zustand['verbrauchsfaktor']}")
    pruefe(zustand["abstand_zur_route_m"] < 500,
           "die Position liegt auf der Route")

    # Mehrverbrauch: jetzt muss die Nachführung anschlagen.
    sitzung2 = client.post(f"/api/live/start/{fahrt_id}").json()["sitzung_id"]
    for messpunkt in punkte_hungrig[:12]:
        zustand2 = client.post(f"/api/live/{sitzung2}/punkt", json={
            "lat": messpunkt["lat"], "lon": messpunkt["lon"],
            "soc": messpunkt["soc"]}).json()
    pruefe(zustand2["verbrauchsfaktor"] > 1.15,
           "25 % Mehrverbrauch werden als Faktor erkannt",
           f"ist {zustand2['verbrauchsfaktor']}")
    pruefe(zustand2["abweichung_pp"] < zustand["abweichung_pp"],
           "der Ist-SoC liegt unter dem Soll",
           f"{zustand2['abweichung_pp']} Prozentpunkte")
    pruefe(zustand2["reserve_bei_km"] is not None
           and zustand2["reserve_bei_km"] < route["reserve_bei_km"],
           "und die Reserve rückt nach vorn - genau das ist die Live-Funktion",
           f"geplant km {route['reserve_bei_km']}, "
           f"jetzt km {zustand2['reserve_bei_km']}")
    pruefe(zustand2["neuplanung_noetig"] is True,
           "die Neuplanung wird angefordert", zustand2["grund"])

    # Abseits der Route. Geprüft wird hier nur die Messung - dass daraus erst
    # nach einer Minute eine Neuplanung wird, hängt an Zeitstempeln und steht
    # deshalb in check_umplanung.py, wo sie sich setzen lassen.
    abseits = client.post(f"/api/live/{sitzung2}/punkt", json={
        "lat": 54.9, "lon": 8.31, "soc": 40.0}).json()
    pruefe(abseits["abstand_zur_route_m"] > 500,
           "ein Sprung weg von der Route wird als Abstand erkannt",
           f"{abseits['abstand_zur_route_m']} m")

    beendet = client.post(f"/api/live/{sitzung2}/ende").json()
    pruefe(beendet["ok"] is True, "die Sitzung lässt sich beenden")
    gesperrt = client.post(f"/api/live/{sitzung2}/punkt", json={
        "lat": 52.0, "lon": 10.0, "soc": 30.0})
    pruefe(gesperrt.status_code == 409,
           "danach werden keine Messpunkte mehr angenommen",
           f"HTTP {gesperrt.status_code}")

    print("\nOberfläche wird ausgeliefert")
    seite = client.get("/")
    pruefe(seite.status_code == 200 and b"jolt" in seite.content.lower(),
           "index.html kommt zurück")
    pruefe(client.get("/manifest.json").status_code == 200, "manifest.json auch")
    pruefe(client.get("/static/karte.js").status_code == 200, "und die Skripte")
    pruefe("Content-Security-Policy" in seite.headers,
           "die Security-Header sitzen")

    print()
    if FEHLER:
        print(f"{len(FEHLER)} Prüfung(en) fehlgeschlagen:")
        for f in FEHLER:
            print(f"  - {f}")
        return 1
    print("Alle Prüfungen bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
