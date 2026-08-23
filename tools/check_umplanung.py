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

    ./tools/check_umplanung.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HIER, "..", "backend"))

# Vor jedem App-Import setzen: Die Engine wird beim Import gebaut.
_DB = os.path.join(tempfile.mkdtemp(prefix="jolt-umplan-"), "check.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.pop("ORS_API_KEY", None)
os.environ.pop("APP_PASSWORT", None)

from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.energie.modell import haversine_m  # noqa: E402
from app.laden import verfuegbarkeit  # noqa: E402
from app.live import sitzung as live_sitzung  # noqa: E402
from app.live import umplanung  # noqa: E402
from app.main import app  # noqa: E402

FEHLER: list[str] = []


def pruefe(bedingung, text: str, zusatz: str = "") -> None:
    if bedingung:
        print(f"  ok    {text}")
    else:
        print(f"  FEHLT {text}   {zusatz}")
        FEHLER.append(text)


class Fahrzeugstub:
    reserve_soc = 10.0


def ausloeser(**abweichungen):
    """`_neuplanung_pruefen` mit lauter unauffälligen Vorgaben aufrufen.

    So steht in jedem Testfall nur die eine Grösse, um die es geht - und man
    sieht sofort, welche den Auslöser bewegt hat.
    """
    werte = dict(fahrzeug=Fahrzeugstub(), abweichung=0.0, abstand_m=10.0,
                 abweg_seit=None, jetzt=datetime(2026, 1, 1, 12, 0),
                 prognose=50.0, reserve_bei=None, gesamt_km=600.0,
                 verschiebung=0.0, naechster=None, ankunft_soc=None)
    werte.update(abweichungen)
    return live_sitzung._neuplanung_pruefen(**werte)


# ---------------------------------------------------------------------------
# Teil 1: die Auslöser
# ---------------------------------------------------------------------------

def teil_ausloeser():
    print("\nAuslöser einzeln (ohne Datenbank)")

    noetig, grund, _ = ausloeser()
    pruefe(not noetig, "ohne Abweichung wird nicht neu geplant", grund)

    noetig, grund, dringend = ausloeser(reserve_bei=420.0)
    pruefe(noetig and dringend,
           "Reserve vor dem Ziel: sofort neu planen", grund)

    noetig, grund, dringend = ausloeser(prognose=4.0)
    pruefe(noetig and dringend,
           "Prognose unter der Reserve: sofort neu planen", grund)

    # Der nächste Stopp ist als belegt gemeldet.
    stopp = {"id": 4242, "name": "Rasthof Nord", "km_auf_route": 200.0,
             "geplant_soc": 15.0, "erwartet_soc": 15.0}
    verfuegbarkeit.MELDUNGEN.melden(4242)
    try:
        noetig, grund, dringend = ausloeser(naechster=stopp, ankunft_soc=15.0)
        pruefe(noetig and dringend,
               "belegt gemeldeter nächster Stopp: sofort neu planen", grund)
        pruefe("belegt" in grund, "und der Grund benennt es", grund)
    finally:
        verfuegbarkeit.MELDUNGEN.freigeben(4242)

    noetig, grund, _ = ausloeser(naechster=stopp, ankunft_soc=15.0)
    pruefe(not noetig,
           "nach der Freigabe greift derselbe Stopp nicht mehr", grund)

    # Ankunfts-SoC am nächsten Stopp: die Schwelle sind 5 Prozentpunkte.
    noetig, grund, _ = ausloeser(naechster=stopp, ankunft_soc=9.0)
    pruefe(noetig, "6 pp weniger am nächsten Stopp: neu planen", grund)
    noetig, grund, _ = ausloeser(naechster=stopp, ankunft_soc=12.0)
    pruefe(not noetig, "3 pp weniger bleiben unter der Schwelle", grund)

    # Abweg braucht Dauer, nicht nur Abstand.
    jetzt = datetime(2026, 1, 1, 12, 0)
    noetig, grund, _ = ausloeser(abstand_m=900.0, abweg_seit=jetzt, jetzt=jetzt)
    pruefe(not noetig,
           "ein einzelner Ausreisser neben der Route löst nichts aus", grund)
    noetig, grund, dringend = ausloeser(
        abstand_m=900.0, abweg_seit=jetzt, jetzt=jetzt + timedelta(seconds=90))
    pruefe(noetig and dringend,
           "90 Sekunden neben der Route dagegen schon", grund)

    # Stau.
    noetig, grund, _ = ausloeser(verschiebung=14.0)
    pruefe(noetig, "14 min spätere Ankunft: neu planen", grund)
    noetig, grund, _ = ausloeser(verschiebung=6.0)
    pruefe(not noetig, "6 min bleiben unter der Schwelle", grund)

    # Ohne Plan bleibt die Abweichung hier die beste verfügbare Aussage.
    noetig, grund, _ = ausloeser(abweichung=-7.0)
    pruefe(noetig, "ohne Plan zählt die Abweichung an der aktuellen Position",
           grund)


# ---------------------------------------------------------------------------
# Teil 2: Rest-Strecke
# ---------------------------------------------------------------------------

def teil_reststrecke():
    print("\nReststrecke abschneiden und skalieren")

    profil = [{"km": k, "kwh": k * 0.18, "minuten": k * 0.5, "soc": 80 - k * 0.1}
              for k in range(0, 301, 10)]
    rest = umplanung.restprofil(profil, 100.0)
    pruefe(rest.km[0] == 0.0, "die Reststrecke beginnt bei km 0",
           f"{rest.km[0]}")
    pruefe(abs(rest.km[-1] - 200.0) < 0.01, "und endet 200 km später",
           f"{rest.km[-1]}")
    pruefe(abs(rest.kwh[0]) < 1e-9 and abs(rest.minuten[0]) < 1e-9,
           "Energie und Zeit starten ebenfalls bei null")

    skaliert = umplanung.restprofil(profil, 100.0, verbrauchsfaktor=1.25,
                                    zeitfaktor=1.4)
    pruefe(abs(skaliert.kwh[-1] - rest.kwh[-1] * 1.25) < 1e-6,
           "der Verbrauchsfaktor skaliert die Energie")
    pruefe(abs(skaliert.minuten[-1] - rest.minuten[-1] * 1.4) < 1e-6,
           "der Zeitfaktor skaliert die Zeit")
    pruefe(skaliert.km[-1] == rest.km[-1],
           "die Strecke bleibt, was sie ist - gefahren wird nicht weniger")

    # Geometrie und Profil müssen denselben Nullpunkt bekommen, sonst ist
    # jede Etappenrechnung um diesen Versatz falsch.
    geo = [[9.0 + i * 0.01, 53.0, 0.0] for i in range(200)]
    teilstueck, km0 = umplanung.rest_ab(geo, 40.0)
    gemessen = 0.0
    for i in range(1, len(geo)):
        gemessen += haversine_m(geo[i - 1][1], geo[i - 1][0],
                                geo[i][1], geo[i][0]) / 1000.0
        if geo[i] == teilstueck[1]:
            break
    pruefe(km0 <= 40.0, "der Schnittpunkt liegt vor dem gesuchten Kilometer",
           f"km0={km0:.1f}")
    pruefe(len(teilstueck) > 2 and teilstueck[0] in geo,
           "und die Restgeometrie ist ein echtes Teilstück der Route")


def teil_planvergleich():
    print("\nWann ist ein Plan ein anderer Plan?")

    def plan(*stopps):
        return {"machbar": True,
                "stopps": [{"id": i, "abfahrt_soc": s, "name": f"LP{i}",
                            "km_auf_route": 100.0 * i} for i, s in stopps]}

    a = plan((1, 50.0), (2, 60.0))
    pruefe(umplanung.stopps_gleich(a, plan((1, 50.0), (2, 60.0))),
           "derselbe Plan ist derselbe Plan")
    pruefe(umplanung.stopps_gleich(a, plan((1, 51.5), (2, 60.0))),
           "anderthalb Prozentpunkte mehr Ladung sind keine Änderung - "
           "darüber will am Steuer niemand unterrichtet werden")
    pruefe(not umplanung.stopps_gleich(a, plan((1, 58.0), (2, 60.0))),
           "acht Prozentpunkte dagegen schon")
    pruefe(not umplanung.stopps_gleich(a, plan((3, 50.0), (2, 60.0))),
           "ein anderer Standort ist immer eine Änderung")
    pruefe(not umplanung.stopps_gleich(a, plan((1, 50.0))),
           "ein Stopp weniger auch")
    pruefe(not umplanung.stopps_gleich(a, {"machbar": False, "stopps": []}),
           "und ein Plan, der nicht mehr aufgeht, erst recht")

    text = umplanung.aenderung_beschreiben(a, plan((1, 50.0)))
    pruefe("1 Ladestopps statt 2" in text or "weniger" in text,
           "die Änderung wird in einem Satz beschrieben", text)


# ---------------------------------------------------------------------------
# Teil 3: die ganze Kette
# ---------------------------------------------------------------------------

def fahrt_vorbereiten(client) -> dict:
    """Eine Fahrt rechnen und Ladepunkte entlang der Route anlegen."""
    fahrzeuge = client.get("/api/fahrzeuge").json()
    route = client.post("/api/route", json={
        "fahrzeug_id": fahrzeuge[0]["id"],
        "start": {"lat": 53.5511, "lon": 9.9937, "text": "Hamburg"},
        "ziel": {"lat": 48.1351, "lon": 11.5820, "text": "München"},
        "start_soc": 80.0}).json()

    geo = route["geometrie"]
    db = SessionLocal()
    try:
        db.query(models.Ladepunkt).delete()
        km, naechster, i = 0.0, 30.0, 0
        for n in range(1, len(geo)):
            km += haversine_m(geo[n - 1][1], geo[n - 1][0],
                              geo[n][1], geo[n][0]) / 1000.0
            if km < naechster:
                continue
            db.add(models.Ladepunkt(
                quelle="ocm", fremd_id=f"pruef-{i}", name=f"Lader km {km:.0f}",
                betreiber="Prüfbetrieb", lat=geo[n][1] + 0.0036, lon=geo[n][0],
                ort=f"Ort {i}", land="DE", anschluesse=[],
                max_kw=150.0 if i % 2 else 300.0, anzahl_punkte=4 + (i % 5),
                steckertypen="CCS"))
            i += 1
            naechster = km + 30.0
        db.commit()
    finally:
        db.close()
    return route


def teil_kette():
    client = TestClient(app)
    route = fahrt_vorbereiten(client)
    fahrt_id = route["fahrt_id"]

    print("\nLive-Sitzung mit Startplan")
    start = client.post(f"/api/live/start/{fahrt_id}",
                        params={"min_kw": 100, "radius_km": 10}).json()
    sitzung_id = start["sitzung_id"]
    startplan = start.get("plan")
    pruefe(startplan is not None and startplan.get("machbar"),
           "beim Start wird sofort ein Ladeplan gerechnet",
           (startplan or {}).get("grund", "kein Plan"))
    pruefe(len(startplan.get("stopps") or []) >= 1,
           "und er enthält Ladestopps",
           f"{len(startplan.get('stopps') or [])}")

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
    _abspielen(client, sitzung_id, fahrt_id, mehrverbrauch=1.3, zeitfaktor=1.0,
               bis_km=100.0)

    zustand = client.get(f"/api/live/{sitzung_id}").json()
    plan = zustand.get("plan")
    pruefe(zustand["verbrauchsfaktor"] > 1.15,
           "der Mehrverbrauch wird als Faktor erkannt",
           f"×{zustand['verbrauchsfaktor']}")
    pruefe(plan is not None, "es liegt ein Plan vor")
    pruefe((plan or {}).get("stand_km", 0) > 0,
           "und er wurde unterwegs neu gerechnet, nicht beim Start",
           f"stand_km={(plan or {}).get('stand_km')}")
    _plan_pruefen(plan, route, "Umplanung")
    pruefe(plan.get("stopps") and plan["stopps"][0]["ankunft_soc"]
           >= route["fahrzeug"]["reserve_soc"] - 0.5,
           "der neue erste Stopp wird noch über der Reserve erreicht",
           str([s["ankunft_soc"] for s in plan.get("stopps") or []]))
    pruefe(not umplanung.stopps_gleich(startplan, plan),
           "und der Plan ist ein anderer als der beim Losfahren - genau "
           "dafür gibt es die Nachführung",
           f"vorher {[s['id'] for s in startplan['stopps']]}, "
           f"jetzt {[s['id'] for s in plan['stopps']]}")

    print("\nWeitergefahren, bis nichts mehr geht")
    _abspielen(client, sitzung_id, fahrt_id, mehrverbrauch=1.3, zeitfaktor=1.0)
    leer = client.get(f"/api/live/{sitzung_id}").json().get("plan") or {}
    pruefe(leer.get("machbar") is False,
           "mit leerem Akku gibt es keinen Plan mehr - und jolt behauptet "
           "auch keinen", str(leer.get("grund"))[:80])
    pruefe(bool(leer.get("grund")),
           "der Grund steht dabei", str(leer.get("grund"))[:80])

    client.post(f"/api/live/{sitzung_id}/ende")

    print("\nStau verschiebt die Ankunft, ohne den Verbrauch zu verbiegen")
    start2 = client.post(f"/api/live/start/{fahrt_id}",
                         params={"min_kw": 100, "radius_km": 10}).json()
    sitzung2 = start2["sitzung_id"]
    letzter = _abspielen(client, sitzung2, fahrt_id, mehrverbrauch=1.0,
                         zeitfaktor=1.5, bis_km=200.0)
    pruefe(abs(letzter["verbrauchsfaktor"] - 1.0) < 0.08,
           "der Verbrauchsfaktor bleibt bei rund 1 - es wird ja nicht mehr "
           "verbraucht, nur langsamer gefahren",
           f"×{letzter['verbrauchsfaktor']}")
    pruefe(letzter["zeitfaktor"] > 1.3,
           "der Zeitfaktor erkennt den Stau", f"×{letzter['zeitfaktor']}")
    pruefe(letzter["ankunft_verschiebung_min"] is not None
           and letzter["ankunft_verschiebung_min"] > 10,
           "und die Ankunft verschiebt sich deutlich",
           f"{letzter.get('ankunft_verschiebung_min')} min")
    client.post(f"/api/live/{sitzung2}/ende")

    print("\nEine Belegt-Meldung wirft den Stopp aus dem Plan")
    start3 = client.post(f"/api/live/start/{fahrt_id}",
                         params={"min_kw": 100, "radius_km": 10}).json()
    sitzung3 = start3["sitzung_id"]
    vorher = start3["plan"]["stopps"][0]
    _abspielen(client, sitzung3, fahrt_id, mehrverbrauch=1.0, zeitfaktor=1.0,
               bis_km=20.0)
    client.post(f"/api/saeulen/{vorher['id']}/belegt")
    nachher = _messen(client, sitzung3, fahrt_id, km=25.0, mehrverbrauch=1.0,
                      zeitfaktor=1.0)
    plan3 = client.get(f"/api/live/{sitzung3}").json().get("plan") or {}
    ids = [s["id"] for s in plan3.get("stopps") or []]
    pruefe(vorher["id"] not in ids,
           "der belegte Stopp steht nicht mehr im Plan",
           f"geplant war {vorher['id']}, jetzt {ids}")
    pruefe(nachher.get("plan_geaendert") is True,
           "und die Änderung wird als Änderung gemeldet",
           nachher.get("aenderung", ""))
    client.delete(f"/api/saeulen/{vorher['id']}/belegt")
    client.post(f"/api/live/{sitzung3}/ende")

    print("\nDer Plan ändert sich nicht bei jeder Messung")
    start4 = client.post(f"/api/live/start/{fahrt_id}",
                         params={"min_kw": 100, "radius_km": 10}).json()
    sitzung4 = start4["sitzung_id"]
    aenderungen = 0
    messungen = 0
    for km in range(5, 300, 5):
        antwort = _messen(client, sitzung4, fahrt_id, km=float(km),
                          mehrverbrauch=1.3, zeitfaktor=1.0)
        messungen += 1
        if antwort.get("plan_geaendert"):
            aenderungen += 1
    pruefe(aenderungen <= messungen / 3,
           "über 300 km wird nicht bei jeder Messung umgeplant",
           f"{aenderungen} Änderungen bei {messungen} Messungen")
    pruefe(aenderungen >= 1,
           "aber mindestens einmal - sonst wäre die Sperre eine Blockade",
           f"{aenderungen} Änderungen")
    client.post(f"/api/live/{sitzung4}/ende")


def _profil_bei(profil, km):
    for vorher, nachher in zip(profil, profil[1:]):
        if (vorher.get("km") or 0) <= km <= (nachher.get("km") or 0):
            return nachher
    return profil[-1] if profil else {}


def _messen(client, sitzung_id, fahrt_id, km, mehrverbrauch, zeitfaktor):
    """Einen einzelnen Messpunkt bei Kilometer `km` melden."""
    fahrt = client.get(f"/api/fahrten/{fahrt_id}").json()
    profil = fahrt["profil"]
    eintrag = _profil_bei(profil, km)
    verbraucht = fahrt["start_soc"] - (eintrag.get("soc") or fahrt["start_soc"])
    soc = max(0.0, fahrt["start_soc"] - verbraucht * mehrverbrauch)
    return client.post(f"/api/live/{sitzung_id}/punkt", json={
        "lat": eintrag["lat"], "lon": eintrag["lon"], "soc": round(soc, 2)}).json()


def _abspielen(client, sitzung_id, fahrt_id, mehrverbrauch, zeitfaktor,
               bis_km=None, schritt_km=10.0):
    """Die Fahrt von Hand abspielen - deterministisch und ohne Warten.

    Der eingebaute Simulator läuft asynchron; für ein Prüfskript ist eine
    Schleife, deren Ende feststeht, die bessere Wahl.
    """
    db = SessionLocal()
    try:
        sitzung = db.get(models.LiveSitzung, sitzung_id)
        fahrt = sitzung.fahrt
        profil = fahrt.energieprofil or []
        gesamt = profil[-1]["km"] if profil else 0.0
        ende = min(gesamt, bis_km) if bis_km else gesamt
        beginn = datetime(2026, 1, 1, 8, 0)

        letzter = None
        km = 0.0
        while km <= ende:
            eintrag = _profil_bei(profil, km)
            verbraucht = fahrt.start_soc - (eintrag.get("soc") or fahrt.start_soc)
            soc = max(0.0, fahrt.start_soc - verbraucht * mehrverbrauch)
            zustand = live_sitzung.messpunkt_aufnehmen(
                db, sitzung, eintrag["lat"], eintrag["lon"], round(soc, 2),
                zeit=beginn + timedelta(
                    minutes=(eintrag.get("minuten") or 0.0) * zeitfaktor))
            letzter = live_sitzung.zustand_als_dict(zustand)
            if soc <= 0:
                break
            km += schritt_km
        return letzter
    finally:
        db.close()


def _plan_pruefen(plan, route, name):
    if not plan or not plan.get("machbar"):
        pruefe(False, f"{name}: es gibt einen gültigen Plan",
               (plan or {}).get("grund", "kein Plan"))
        return
    stopps = plan.get("stopps") or []
    km = [s["km_auf_route"] for s in stopps]
    pruefe(km == sorted(km), f"{name}: die Stopps stehen in Fahrtreihenfolge",
           str(km))
    pruefe(all(s["km_auf_route"] >= plan["stand_km"] - 1 for s in stopps),
           f"{name}: kein Stopp liegt hinter dem Fahrzeug",
           f"stand km {plan['stand_km']}, Stopps {km}")
    pruefe(all(s["km_auf_route"] <= route["strecke_km"] + 1 for s in stopps),
           f"{name}: und keiner hinter dem Ziel", str(km))
    pruefe(all(s["abfahrt_soc"] > s["ankunft_soc"] for s in stopps),
           f"{name}: an jedem Stopp wird tatsächlich geladen")


def main() -> int:
    teil_ausloeser()
    teil_reststrecke()
    teil_planvergleich()
    teil_kette()

    print()
    if FEHLER:
        print(f"{len(FEHLER)} Prüfung(en) fehlgeschlagen:")
        for text in FEHLER:
            print(f"  - {text}")
        return 1
    print("Alle Prüfungen bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
