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
import re
import sys
import tempfile

WERKZEUGE = os.path.dirname(os.path.abspath(__file__))
# Das Frontend liegt im Repo neben backend/, im Image direkt neben tools/.
FRONTEND = next(
    (p for p in (os.path.join(WERKZEUGE, "..", "frontend"),)
     if os.path.isdir(p)), os.path.join(WERKZEUGE, "..", "frontend"))
sys.path.insert(0, WERKZEUGE)
from pruefen import Pruefung, anwendung_bereitstellen  # noqa: E402

anwendung_bereitstellen("backend", datenbank=False)

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

pruefe = Pruefung()


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

    # Regression: ein doppelter Ladestand in der Kurve verletzt die
    # Unique-Constraint (fahrzeug_id, soc_prozent) - das darf als
    # verständliche 400 ankommen, nicht als nackter 500er beim Commit.
    doppelt = client.post("/api/fahrzeuge", json={
        "name": "Doppelte Kurve", "akku_brutto_kwh": 82.0, "akku_netto_kwh": 77.0,
        "ladekurve": [[0, 180], [20, 180], [80, 80], [90, 90], [90, 60],
                     [100, 45]]})
    pruefe(doppelt.status_code == 400,
           "ein doppelter Ladestand in der Kurve wird sauber abgelehnt",
           f"HTTP {doppelt.status_code}: {doppelt.text[:120]}")
    pruefe("90" in doppelt.json().get("detail", ""),
           "und die Meldung nennt den betroffenen Ladestand",
           doppelt.json())

    # Regression: Ein Fahrzeug ändern und dabei dieselben Ladestände wie
    # zuvor behalten (nur die kW-Werte ändern - der Normalfall beim
    # Bearbeiten) darf nicht crashen. SQLAlchemy schreibt im selben Flush
    # sonst die neuen Zeilen vor dem Löschen der alten und verletzt die
    # Unique-Constraint, obwohl die neue Kurve für sich genommen keine
    # Duplikate hat.
    geaendert = client.put(f"/api/fahrzeuge/{fahrzeuge[0]['id']}", json={
        "name": fahrzeuge[0]["name"], "akku_brutto_kwh": fahrzeuge[0]["akku_brutto_kwh"],
        "akku_netto_kwh": fahrzeuge[0]["akku_netto_kwh"],
        "ladekurve": [[soc, kw + 5] for soc, kw in fahrzeuge[0]["ladekurve"]]})
    pruefe(geaendert.status_code == 200,
           "dieselben Ladestände beim Ändern zu behalten funktioniert",
           f"HTTP {geaendert.status_code}: {geaendert.text[:150]}")

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
        # Nur die Existenz zählt: `.one()` wirft, wenn der Datensatz fehlt
        # oder doppelt ist - beides wäre ein Importfehler.
        db.query(models.Ladepunkt).filter(
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

    print("\nDoppelter fremd_id innerhalb eines Imports")
    # Regression: eine Mehrländer-Abfrage bei Open Charge Map kann einen
    # Standort nahe der Grenze zweimal liefern. Ohne Flush zwischen zwei
    # _speichern()-Aufrufen für dieselbe fremd_id sieht die zweite Suche den
    # ersten, noch nicht committeten INSERT nicht - der zweite INSERT
    # verletzt dann die Unique-Constraint (quelle, fremd_id) und die ganze
    # Charge scheitert mit HTTP 500 (bzw. hier: einer nackten IntegrityError).
    from app.laden.saeulen_import import _speichern

    db = SessionLocal()
    try:
        erst = _speichern(db, "ocm", "pruef-doppelt", {
            "name": "Erststand", "betreiber": "", "lat": 50.0, "lon": 10.0,
            "adresse": "", "plz": "", "ort": "", "land": "DE",
            "anschluesse": [], "max_kw": 50.0, "anzahl_punkte": 1,
            "steckertypen": "CCS", "stand": ""})
        zweit = _speichern(db, "ocm", "pruef-doppelt", {
            "name": "Zweitstand", "betreiber": "", "lat": 50.0, "lon": 10.0,
            "adresse": "", "plz": "", "ort": "", "land": "DE",
            "anschluesse": [], "max_kw": 60.0, "anzahl_punkte": 1,
            "steckertypen": "CCS", "stand": ""})
        db.commit()
        pruefe(erst == "neu" and zweit == "aktualisiert",
               "der zweite Aufruf für dieselbe fremd_id aktualisiert, statt "
               "ein Duplikat anzulegen", f"{erst}, {zweit}")
    finally:
        db.close()

    print("\nOpen-Charge-Map-Import ohne compact=true")
    # Regression 1: compact=true lässt OCM AddressInfo.Country und
    # OperatorInfo als blosse IDs statt als Objekte liefern - "land" und
    # "betreiber" kamen dadurch für jeden importierten Punkt leer an.
    # Regression 2: eine Anfrage mit kommagetrennten Ländercodes
    # (countrycode=AT,CH,...) wird von OCM nicht zuverlässig eingehalten - in
    # der Praxis kamen Standorte aus der ganzen Welt zurück, nicht nur aus den
    # angefragten Ländern. Beides wird direkt gegen eine gefälschte, aber
    # realistische (verbose) OCM-Antwort geprüft: kein compact-Parameter, und
    # ein Aufruf je Land statt einer kommagetrennten Liste.
    from app.laden import saeulen_import as saeulen_import_modul

    gesendete_countrycodes: list = []
    letzte_params: dict = {}

    class _GefaelschteOcmAntwort:
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
                # Genau die Felder, die der Import bisher weggeworfen hat.
                # Darin steht, was einen Ladepunkt für eine Fahrt
                # unbrauchbar macht - und das entscheidet mehr als jede
                # Zielfunktion.
                "StatusType": {"Title": "Operational", "IsOperational": True},
                "UsageType": {"Title": "Private - Restricted access",
                              "IsMembershipRequired": True},
                "UsageCost": "0,59 EUR/kWh",
                "GeneralComments": "Kabel kurz, für Kastenwagen ungeeignet",
                "AccessComments": "Hinter Schranke, nachts geschlossen",
                "DateLastVerified": "2026-07-01T00:00:00Z",
            }]

    def _gefaelschtes_ocm_get(url, timeout=None, params=None):
        letzte_params.clear()
        letzte_params.update(params or {})
        gesendete_countrycodes.append((params or {}).get("countrycode"))
        return _GefaelschteOcmAntwort()

    ocm_get_original = saeulen_import_modul.requests.get
    saeulen_import_modul.requests.get = _gefaelschtes_ocm_get
    db = SessionLocal()
    try:
        saeulen_import_modul.aus_ocm(db, "test-schluessel", laender=["AT", "CH"],
                                     max_ergebnisse=1)
        pruefe("compact" not in letzte_params,
               "compact=true wird nicht mehr gesendet", str(letzte_params))
        pruefe(gesendete_countrycodes == ["AT", "CH"],
               "jedes Land wird einzeln angefragt, nicht als kommagetrennte Liste",
               str(gesendete_countrycodes))
        eintrag = (db.query(models.Ladepunkt)
                   .filter_by(quelle="ocm", fremd_id="999001").one())
        pruefe(eintrag.land == "AT", "das Land wird aus der Antwort übernommen",
               f"ist {eintrag.land!r}")
        pruefe(eintrag.betreiber == "EnBW mobility+",
               "und der Betreiber ebenso", f"ist {eintrag.betreiber!r}")
    finally:
        saeulen_import_modul.requests.get = ocm_get_original
        db.close()

    # Was in Worten dasteht, wird jetzt aufgehoben. Ein Ladepunkt kann
    # tadellos aussehen - 150 kW, zwei Säulen - und trotzdem unbrauchbar
    # sein, weil er hinter einer Schranke steht.
    db = SessionLocal()
    try:
        lp = db.query(models.Ladepunkt).filter_by(quelle="ocm",
                                                  fremd_id="999001").one()
        pruefe(lp.betriebsbereit is True,
               "der Betriebszustand wird übernommen", str(lp.betriebsbereit))
        pruefe(lp.zugang == "Private - Restricted access",
               "die Zugangsart auch", str(lp.zugang))
        pruefe(lp.mitgliedschaft_noetig is True,
               "und ob eine Mitgliedschaft nötig ist")
        pruefe((lp.hinweise or {}).get("kosten") == "0,59 EUR/kWh",
               "der Preistext der Quelle wird aufgehoben",
               str(lp.hinweise))
        pruefe("Kastenwagen" in (lp.hinweise or {}).get("allgemein", ""),
               "und die Kommentare - hier steht, was kein Datenfeld verrät",
               str((lp.hinweise or {}).get("allgemein")))
        pruefe("Schranke" in (lp.hinweise or {}).get("zugang", ""),
               "Zugangshinweise ebenso")
        pruefe("geprueft_am" in (lp.hinweise or {}),
               "und wann die Angabe zuletzt geprüft wurde")
    finally:
        db.close()

    print("\nOpen-Charge-Map-Import entlang einer Strecke")
    # Regression: aus_ocm()s offset-Pagination blättert bei einem sehr grossen
    # Land nicht zuverlässig durch (siehe oben) - aus_ocm_route() fragt
    # stattdessen mehrere Umkreise entlang der Streckengeometrie ab. Geprüft
    # wird: mehrere Anker bei einer längeren Strecke, und ein Standort, den
    # zwei überlappende Umkreise beide sehen, wird nur einmal gezählt.
    strecke = [[16.37 + 0.01 * i, 48.21] for i in range(151)]  # ~150 km Ost-West

    angefragte_anker: list = []

    class _GefaelschteRoutenAntwort:
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

    def _gefaelschter_routen_get(url, timeout=None, params=None):
        angefragte_anker.append((params.get("latitude"), params.get("longitude")))
        return _GefaelschteRoutenAntwort()

    ocm_get_original = saeulen_import_modul.requests.get
    saeulen_import_modul.requests.get = _gefaelschter_routen_get
    db = SessionLocal()
    try:
        zaehler = saeulen_import_modul.aus_ocm_route(
            db, "test-schluessel", strecke, radius_km=30.0)
        pruefe(len(angefragte_anker) >= 2,
               "eine längere Strecke fragt mehrere Umkreise ab",
               f"{len(angefragte_anker)} Anker")
        pruefe(zaehler["neu"] == 1,
               "ein Standort, den mehrere überlappende Umkreise sehen, "
               "wird nur einmal gezählt", str(zaehler))
    finally:
        saeulen_import_modul.requests.get = ocm_get_original
        db.close()

    print("\nOrtssuche ohne Länderfilter")
    # Regression: `land` stand früher fest auf "DE" und /api/orte fragte damit
    # nie explizit - jedes Ziel jenseits der Grenze verschwand über
    # ORS' boundary.country-Filter. Das Demo-Routing kennt keinen echten
    # HTTP-Aufruf, deshalb wird hier der ORS-Adapter direkt geprüft: welche
    # Parameter tatsächlich an openrouteservice gingen.
    from app.routing.ors import ORS
    import app.routing.ors as ors_modul

    angefragt: dict = {}

    class _GefaelschteAntwort:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"features": []}

    def _gefaelschtes_get(url, timeout=None, params=None):
        angefragt.clear()
        angefragt.update(params or {})
        return _GefaelschteAntwort()

    ors_get_original = ors_modul.requests.get
    ors_modul.requests.get = _gefaelschtes_get
    try:
        ORS(api_key="test").suchen("Paris")
        pruefe("boundary.country" not in angefragt,
               "ohne Land wird nicht mehr fest auf DE eingeschränkt",
               str(angefragt))
        ORS(api_key="test").suchen("Paris", land="FR")
        pruefe(angefragt.get("boundary.country") == "FR",
               "ein explizit gesetztes Land wird weiterhin übergeben",
               str(angefragt))
    finally:
        ors_modul.requests.get = ors_get_original

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
    # Vorgabe ist eine einzige Route, die schnellste.
    #
    # Vorher waren es drei ("fastest", "shortest", "recommended"). "shortest"
    # ist inzwischen ganz raus: Auf Le Gurp - Montchanin liefert sie 554 km
    # in 11,8 Stunden gegen 654 km in 6,3 - hundert Kilometer weniger,
    # gekauft mit fünfeinhalb Stunden. Und "recommended" ergibt auf
    # Autobahnstrecken meist dieselbe Strasse wie "fastest". Drei Anfragen
    # für eine Antwort, bei 2.500 ORS-Anfragen am Tag.
    pruefe(len(varianten) == 1,
           "ohne Alternative wird genau eine Route gerechnet",
           f"{len(varianten)} Varianten")
    route = varianten[0]
    fahrt_id = route["fahrt_id"]
    pruefe(route["etiketten"] == ["schnellste"],
           "und sie ist die schnellste", str(route["etiketten"]))

    # Mit Alternative kommt die mautfreie dazu. Das Demo-Routing erfindet
    # eine Luftlinie und kennt keine Mautstrassen - beide Anfragen ergeben
    # deshalb dieselbe Strecke, und /api/route legt sie zu einer Variante
    # mit beiden Etiketten zusammen. Genau das ist hier zu prüfen: dass die
    # Zusammenlegung greift und nicht zweimal dasselbe angeboten wird.
    mit_alt = client.post("/api/route", json={
        "fahrzeug_id": fahrzeuge[0]["id"],
        "start": {"lat": 53.5511, "lon": 9.9937, "text": "Hamburg"},
        "ziel": {"lat": 48.1351, "lon": 11.5820, "text": "München"},
        "start_soc": 80.0, "alternative": True}).json()["varianten"]
    pruefe(len(mit_alt) == 1,
           "im Demo-Modus ist die mautfreie Route dieselbe - sie wird "
           "zusammengelegt statt doppelt angeboten",
           f"{len(mit_alt)} Varianten")
    pruefe("mautfrei" in mit_alt[0]["etiketten"],
           "und das Etikett sagt es", str(mit_alt[0]["etiketten"]))
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

    # Der Regler "Aufwand je Halt" bis zum Optimierer durchgereicht. Bei null
    # ist Anhalten gratis, und der Plan zersplittert in Kurzstopps - genau das
    # Verhalten, das die Vorgabe von fünf Minuten verhindert.
    gratis = client.post(f"/api/fahrten/{fahrt_id}/ladeplan",
                         params={**LADEPLAN, "stopp_fixkosten_min": 0}).json()
    teuer = client.post(f"/api/fahrten/{fahrt_id}/ladeplan",
                        params={**LADEPLAN, "stopp_fixkosten_min": 20}).json()
    pruefe(gratis["haltekosten_minuten"] == 0,
           "mit Aufwand null kostet ein Halt nichts",
           str(gratis["haltekosten_minuten"]))
    pruefe(teuer["anzahl_stopps"] <= gratis["anzahl_stopps"],
           "und je teurer ein Halt, desto weniger Halte plant jolt",
           f"{teuer['anzahl_stopps']} bei 20 min gegen "
           f"{gratis['anzahl_stopps']} bei 0 min")
    pruefe(teuer["haltekosten_minuten"] == teuer["anzahl_stopps"] * 20,
           "die Haltekosten in der Bilanz sind Anzahl mal Aufwand",
           f"{teuer['haltekosten_minuten']} bei {teuer['anzahl_stopps']} Stopps")

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

    print("\nLogger im Auto meldet sich über das Fahrzeug")
    # Ein Gerät, das fest im Auto sitzt, kann die Sitzungs-ID nicht kennen:
    # Sie entsteht beim Losfahren in der App und wechselt mit jeder Fahrt.
    # Es weist sich deshalb mit dem Logger-Token des Fahrzeugs aus.
    fahrzeug_id = fahrzeuge[0]["id"]
    client.post(f"/api/live/{sitzung_id}/ende")      # erst mal Ruhe schaffen

    falsch = client.post("/api/live/melden", json={
        "token": "gibtesnicht", "lat": 53.5, "lon": 10.0, "soc": 50.0})
    pruefe(falsch.status_code == 401,
           "ein unbekanntes Token wird abgewiesen",
           f"HTTP {falsch.status_code}")

    token = client.post(
        f"/api/fahrzeuge/{fahrzeug_id}/logger-token").json()["logger_token"]
    pruefe(len(token) >= 32, "ein Logger-Token lässt sich erzeugen",
           f"{len(token)} Zeichen")
    liste = client.get("/api/fahrzeuge").json()[0]
    pruefe(liste.get("logger_aktiv") is True,
           "das Fahrzeug meldet, dass ein Logger eingerichtet ist")
    pruefe("logger_token" not in liste,
           "das Token selbst steht in keiner Listenantwort - es wird genau "
           "einmal gezeigt", str(list(liste.keys())))

    # Das Auto steht vor der Tür und der Logger sendet trotzdem. Das ist kein
    # Fehler: Ein unbeaufsichtigtes Gerät, das Fehlerantworten bekommt, fängt
    # an zu protokollieren oder schaltet sich ab.
    ruhend = client.post("/api/live/melden", json={
        "token": token, "lat": 53.5, "lon": 10.0, "soc": 50.0})
    pruefe(ruhend.status_code == 200
           and ruhend.json().get("aufgenommen") is False,
           "ohne laufende Fahrt wird nichts aufgenommen - aber es ist kein "
           "Fehler", f"HTTP {ruhend.status_code}: {ruhend.text[:120]}")

    sitzung3 = client.post(f"/api/live/start/{fahrt_id}").json()["sitzung_id"]
    messpunkt = punkte_planmaessig[3]
    gemeldet = client.post("/api/live/melden", json={
        "token": token, "lat": messpunkt["lat"], "lon": messpunkt["lon"],
        "soc": messpunkt["soc"]})
    pruefe(gemeldet.status_code == 200
           and gemeldet.json().get("aufgenommen") is True,
           "sobald eine Fahrt läuft, findet der Logger sie von allein",
           f"HTTP {gemeldet.status_code}: {gemeldet.text[:120]}")
    pruefe(gemeldet.json().get("sitzung_id") == sitzung3,
           "und zwar die richtige", f"{gemeldet.json().get('sitzung_id')} "
           f"statt {sitzung3}")
    pruefe(client.get(f"/api/live/{sitzung3}").json()["punkte"] == 1,
           "der Messpunkt liegt in dieser Sitzung")

    # Fremdes Format: dieselbe Meldung, in der Sprache von Iternio/ABRP. Das
    # ist der Weg, auf dem die OBD2-Daten hereinkommen werden - übersetzt
    # wird in live/quellen/, geprüft im Einzelnen von check_quellen.py.
    fremd = client.post("/api/live/melden", json={
        "token": token, "format": "abrp",
        "tlm": {"utc": 1787654321, "soc": 44.0, "lat": messpunkt["lat"],
                "lon": messpunkt["lon"], "speed": 98.0, "ext_temp": 19.0,
                "is_charging": 0}})
    pruefe(fremd.status_code == 200
           and fremd.json().get("aufgenommen") is True,
           "eine Meldung im ABRP-Format wird angenommen",
           f"HTTP {fremd.status_code}: {fremd.text[:140]}")
    pruefe(fremd.json().get("ist_soc") == 44.0,
           "und der Ladestand kommt übersetzt an",
           str(fremd.json().get("ist_soc")))
    pruefe(client.get(f"/api/live/{sitzung3}").json()["punkte"] == 2,
           "der übersetzte Punkt liegt in derselben Sitzung")

    kaputt = client.post("/api/live/melden", json={
        "token": token, "format": "abrp",
        "tlm": {"utc": 1787654321, "lat": 48.0, "lon": 11.0}})
    pruefe(kaputt.status_code == 400,
           "eine Meldung ohne Ladestand wird abgelehnt",
           f"HTTP {kaputt.status_code}")
    pruefe("soc" in kaputt.text.lower(),
           "und der Grund nennt das fehlende Feld", kaputt.text[:140])

    unbekannt = client.post("/api/live/melden", json={
        "token": token, "format": "torque", "lat": 48.0, "lon": 11.0,
        "soc": 50.0})
    pruefe(unbekannt.status_code == 400,
           "ein unbekanntes Format wird abgelehnt",
           f"HTTP {unbekannt.status_code}")

    # Ein neues Token entwertet das alte - sonst wäre "erneuern" wertlos.
    neues = client.post(
        f"/api/fahrzeuge/{fahrzeug_id}/logger-token").json()["logger_token"]
    pruefe(neues != token, "ein erneuertes Token ist ein anderes")
    alt = client.post("/api/live/melden", json={
        "token": token, "lat": messpunkt["lat"], "lon": messpunkt["lon"],
        "soc": messpunkt["soc"]})
    pruefe(alt.status_code == 401, "und das alte gilt nicht mehr",
           f"HTTP {alt.status_code}")

    client.delete(f"/api/fahrzeuge/{fahrzeug_id}/logger-token")
    pruefe(client.get("/api/fahrzeuge").json()[0].get("logger_aktiv") is False,
           "der Logger lässt sich wieder abmelden")
    entwertet = client.post("/api/live/melden", json={
        "token": neues, "lat": messpunkt["lat"], "lon": messpunkt["lon"],
        "soc": messpunkt["soc"]})
    pruefe(entwertet.status_code == 401,
           "danach wird von ihm nichts mehr angenommen",
           f"HTTP {entwertet.status_code}")
    client.post(f"/api/live/{sitzung3}/ende")

    print("\nOberfläche wird ausgeliefert")
    seite = client.get("/")
    pruefe(seite.status_code == 200 and b"jolt" in seite.content.lower(),
           "index.html kommt zurück")
    pruefe(client.get("/manifest.json").status_code == 200, "manifest.json auch")

    # Der Fehler, der viermal zugeschlagen hat: index.html wird nie
    # zwischengespeichert, die Dateien unter /static aber schon - Cloudflare
    # ersetzt dort das no-cache des Ursprungs durch max-age=14400. Der
    # Browser holt frisches HTML und fragt fürs JavaScript gar nicht erst
    # nach. Vier Stunden lang neue Oberfläche mit alter Logik.
    inhalt = seite.content
    pruefe(b"/static/app.js?v=" in inhalt and b"/static/fahrten.js?v=" in inhalt,
           "die Skriptverweise in index.html tragen eine Version - sonst "
           "zieht frisches HTML altes JavaScript nach",
           str([z for z in inhalt.split() if b"app.js" in z][:2]))
    pruefe(b'"/static/core.js"' not in inhalt,
           "und zwar alle, nicht nur einige",
           "core.js steht ohne Version im HTML")

    # Die OBD2-Seite liegt ausserhalb von /static, weil Cloudflare allem
    # darunter eine Browser-Frist von vier Stunden aufdrückt. Beim
    # Fehlersuchen im Auto ist das der Unterschied zwischen "die Änderung
    # wirkt nicht" und "die Änderung ist noch gar nicht da".
    obd = client.get("/obd")
    pruefe(obd.status_code == 200 and b"aufzeichnen" in obd.content.lower(),
           "die Aufzeichnungsseite wird unter /obd ausgeliefert",
           f"HTTP {obd.status_code}")
    pruefe("no-cache" in obd.headers.get("Cache-Control", ""),
           "und zwar ohne Cache - sonst hängt das Telefon auf einer alten "
           "Fassung fest", obd.headers.get("Cache-Control", "(keiner)"))
    pruefe(b"/static/obd.js?v=" in obd.content
           and b"/static/obd.css?v=" in obd.content,
           "die Verweise auf Skript und Stylesheet tragen eine Version - "
           "sonst zieht eine frische Seite altes JavaScript nach",
           str([z for z in obd.content.split() if b"obd." in z][:3]))
    # Ein eigenes Manifest, damit die Seite als Symbol auf dem
    # Home-Bildschirm liegt. Ohne das ist Aufzeichnen ein Weg durch Bluefy
    # und die Adresszeile - und damit etwas, das man sich für "nächstes Mal"
    # aufhebt.
    obd_manifest = client.get("/manifest-obd.json")
    pruefe(obd_manifest.status_code == 200
           and obd_manifest.json().get("start_url") == "/obd",
           "und hat ein eigenes Manifest, das direkt auf /obd startet",
           f"HTTP {obd_manifest.status_code}")
    pruefe(b"/manifest-obd.json" in obd.content,
           "auf das die Seite auch verweist - ein Manifest, das niemand "
           "verlinkt, legt kein Symbol an")

    pruefe(client.get("/static/karte.js").status_code == 200, "und die Skripte")

    # Jeder ausgelesene Messwert braucht eine Beschriftung, sonst steht im
    # Dashboard "ptc_strom_a" statt "Heizstrom". Die Liste wird an genau
    # einer Stelle gefuehrt (obd-kern.js) und von der Oberflaeche ueber
    # `FELDER` bezogen - sonst taucht eine neue Datenkennung dort nie auf.
    kern = open(os.path.join(FRONTEND, "obd-kern.js"), encoding="utf-8").read()
    eintraege = re.findall(r'\{ name: "([a-z_]+)",(.*?)(?=\n    \{ name:|\n  \];)',
                           kern, re.S)
    ohne = [n for n, rest in eintraege if "titel:" not in rest]
    pruefe(eintraege and not ohne,
           f"alle {len(eintraege)} ausgelesenen Messwerte tragen eine "
           f"Beschriftung fürs Dashboard", str(ohne))
    fehlende_einheit = [n for n, rest in eintraege
                        if "einheit:" not in rest]
    pruefe(not fehlende_einheit,
           "und eine Einheit - auch wenn sie null ist, muss die Entscheidung "
           "dastehen", str(fehlende_einheit))
    pruefe("FELDER: MESSWERTE.flatMap" in kern,
           "die Liste wird exportiert statt in der Oberfläche wiederholt")
    live = open(os.path.join(FRONTEND, "live.js"), encoding="utf-8").read()
    pruefe("joltObd.FELDER" in live,
           "und das Dashboard bezieht sie von dort - eine neue Datenkennung "
           "taucht damit von selbst auf")
    # Das Aufzeichnen braucht eine **eigene** Fahrzeugwahl. Vorher griff es
    # auf die der Planen-Ansicht zu und fiel, wenn die leer war, auf das
    # erste Fahrzeug der Liste zurueck - das beim ersten Start angelegte
    # "Allgemeine E-Auto". Zwei echte Testfahrten sind so dem falschen Auto
    # zugeschrieben worden.
    html = open(os.path.join(FRONTEND, "index.html"), encoding="utf-8").read()
    fahrten_js = open(os.path.join(FRONTEND, "fahrten.js"),
                      encoding="utf-8").read()
    pruefe('id="aufz-fahrzeug"' in html,
           "der Aufzeichnungs-Abschnitt hat eine eigene Fahrzeugwahl")
    pruefe("aufz-fahrzeug" in fahrten_js and "fahrzeug-wahl" not in fahrten_js,
           "und das Aufzeichnen nimmt sie, nicht die aus der Planen-Ansicht",
           "fahrten.js greift noch auf fahrzeug-wahl zu")
    pruefe("K.zustand.fahrzeuge || [])[0]" not in fahrten_js,
           "ohne Rückfall auf das erste Fahrzeug der Liste - lieber gar "
           "nicht aufzeichnen als dem falschen Auto")
    pruefe("aufz-fahrzeug" in open(os.path.join(FRONTEND, "fahrzeug.js"),
                                   encoding="utf-8").read(),
           "und sie wird mit den Fahrzeugen gefüllt")

    # Ein Steuergeraet auf 11-Bit-Kennung braucht ein anderes Protokoll.
    # Geht der Wechsel schief, darf das die Pflichtwerte derselben Runde
    # nicht kosten - deshalb stehen diese Abfragen zuletzt und der Wechsel
    # wird im finally zurueckgenommen.
    namen = re.findall(r'\{ name: "([a-z_]+)"', kern)
    klima = [n for n in ("aussentemp_c", "innentemp_c") if n in namen]
    pruefe(klima and all(namen.index(n) > namen.index("soc_roh")
                         for n in klima),
           "die Messwerte mit Protokollwechsel stehen hinter dem Ladestand - "
           "ein misslungener Wechsel darf die Pflichtwerte nicht mitreissen",
           str(namen))
    pruefe(namen and namen[-1] in ("aussentemp_c", "innentemp_c"),
           "und ganz am Ende der Runde", str(namen[-2:]))
    pruefe("} finally {" in kern and 'befehl("ATSP7")' in kern,
           "das Protokoll wird im finally zurückgesetzt - eine Sitzung, die "
           "im falschen Protokoll hängen bleibt, kostet jede weitere Runde")
    # Der Wiederaufbau darf nicht aufgeben, solange die Fahrt laeuft. Mit
    # der alten Obergrenze von sechs Versuchen war nach zweieinhalb Minuten
    # Schluss - fuenf Minuten mit der Seite im Hintergrund haben auf einer
    # echten Fahrt zwanzig Kilometer ohne einen Fahrzeugwert gekostet.
    pruefe("versuch >= grenze" not in kern,
           "der Wiederaufbau gibt nicht nach sechs Versuchen auf - `weiter` "
           "beendet ihn, wenn die Fahrt endet")
    pruefe("WIEDER_HOECHSTABSTAND_MS" in kern,
           "stattdessen ist nur der Abstand gedeckelt")
    pruefe("stilleGemeldet" in live and "alter > 180" in live,
           "und das Dashboard sagt einmal deutlich, wenn nichts mehr aus dem "
           "Auto kommt - eine Aufzeichnung ohne Ladestand taugt nicht zum "
           "Lernen, und das erfährt man sonst erst hinterher")

    pruefe("wechselGescheitert" in kern,
           "und ein gescheiterter Wechsel wird nicht endlos wiederholt")

    # Ohne Flusskontrolle scheitert jede Antwort, die nicht in einen CAN-
    # Rahmen passt - der ELM327 muss wissen, mit welchem Kopf er das
    # Flow-Control-Paket schickt. Das WiCAN-Fahrzeugprofil setzt die drei
    # Befehle vor jeder Abfrage; jolt setzte sie gar nicht, und genau
    # deshalb kam der Batteriestrom in keiner einzigen Runde an.
    for befehl in ("ATFCSH", "ATFCSD300000", "ATFCSM1"):
        pruefe(befehl in kern, f"die Flusskontrolle setzt {befehl}")
    pruefe(kern.count("await flusskontrolle(ziel)") >= 2,
           "und zwar auf beiden Wegen - mit und ohne Protokollwechsel")
    pruefe(all(f'fcsh: "{h}"' in kern
               for h in ("17FC007B", "17FC0076", "17FC00B9", "746", "710")),
           "jede Zieladresse bringt ihren eigenen Flow-Control-Kopf mit")

    pruefe("function mehrrahmen" in kern,
           "lange Antworten werden aus mehreren CAN-Rahmen zusammengesetzt - "
           "ohne das landen Köpfe und Steuerbytes als Nutzdaten im Ergebnis")
    pruefe("hex.slice(3) : hex.slice(8)" in kern,
           "und zwar für beide Rahmenbreiten: acht Kopfzeichen bei 29 Bit, "
           "drei bei 11 - der Klimakompressor sitzt auf der 11-Bit-Seite")
    pruefe("akku_kwh" in kern and "kwh >= 10 && kwh <= 200" in kern,
           "die Akkukapazität wird gegen eine Plausibilitätsgrenze gehalten - "
           "die Umrechnung ist nicht belegt, also lieber leer als erfunden")
    pruefe("K.zahl(z.ist_soc, 1)" in live,
           "der Ladestand steht mit einer Nachkommastelle da - der Dongle "
           "liefert ihn in Schritten von 0,4 pp, auf ganze Prozent gerundet "
           "steht die Zahl minutenlang still")
    pruefe("verbrauchZeichnen" in live and "verbrauchsabschnitte" in live,
           "es gibt einen Balkenplot des Verbrauchs je Zeitabschnitt")
    # Die Plausibilitaetspruefung im Stand. Der Kreuzvergleich ist der
    # schaerfere Teil: Entladezaehler geteilt durch Kilometerstand muss
    # einen sinnvollen Lebensdauerverbrauch ergeben, und das prueft beide
    # Byte-Lagen auf einmal - ohne eine einzige gefahrene Minute.
    obd_js = open(os.path.join(FRONTEND, "obd.js"), encoding="utf-8").read()
    obd_html = open(os.path.join(FRONTEND, "obd.html"), encoding="utf-8").read()
    pruefe('id="pruefen"' in obd_html and "werteRuefen" in obd_js,
           "die Diagnoseseite kann alle Werte im Stand prüfen")
    pruefe("BEREICHE" in obd_js and "Kreuzvergleich" in obd_js,
           "gegen Bereiche und über einen Kreuzvergleich - der prüft zwei "
           "Formeln auf einmal, ohne dass gefahren werden muss")
    pruefe('id="klima-a"' in obd_html and 'id="klima-b"' in obd_html
           and "klimaZeigen" in obd_js,
           "und der Klimakompressor über eine Differenzmessung statt über "
           "eine geratene Formel")
    pruefe("nutzbytes," in kern,
           "dafür gibt der Baustein die rohen Nutzbytes heraus")

    pruefe("kompressor_w" in kern and "b[5] * 256) + b[6]" in kern,
           "die Kompressorleistung steht drin - aus einer Differenzmessung "
           "abgeleitet, weil keine der drei Quellen eine Formel nennt")
    pruefe("i += 2" in obd_js,
           "die Differenzanzeige richtet die Byte-Paare aus, statt ein "
           "Fenster byteweise zu schieben - eine Mehrbyte-Zahl fängt nicht "
           "an jedem Byte an")

    pruefe("roh - 4294967296" in kern,
           "der Entladezähler wird vorzeichenbehaftet gelesen - unsigned "
           "ergab am Fahrzeug 482 961 statt 17 439 kWh")
    pruefe("entladen_kwh: [100, 100000" in obd_js,
           "und seine Plausibilitätsschranke fängt genau diesen Fehler - "
           "die alte [1, 999999] liess ihn durch")
    pruefe("if (drin) gut += 1; else schlecht += 1;" in obd_js,
           "der Kreuzvergleich zählt in die Zusammenfassung - rot in der "
           "Tabelle und \"0 auffällig\" darüber ist schlimmer als nichts")

    pruefe("entladen_kwh" in kern and "8583.07" in kern,
           "die Energiezähler des Fahrzeugs werden gelesen - ihre Differenz "
           "ist die verbrauchte Energie, 0,117 Wh statt 339 Wh Auflösung")
    pruefe("weitere:" in kern and "Object.assign(roh, wert.weitere)" in kern,
           "und Lade- wie Entladezähler kommen aus **einer** Abfrage - eine "
           "Mehrrahmen-Antwort zweimal zu holen kostet Zeit")
    pruefe("ZUSATZ_TITEL" in kern,
           "auch der mitgelieferte Wert steht in der Feldliste, sonst zeigt "
           "die Tabelle weniger, als gemessen wird")
    pruefe("ABSCHNITT_MIT_ZAEHLER_S = 60" in live
           and "ABSCHNITT_AUS_SOC_S = 300" in live,
           "die Balkenbreite folgt der Quelle: eine Minute mit Zähler, "
           "fünf ohne - nicht dem Wunsch")
    pruefe("letzt.netto - erst.netto" in live,
           "und die Balken rechnen mit der Zählerdifferenz, wenn es sie gibt")
    pruefe("letzt.gps - erst.gps" in live,
           "die Strecke je Balken kommt dagegen aus dem GPS - der "
           "Kilometerstand löst in ganzen Kilometern auf, und eine Minute "
           "sind rund 1,2 km")
    # Die Rohwerte gehoeren hinter eine Klappe: siebzehn Zeilen mitten im
    # Fahrbild sind Laerm. Und die Kacheln, die man liest, gehoeren ueber
    # die Diagramme, nicht darunter.
    pruefe('<details id="live-roh"' in html,
           "die Rohwerte stehen hinter einer Klappe, nicht im Fahrbild")
    pruefe(html.index('id="live-werte"') < html.index('id="live-verlauf"'),
           "und die Kacheln über den Diagrammen - was man im Fahren liest, "
           "steht oben")
    pruefe('id="live-auto-stand"' in html.split("<summary>")[1].split("</summary>")[0],
           "das Alter steht in der zugeklappten Zeile - man soll ohne "
           "Aufklappen sehen, ob es lebt")

    pruefe('fillText("kWh/100"' in live,
           "der Balkenplot hat eine beschriftete Achse - ohne sie sieht man "
           "Unterschiede, aber keine Grössenordnung")
    html_obd = open(os.path.join(FRONTEND, "obd.js"), encoding="utf-8").read()
    pruefe("knopf.disabled = true" in html_obd and "läuft …" in html_obd,
           "der Senden-Knopf sperrt sich, solange eine Befehlsreihe läuft - "
           "sonst fällt ein zweiter Start dem ersten in den Rücken")

    pruefe("laufenderVerbrauch" in live and "VERBRAUCH_AB_KM" in live,
           "der Verbrauch der laufenden Fahrt wird aus Ladestand und "
           "Kilometerstand gerechnet, erst ab einer Mindeststrecke")
    pruefe("aufzFahrzeug" in live,
           "und kennt dafür das Fahrzeug der Aufzeichnung - ohne Akkugrösse "
           "wird aus einem Ladestand keine Kilowattstunde")

    pruefe("_leer" in kern and "roh._leer = roh._leer" in kern,
           "ein Messwert, der antwortet aber nichts liefert, wird vermerkt - "
           "vorher fiel er stumm durch, und vier von dreizehn Werten fehlten "
           "eine ganze Fahrt lang ohne Spur")
    pruefe("werteStand" in live and "nieGekommen" in live,
           "das Dashboard hält den letzten bekannten Wert je Messgrösse fest, "
           "statt die Zeile leer zu lassen")
    pruefe("alterText" in live and 'class="wann"' in live,
           "und schreibt sein Alter daneben - ein alter Wert ist nützlich, "
           "solange man ihm ansieht, dass er alt ist")
    pruefe("letzteRohwerteZeit" in live,
           "das Dashboard zeigt, wie alt der letzte Satz aus dem Auto ist - "
           "eine eingefrorene Anzeige sieht sonst aus wie eine laufende")

    # Die Betriebsskripte müssen auch im Container laufen. Dort liegt das
    # Paket als /srv/app neben /srv/tools, lokal dagegen unter backend/app -
    # wer nur ein Layout kennt, scheitert im jeweils anderen mit
    # `ModuleNotFoundError: No module named 'app'`. Genau das war der Fall:
    # `pruefen.py` hing auf `../backend` fest, und damit lief per
    # `docker exec` kein einziges Prüfskript - der Weg, für den `tools/`
    # überhaupt ins Image aufgenommen wurde.
    werkzeuge = sorted(pfad for pfad in os.listdir(WERKZEUGE)
                       if pfad.endswith(".py"))
    ohne_beide = []
    for name in werkzeuge:
        quelle = open(os.path.join(WERKZEUGE, name), encoding="utf-8").read()
        if "app" not in quelle:
            continue
        # Entweder das Skript kennt beide Layouts selbst, oder es überlässt
        # das `pruefen.anwendung_bereitstellen`.
        if 'os.path.join(_HIER, "..")' in quelle \
                or 'os.path.join(HIER, "..")' in quelle \
                or "anwendung_bereitstellen" in quelle \
                or name == "pruefen.py":
            continue
        ohne_beide.append(name)
    pruefe(not ohne_beide,
           "jedes Werkzeug in tools/ findet das Paket in beiden Layouten - "
           "im Repo unter backend/app, im Image daneben als app",
           str(ohne_beide))
    pruefe('os.path.join(hier, "..")' in
           open(os.path.join(WERKZEUGE, "pruefen.py"), encoding="utf-8").read(),
           "und pruefen.py selbst auch - sonst läuft per docker exec kein "
           "einziges Prüfskript")
    pruefe("Content-Security-Policy" in seite.headers,
           "die Security-Header sitzen")

    return pruefe.bilanz()


if __name__ == "__main__":
    sys.exit(main())
