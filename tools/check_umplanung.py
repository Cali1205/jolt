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
from types import SimpleNamespace
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pruefen import Pruefung, anwendung_bereitstellen  # noqa: E402

anwendung_bereitstellen("umplan")

from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.energie.modell import (Fahrzeugwerte, Umgebung,  # noqa: E402
                                haversine_m)
from app.energie.profil import eintrag_bei as _profil_bei  # noqa: E402
from app.laden import verfuegbarkeit  # noqa: E402
from app.live import sitzung as live_sitzung  # noqa: E402
from app.live import umplanung  # noqa: E402
from app.main import app  # noqa: E402

pruefe = Pruefung()


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


class Punktstub:
    """Ein Messpunkt, so viel davon wie `_ladepausen_minuten` anfasst."""

    NULL = datetime(2026, 1, 1, 8, 0)

    def __init__(self, km: float, soc: float, minuten: float):
        self.km_auf_route = km
        self.soc = soc
        self.zeit = self.NULL + timedelta(minutes=minuten)


def teil_ladepausen():
    """Was von der verstrichenen Zeit eine Ladepause war - und was nicht.

    Das Energieprofil führt ausschliesslich Fahrzeit; die Ladezeit steht im
    Plan. Wer die Wanduhr ungefiltert dagegen hält, hat nach dem ersten
    Ladestopp eine Verspätung in Höhe der Ladedauer - dauerhaft, denn
    aufgeholt wird sie nie.
    """
    print("\nLadepause von Verspätung unterscheiden")

    # 100 km/h, 0,1 Prozentpunkte je Kilometer.
    profil = [{"km": k, "minuten": k * 0.6, "soc": 80 - k * 0.1}
              for k in range(0, 401, 10)]

    # Der Logger sendet während des Ladens weiter: dreissig Minuten am selben
    # Ort, der Ladestand steigt.
    laden = [Punktstub(150.0, 30.0, 90.0), Punktstub(150.0, 45.0, 100.0),
             Punktstub(150.0, 60.0, 110.0), Punktstub(150.0, 70.0, 120.0),
             Punktstub(160.0, 68.0, 126.0)]
    gemessen = live_sitzung._ladepausen_minuten(laden, profil)
    pruefe(abs(gemessen - 30.0) < 0.1,
           "dreissig Minuten an der Säule werden als Ladepause erkannt",
           f"{gemessen:.1f} min")

    # Derselbe Ladestopp, aber der Logger hat geschlafen und meldet sich erst
    # zwanzig Kilometer später wieder. Auch dann darf nur die Standzeit
    # zählen, nicht die Fahrzeit für die zwanzig Kilometer.
    geschlafen = [Punktstub(150.0, 30.0, 90.0), Punktstub(170.0, 65.0, 132.0)]
    gemessen = live_sitzung._ladepausen_minuten(geschlafen, profil)
    pruefe(abs(gemessen - 30.0) < 0.1,
           "auch wenn der Logger die Pause verschlafen hat",
           f"{gemessen:.1f} min")

    # Rekuperation auf langer Talfahrt hebt den Ladestand ebenfalls - kostet
    # aber keine zusätzliche Zeit, also auch keine Gutschrift.
    bergab = [Punktstub(150.0, 30.0, 90.0), Punktstub(160.0, 31.0, 96.0)]
    gemessen = live_sitzung._ladepausen_minuten(bergab, profil)
    pruefe(gemessen < 0.5,
           "Rekuperation bergab ist keine Ladepause - das Auto fährt ja",
           f"{gemessen:.1f} min")

    # Mittagessen: eine Dreiviertelstunde Stillstand ohne Ladung. Die
    # verschiebt die Ankunft wirklich und muss stehen bleiben.
    pause = [Punktstub(150.0, 30.0, 90.0), Punktstub(150.0, 29.8, 135.0)]
    gemessen = live_sitzung._ladepausen_minuten(pause, profil)
    pruefe(gemessen < 0.5,
           "eine Pause ohne Ladung bleibt Verspätung - Mittagessen verschiebt "
           "die Ankunft wirklich", f"{gemessen:.1f} min")


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


class FahrzeugStub:
    masse_kg = 2500.0
    c_w = 0.29
    stirnflaeche_m2 = 2.9
    c_rr = 0.010
    eta_antrieb = 0.88
    eta_rekup = 0.70
    p_neben_w = 350.0
    waermepumpe = True
    akku_netto_kwh = 77.0
    reserve_soc = 10.0
    korrekturfaktor = 1.0


class FahrtStub:
    aussentemp_c = 10.0
    fahrzeug = FahrzeugStub()


def teil_tempo_neurechnen():
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
                     "hoehe": 100.0, "tempo_kmh": 100.0,
                     "minuten": km * 0.6, "soc": 80 - km * 0.1,
                     "kwh": km * 0.18})
    umgebung = lambda lat, lon: Umgebung(temp_c=10.0)      # noqa: E731
    fahrt = FahrtStub()

    nach_plan = umplanung.restprofil_physik(fahrt, rest, 1.0, umgebung)
    schnell = umplanung.restprofil_physik(fahrt, rest, 1.2, umgebung)
    langsam = umplanung.restprofil_physik(fahrt, rest, 0.8, umgebung)

    pruefe(nach_plan is not None and len(nach_plan.km) > 30,
           "das gespeicherte Profil reicht zum Neurechnen aus - Position, "
           "Höhe und Tempo je Stützstelle stehen darin")
    pruefe(abs(nach_plan.km[-1] - 200.0) < 3.0,
           "und die Strecke kommt dabei heraus, die hineinging",
           f"{nach_plan.km[-1]:.1f} km")

    pruefe(schnell.kwh[-1] > nach_plan.kwh[-1],
           "zwanzig Prozent schneller kostet mehr Energie",
           f"{schnell.kwh[-1]:.1f} gegen {nach_plan.kwh[-1]:.1f} kWh")
    pruefe(schnell.minuten[-1] < nach_plan.minuten[-1],
           "und weniger Zeit - beides zugleich, das kann kein Energiefaktor",
           f"{schnell.minuten[-1]:.0f} gegen {nach_plan.minuten[-1]:.0f} min")

    # Die Schranke nach oben ist der reine v²-Anteil. Läge der Zuwachs
    # darüber, wäre mehr als der Luftwiderstand skaliert worden; läge er bei
    # null, wäre das Tempo gar nicht angekommen.
    zuwachs = schnell.kwh[-1] / nach_plan.kwh[-1]
    pruefe(1.02 < zuwachs < 1.44,
           "der Mehrverbrauch liegt zwischen spürbar und dem reinen "
           "v²-Faktor - Rollwiderstand und Nebenverbraucher skalieren nicht "
           "mit dem Quadrat", f"×{zuwachs:.3f}")

    pruefe(langsam.kwh[-1] < nach_plan.kwh[-1]
           and langsam.minuten[-1] > nach_plan.minuten[-1],
           "langsamer fahren dreht beides um",
           f"{langsam.kwh[-1]:.1f} kWh in {langsam.minuten[-1]:.0f} min")

    # Fahrten aus der Zeit vor diesen Profilfeldern müssen aufs Skalieren
    # zurückfallen und nicht abstürzen.
    ohne_ort = [{k: v for k, v in e.items() if k not in ("lat", "lon")}
                for e in rest]
    pruefe(umplanung.restprofil_physik(fahrt, ohne_ort, 1.2, umgebung) is None,
           "ohne Position im Profil wird nicht gerechnet, sondern None "
           "gemeldet - der Aufrufer skaliert dann wie bisher")
    pruefe(umplanung.restprofil_physik(fahrt, rest[:1], 1.2, umgebung) is None,
           "und ein Profil mit einem einzigen Punkt ebenso")


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
        # Der Demo-Adapter kennt keine drei unterschiedlichen Vorgaben - hier
        # reicht deshalb die erste (einzige) Variante.
        "start_soc": 80.0}).json()["varianten"][0]

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
    _abspielen(client, sitzung3, fahrt_id, mehrverbrauch=1.0, zeitfaktor=1.0,
               bis_km=20.0)

    # Gemeldet wird der Stopp, der **jetzt** gilt - nicht der aus dem
    # Startplan. Bis km 20 ist meist schon einmal umgeplant, und dann steht
    # dort ein anderer. Vorher stand hier `start3["plan"]["stopps"][0]`, und
    # der Fall prüfte unbemerkt nichts mehr: Der gemeldete Stopp war gar
    # nicht der nächste, der Auslöser griff zu Recht nicht, und die Prüfung
    # "der belegte Stopp steht nicht mehr im Plan" bestand aus dem falschen
    # Grund - er fehlte, weil längst umgeplant war.
    laufend = client.get(f"/api/live/{sitzung3}").json().get("plan") or {}
    vorher = (laufend.get("stopps") or [None])[0]
    pruefe(vorher is not None,
           "vor der Meldung steht ein nächster Stopp im laufenden Plan",
           str(laufend.get("stopps")))
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


def _fahrt_anlegen(client, fahrzeug, name, minuten_her, laedt=False):
    """Eine Aufzeichnung mit Messpunkten, deren letzter `minuten_her` alt ist.

    Achtung beim Lesen: `/api/live/aufzeichnung` beendet beim Start **alle**
    anderen laufenden Sitzungen. Zwei Sitzungen nebeneinander aufzubauen geht
    deshalb nicht - jeder Fall wird einzeln geprüft.
    """
    antwort = client.post("/api/live/aufzeichnung", json={
        "fahrzeug_id": fahrzeug["id"], "lat": 48.0, "lon": 11.0,
        "soc": 90.0, "name": name}).json()
    db = SessionLocal()
    try:
        sitzung = db.get(models.LiveSitzung, antwort["sitzung_id"])
        beginn = datetime.utcnow() - timedelta(minutes=minuten_her + 40)
        for i in range(41):
            # Beim Laden steht das Auto und der Ladestand steigt; sonst fährt
            # es und der Ladestand fällt.
            soc = (65.0 + i * 0.6) if laedt else (90.0 - i * 0.25)
            live_sitzung.messpunkt_aufnehmen(
                db, sitzung, 48.0 + (0.0 if laedt else i * 0.009), 11.0,
                soc=soc, aussentemp_c=12.0, zeit=beginn + timedelta(minutes=i))
        return antwort["sitzung_id"], aufraeumen.verwaiste_beenden(db)
    finally:
        db.close()


def teil_verwaiste_fahrt():
    """Vergessene Fahrten beendet jolt selbst - aber nicht die Ladepause.

    Für eine geplante Fahrt ist das Vergessen halb so schlimm: Die Messpunkte
    liegen in der Datenbank. Für eine **Aufzeichnung** ist es der
    Totalverlust - Strecke und Energieprofil entstehen erst beim Beenden aus
    den Messpunkten.

    Der teurere Fehler ist aber der umgekehrte: eine Fahrt abschneiden, die
    nur gerade lädt. Die zweite Hälfte wäre unwiederbringlich weg.
    """
    from app.live import aufraeumen as _a
    globals()["aufraeumen"] = _a

    client = TestClient(app)
    print("\nVergessene Fahrt selbst beenden")
    fahrzeug = client.get("/api/fahrzeuge").json()[0]

    # 1. Seit Stunden still, zuletzt gefahren - die wird beendet.
    still_id, beendet = _fahrt_anlegen(client, fahrzeug, "Vergessen", 200)
    treffer = next((b for b in beendet if b["sitzung_id"] == still_id), None)
    pruefe(treffer is not None,
           "eine Fahrt, die seit Stunden schweigt, wird beendet",
           str([b["sitzung_id"] for b in beendet]))
    if treffer:
        gebaut = treffer.get("aufzeichnung") or {}
        pruefe(gebaut.get("ok") is True,
               "und dabei entsteht die Strecke aus den Messpunkten - genau "
               "das, was beim Vergessen sonst verloren geht",
               str(gebaut.get("grund")))
        pruefe(treffer.get("gelernt") is not None,
               "auch gelernt wird - eine vergessene Fahrt ist keine "
               "schlechtere Messung als eine ordentlich beendete")
    pruefe(client.get(f"/api/live/{still_id}").json()["laeuft"] is False,
           "die Sitzung ist danach beendet")

    # 2. Genauso lange still, aber zuletzt wurde geladen. Eine Ladepause kann
    #    eine Stunde dauern, das Telefon liegt derweil gesperrt im Auto - und
    #    danach geht die Fahrt weiter.
    lade_id, beendet = _fahrt_anlegen(client, fahrzeug, "Ladepause", 200,
                                      laedt=True)
    pruefe(lade_id not in [b["sitzung_id"] for b in beendet],
           "eine Fahrt, deren Ladestand zuletzt stieg, wird nach derselben "
           "Stille noch nicht beendet - sie lädt und fährt gleich weiter",
           str(beendet))
    pruefe(client.get(f"/api/live/{lade_id}").json()["laeuft"] is True,
           "sie läuft weiter")

    # 3. Aber auch die Ladepause ist irgendwann vorbei.
    lange_id, beendet = _fahrt_anlegen(client, fahrzeug, "Lange Pause", 400,
                                       laedt=True)
    pruefe(lange_id in [b["sitzung_id"] for b in beendet],
           "nach sieben Stunden wird auch sie beendet - sonst bliebe sie "
           "ewig offen", str(beendet))

    # 4. Eine Sitzung, die gerade eben gemeldet hat, bleibt unangetastet.
    frisch_id, beendet = _fahrt_anlegen(client, fahrzeug, "Läuft noch", 0)
    pruefe(frisch_id not in [b["sitzung_id"] for b in beendet],
           "eine, die gerade gemeldet hat, bleibt in Ruhe - Aufräumen darf "
           "keine laufende Fahrt abschneiden", str(beendet))


def teil_odometer_strecke():
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

    from app.live import aufzeichnung as auf

    client = TestClient(app)
    print("\nStrecke aus dem Kilometerstand")
    fahrzeug = client.get("/api/fahrzeuge").json()[0]

    def fahrt(name, mit_zaehler):
        start = client.post("/api/live/aufzeichnung", json={
            "fahrzeug_id": fahrzeug["id"], "lat": 48.0, "lon": 11.0,
            "soc": 90.0, "name": name}).json()
        db = SessionLocal()
        try:
            sitzung = db.get(models.LiveSitzung, start["sitzung_id"])
            beginn = datetime.utcnow() - timedelta(minutes=60)
            # Eine Serpentinenstrasse, grob abgetastet: Die Messpunkte liegen
            # so weit auseinander, dass die Luftlinie die Kurven abschneidet -
            # genau wie bei dreissig Sekunden Meldeabstand.
            for i in range(60):
                lat = 48.0 + i * 0.004
                lon = 11.0 + 0.02 * math.sin(i * 1.1)
                roh = {"soc_roh": 200}
                if mit_zaehler:
                    # Der Zaehler laeuft mit der *wirklichen* Strecke. Die
                    # Luftlinien zwischen den Messpunkten ergeben rund
                    # 1,1 km je Schritt; gefahren wurden 1,55 - die Kurven
                    # dazwischen, die kein Messpunkt gesehen hat.
                    roh["km_stand"] = 59500 + round(i * 1.55)
                live_sitzung.messpunkt_aufnehmen(
                    db, sitzung, lat, lon, soc=90.0 - i * 0.4,
                    aussentemp_c=12.0, zeit=beginn + timedelta(minutes=i),
                    rohwerte=roh)
        finally:
            db.close()
        return client.post(f"/api/live/{start['sitzung_id']}/ende").json()

    ohne = (fahrt("Nur GPS", False).get("aufzeichnung") or {})
    mit = (fahrt("Mit Zähler", True).get("aufzeichnung") or {})

    pruefe(ohne.get("ok") and mit.get("ok"),
           "beide Aufzeichnungen lassen sich abschliessen",
           f"{ohne.get('grund')} / {mit.get('grund')}")
    pruefe(ohne.get("strecke_quelle") == "gps",
           "ohne Kilometerstand bleibt es bei der GPS-Spur",
           str(ohne.get("strecke_quelle")))
    pruefe(mit.get("strecke_quelle") == "kilometerstand",
           "mit Kilometerstand wird der genommen",
           f"{mit.get('strecke_quelle')} - {mit.get('odometer')}")
    pruefe((mit.get("strecke_km") or 0) > (ohne.get("strecke_km") or 0) * 1.2,
           "und die Strecke wird spürbar länger - die abgeschnittenen Kurven "
           "kommen zurück",
           f"{ohne.get('strecke_km')} km → {mit.get('strecke_km')} km")

    # Der Zaehler muss auch zum Zaehlerstand passen.
    odo = (mit.get("odometer") or {}).get("odometer_km")
    pruefe(odo and abs((mit.get("strecke_km") or 0) - odo) < 1.5,
           "die gebaute Strecke trifft den Zählerstand",
           f"{mit.get('strecke_km')} km gegen {odo} km laut Zähler")

    # Unsinnige Werte duerfen nicht durchschlagen.
    faktor, grund = auf.odometer_faktor(
        [SimpleNamespace(rohwerte={"km_stand": 1000}),
         SimpleNamespace(rohwerte={"km_stand": 1900})], gps_km=10.0)
    pruefe(faktor == 1.0,
           "ein Zählerstand, der die Strecke verneunfachen würde, wird "
           "verworfen statt geglaubt", str(grund))
    faktor, _ = auf.odometer_faktor(
        [SimpleNamespace(rohwerte={"km_stand": 1000}),
         SimpleNamespace(rohwerte={"km_stand": 1002})], gps_km=2.5)
    pruefe(faktor == 1.0,
           "und unter fünf Kilometern gar nicht erst benutzt - bei einem "
           "Kilometer Auflösung wäre das geraten")


def teil_ladeplan_ein_weg():
    """`/ladeplan` und die Umplanung rechnen ueber denselben Weg.

    Beide bauten den Ladeplan vorher vollstaendig fuer sich: dieselbe
    Uebersetzung von Korridor-Kandidaten in Ladeoptionen (byteweise dieselben
    elf Zeilen) und derselbe Aufruf des Optimierers mit dreizehn Argumenten.
    Zweimal gepflegt heisst frueher oder later einmal vergessen - beim zuletzt
    ergaenzten `km_versatz` ist genau das passiert.

    Geprueft wird die Eigenschaft, auf die es ankommt: Eine Planung ab km 0
    mit dem Start-Ladestand ist derselbe Vorgang wie eine Umplanung ohne
    zurueckgelegte Strecke, also muss auch dasselbe herauskommen.
    """
    from app.live import umplanung as _u

    client = TestClient(app)
    route = fahrt_vorbereiten(client)
    fahrt_id = route["fahrt_id"]
    print("\nLadeplan und Umplanung: ein Weg")

    ueber_router = client.post(f"/api/fahrten/{fahrt_id}/ladeplan",
                               params={"min_kw": 100, "radius_km": 10}).json()
    db = SessionLocal()
    try:
        fahrt = db.get(models.Fahrt, fahrt_id)
        ueber_umplanung = _u.planen(db, fahrt, 0.0, fahrt.start_soc, {
            "radius_km": 10.0, "min_kw": 100.0, "steckertyp": "",
            "umweg_grenze_min": _u.VORGABEN["umweg_grenze_min"],
            "stopp_fixkosten_min": _u.VORGABEN["stopp_fixkosten_min"],
            "ladepark_bonus_min": _u.VORGABEN["ladepark_bonus_min"],
            "zeitwert_eur_h": _u.VORGABEN["zeitwert_eur_h"]})
    finally:
        db.close()

    pruefe(ueber_router.get("machbar") is True,
           "der Endpunkt liefert weiterhin einen machbaren Plan",
           ueber_router.get("grund", ""))
    pruefe(bool(ueber_router.get("stopps")),
           "mit Ladestopps", str(len(ueber_router.get("stopps") or [])))

    for feld in ("machbar", "anzahl_stopps", "gesamt_minuten",
                 "ladezeit_minuten", "kosten_eur", "soc_am_ziel"):
        pruefe(ueber_router.get(feld) == ueber_umplanung.get(feld),
               f"'{feld}' stimmt zwischen beiden Wegen überein",
               f"Router {ueber_router.get(feld)} / "
               f"Umplanung {ueber_umplanung.get(feld)}")

    a = [(s["id"], s["km_auf_route"], s["abfahrt_soc"])
         for s in ueber_router.get("stopps") or []]
    b = [(s["id"], s["km_auf_route"], s["abfahrt_soc"])
         for s in ueber_umplanung.get("stopps") or []]
    pruefe(a == b, "und die Stopps selbst sind dieselben - Standort, "
           "Kilometer und Abfahrts-Ladestand", f"{a[:2]} vs {b[:2]}")

    # Die Felder, die die Oberflaeche liest, muessen weiter da sein.
    fehlt = [f for f in ("machbar", "grund", "anzahl_stopps", "stopps",
                         "gesamt_minuten", "ladezeit_minuten",
                         "umwegzeit_minuten", "haltekosten_minuten",
                         "kosten_eur", "soc_am_ziel", "fahrt_id", "demo",
                         "steckertyp", "min_kw", "radius_km")
             if f not in ueber_router]
    pruefe(not fehlt,
           "und die Antwort trägt weiter alles, was die Oberfläche liest",
           str(fehlt))


def teil_laden_verfaelscht_nicht():
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
    from app.energie import kalibrierung

    client = TestClient(app)
    print("\nEin Ladestopp verfälscht weder Lernen noch Abweichung")
    fahrzeug = client.get("/api/fahrzeuge").json()[0]
    start = client.post("/api/live/aufzeichnung", json={
        "fahrzeug_id": fahrzeug["id"], "lat": 48.0, "lon": 11.0,
        "soc": 80.0, "name": "Mit Ladestopp"}).json()

    # Erst fahren, dann laden, dann weiterfahren - und zwar so, dass beide
    # Fahrtstuecke denselben Verbrauch je Kilometer haben.
    db = SessionLocal()
    try:
        sitzung = db.get(models.LiveSitzung, start["sitzung_id"])
        beginn = datetime.utcnow() - timedelta(minutes=150)
        soc, lat = 80.0, 48.0
        for i in range(51):
            if 25 <= i < 35:                 # Ladepause, das Auto steht
                soc = min(80.0, soc + 2.5)
            else:
                lat += 0.030
                soc -= 1.0
            live_sitzung.messpunkt_aufnehmen(
                db, sitzung, lat, 11.0, soc=round(soc, 1), aussentemp_c=10.0,
                zeit=beginn + timedelta(minutes=i * 2))
    finally:
        db.close()

    ende = client.post(f"/api/live/{start['sitzung_id']}/ende").json()
    gelernt = ende.get("gelernt")

    # Was der naive Weg geliefert haette - aus denselben Punkten gerechnet,
    # damit der Prüffall sich nicht an ausgerechneten Zahlen festmacht.
    db = SessionLocal()
    try:
        sitzung = db.get(models.LiveSitzung, start["sitzung_id"])
        akku = sitzung.fahrt.fahrzeug.akku_netto_kwh
        punkte = [p for p in sitzung.punkte if p.soll_soc is not None
                  and p.km_auf_route is not None and p.soc is not None]
        erster, letzter = punkte[0], punkte[-1]
        naiv_pp = erster.soc - letzter.soc
        echt_pp = sum(max(0.0, v.soc - n.soc)
                      for v, n in zip(punkte, punkte[1:]))
        naiv = kalibrierung.faktor_aus_fahrt(
            ((erster.soll_soc or 0.0) - (letzter.soll_soc or 0.0)) / 100.0 * akku,
            naiv_pp / 100.0 * akku,
            (letzter.km_auf_route or 0.0) - (erster.km_auf_route or 0.0))
    finally:
        db.close()

    pruefe(echt_pp > naiv_pp * 1.5,
           "der Aufbau prüft wirklich einen Ladestopp: tatsächlich verbraucht "
           f"wurden {echt_pp:.0f} pp, die nackte Differenz von Anfang bis "
           f"Ende zeigt nur {naiv_pp:.0f}",
           f"{echt_pp:.1f} vs {naiv_pp:.1f}")
    pruefe(gelernt is not None,
           "aus einer Fahrt mit Ladestopp wird überhaupt gelernt",
           str(ende.get("nicht_gelernt")))
    if gelernt:
        pruefe(naiv is None or gelernt["rohfaktor"] > naiv * 1.5,
               "und der gelernte Faktor rechnet den Ladestopp heraus - der "
               "naive Weg (Anfang minus Ende) läge deutlich darunter",
               f"gelernt={gelernt['rohfaktor']} naiv={naiv}")

    # Und die Abweichung waehrend der Fahrt.
    zustand = client.post(f"/api/live/{start['sitzung_id']}/punkt",
                          json={"lat": 48.5, "lon": 11.0, "soc": 50.0})
    # Die Sitzung ist beendet; eine zweite fuer die Abweichung.
    start2 = client.post("/api/live/aufzeichnung", json={
        "fahrzeug_id": fahrzeug["id"], "lat": 48.0, "lon": 11.0,
        "soc": 80.0, "name": "Abweichung"}).json()
    letzte = None
    for i in range(30):
        soc = 80.0 - i * 0.8 if i < 15 else 80.0 - (i - 15) * 0.8
        letzte = client.post(f"/api/live/{start2['sitzung_id']}/punkt", json={
            "lat": 48.0 + i * 0.012, "lon": 11.0, "soc": round(soc, 1)}).json()
    abw = letzte.get("abweichung_pp")
    pruefe(abw is None or abs(abw) < 40.0,
           "die Abweichung bleibt nach einem Ladesprung im lesbaren Bereich - "
           "vorher standen dort dreistellige Prozentpunkte", f"{abw} pp")


def teil_luecke_kilometer():
    """Die Begründung einer Umplanung nennt Kilometer der ganzen Fahrt.

    Der Optimierer rechnet unterwegs auf der **Reststrecke**, die bei null
    beginnt. Die Stopps werden hinterher zurueckgerechnet, der Begruendungs-
    satz aber nicht - er nannte Kilometer, die es auf der Strecke nicht gibt.
    In einem Probelauf stand bei km 137 die Meldung "zwischen km 0 und km 44".
    """
    from app.laden import optimierer

    print("\nBegründung nennt Kilometer der ganzen Fahrt")
    profil = optimierer.Streckenprofil(
        km=[0.0, 100.0, 200.0], kwh=[0.0, 40.0, 80.0],
        minuten=[0.0, 60.0, 120.0])

    class FZ:
        akku_netto_kwh = 50.0
        reserve_soc = 10.0

    # Ein einziger Ladepunkt weit hinten - von vorn nicht erreichbar.
    optionen = [optimierer.Ladeoption(
        id=1, km_auf_route=180.0, umweg_minuten=1.0, max_kw=150.0,
        anzahl_punkte=4, name="Weit weg", betreiber="X", ort="", lat=0.0,
        lon=0.0)]
    plan = optimierer.planen(profil, optionen, FZ(), [(0.0, 150.0), (100.0, 20.0)],
                             start_soc=30.0, km_versatz=250.0)
    pruefe(not plan.machbar, "der Plan geht nicht auf", plan.grund)
    zahlen = [int(t) for t in plan.grund.replace(".", " ").split()
              if t.isdigit()]
    pruefe(zahlen and min(zahlen) >= 250,
           "und die genannten Kilometer liegen hinter dem Versatz, nicht bei "
           "null - sonst sucht man am Steuer eine Stelle, die es nicht gibt",
           plan.grund)


def teil_zustand_koordinate():
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
    fahrzeug = client.get("/api/fahrzeuge").json()[0]
    start = client.post("/api/live/aufzeichnung", json={
        "fahrzeug_id": fahrzeug["id"], "lat": 48.0, "lon": 11.0,
        "soc": 90.0, "name": "Koordinate"}).json()

    zustand = client.post(f"/api/live/{start['sitzung_id']}/punkt",
                          json={"lat": 48.21, "lon": 11.34, "soc": 88.0}).json()
    pruefe(abs((zustand.get("lat") or 0) - 48.21) < 1e-6
           and abs((zustand.get("lon") or 0) - 11.34) < 1e-6,
           "der gemeldete Punkt kommt mit seiner Koordinate zurück",
           f"lat={zustand.get('lat')} lon={zustand.get('lon')}")

    zwei = client.post(f"/api/live/{start['sitzung_id']}/punkt",
                       json={"lat": 48.30, "lon": 11.40, "soc": 87.0}).json()
    pruefe(zwei.get("lat") != zustand.get("lat"),
           "und sie wandert mit - sonst bestünde die Spur aus einem Punkt",
           f"{zustand.get('lat')} → {zwei.get('lat')}")


def teil_import_laengen():
    """Ein zu langes Feld darf nicht den ganzen Import kosten.

    Passiert ist genau das: OCM liefert fuer Standorte mit mehreren
    Postleitzahlen eine Semikolon-Liste, und eine davon hat einen Lauf
    abgebrochen. Die Spalte wurde daraufhin verbreitert - was denselben
    Fehler nur hinausschiebt, denn der naechste Standort hat eine
    Postleitzahl mehr.
    """
    from app.laden import saeulen_import

    print("\nImport: zu lange Felder")
    db = SessionLocal()
    try:
        lang = ";".join(f"{33000 + i * 100}" for i in range(12))
        pruefe(len(lang) > 40, "der Testwert ist länger als die Spalte",
               f"{len(lang)} Zeichen")
        art = saeulen_import._speichern(db, "test", "lang-1", {
            "name": "Auchan " + "x" * 400, "betreiber": "Test",
            "lat": 44.9, "lon": -0.6, "plz": lang, "ort": "Bordeaux",
            "land": "FR", "anschluesse": [], "max_kw": 150.0,
            "anzahl_punkte": 4, "steckertypen": "CCS"})
        db.commit()
        pruefe(art == "neu", "der Datensatz geht durch, statt den Lauf "
               "abzubrechen", art)
        gespeichert = (db.query(models.Ladepunkt)
                       .filter_by(quelle="test", fremd_id="lang-1").one())
        pruefe(len(gespeichert.plz) <= 40,
               "die zu lange Postleitzahl ist gestutzt, nicht der Ladepunkt "
               "verworfen - eine halbe PLZ ist ein Schönheitsfehler, ein "
               "fehlender Ladepunkt auf der Route nicht",
               f"{len(gespeichert.plz)} Zeichen")
        pruefe(gespeichert.max_kw == 150.0 and gespeichert.anzahl_punkte == 4,
               "und die Zahlen bleiben unangetastet")
    finally:
        db.close()


def teil_hoehenquelle():
    """Geglättete GPS-Höhen - und eine ehrliche Auskunft, woher sie stammen.

    Das Verbrauchsmodell summiert die *positiven* Höhenunterschiede auf.
    Diese Gleichrichtung macht aus mittelwertfreiem Rauschen einen
    systematischen Zuschlag, der sich linear über die Punkte aufaddiert -
    aus einer Fahrt durch die Ebene wird eine Alpenetappe, und die geht
    ungebremst in den Korrekturfaktor.
    """
    import random

    from app.geo import haversine_m as _haversine
    from app.live import aufzeichnung as auf

    print("\nHöhen: Quelle und Glättung")

    def steigung(hoehen):
        return sum(max(0.0, b - a) for a, b in zip(hoehen, hoehen[1:]))

    # Eine Fahrt geradeaus durch die Ebene, rund 800 Punkte im Abstand von
    # etwa 25 m. Die wahre Steigung ist null.
    strecke = [[11.0 + i * 0.00032, 48.0] for i in range(800)]
    abstand = _haversine(48.0, strecke[0][0], 48.0, strecke[1][0])
    pruefe(15.0 < abstand < 40.0,
           "die Testpunkte liegen etwa so dicht wie echte Messpunkte",
           f"{abstand:.0f} m")

    random.seed(7)
    verrauscht = [random.gauss(0.0, 10.0) for _ in strecke]
    roh_steigung = steigung(verrauscht)
    pruefe(roh_steigung > 2000.0,
           "rohes GPS macht aus der Ebene mehrere Kilometer Steigung - "
           "genau der Grund für die Glättung", f"{roh_steigung:.0f} m")

    geglaettet = auf.gps_hoehen_glaetten(strecke, verrauscht)
    glatt_steigung = steigung(geglaettet)
    pruefe(glatt_steigung < roh_steigung / 5.0,
           "geglättet bleibt davon weniger als ein Fünftel",
           f"{roh_steigung:.0f} m → {glatt_steigung:.0f} m")

    # Aber ein echter Hügel muss stehen bleiben - eine Glättung, die auch
    # das Gelände wegnimmt, wäre nur eine umständliche Art, flach zu rechnen.
    hoch = 300.0
    berg = [hoch * (i / 400.0 if i < 400 else (800 - i) / 400.0)
            for i in range(800)]
    berg_glatt = auf.gps_hoehen_glaetten(strecke, berg)
    pruefe(steigung(berg_glatt) > 0.9 * hoch,
           "ein echter Anstieg über 10 km übersteht die Glättung",
           f"{steigung(berg_glatt):.0f} von {hoch:.0f} m")

    # Und die Auskunft muss stimmen. Ohne ORS-Schlüssel liefert das
    # Demo-Routing bewusst keine Höhen; dann ist die Quelle "gps" - und
    # nicht, wie vorher, immer "karte".
    _, quelle = auf.hoehen_ergaenzen(strecke, verrauscht)
    pruefe(quelle == "gps",
           "fällt die Kartenabfrage aus, heisst die Quelle auch 'gps'", quelle)
    _, quelle = auf.hoehen_ergaenzen(strecke, None)
    pruefe(quelle == "flach",
           "und ohne jede Höhe 'flach' - man muss einer Fahrt ansehen "
           "können, ob ihre Höhen etwas taugen", quelle)


def teil_abgeloeste_fahrt():
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
    fahrzeug = client.get("/api/fahrzeuge").json()[0]

    erste = client.post("/api/live/aufzeichnung", json={
        "fahrzeug_id": fahrzeug["id"], "lat": 48.0, "lon": 11.0,
        "soc": 90.0, "name": "Vergessen"}).json()
    db = SessionLocal()
    try:
        sitzung = db.get(models.LiveSitzung, erste["sitzung_id"])
        beginn = datetime.utcnow() - timedelta(minutes=40)
        for i in range(41):
            live_sitzung.messpunkt_aufnehmen(
                db, sitzung, 48.0 + i * 0.009, 11.0, soc=90.0 - i * 0.25,
                aussentemp_c=12.0, zeit=beginn + timedelta(minutes=i))
    finally:
        db.close()

    # Und jetzt faehrt jemand los, ohne die alte Fahrt beendet zu haben.
    client.post("/api/live/aufzeichnung", json={
        "fahrzeug_id": fahrzeug["id"], "lat": 49.0, "lon": 9.0,
        "soc": 80.0, "name": "Die neue"})

    db = SessionLocal()
    try:
        alte = db.get(models.LiveSitzung, erste["sitzung_id"])
        pruefe(alte.laeuft is False, "die alte Sitzung weicht der neuen")
        fahrt = alte.fahrt
        pruefe(bool(fahrt.geometrie),
               "aber sie wird dabei abgeschlossen - die Strecke entsteht "
               "noch, statt mit der Sitzung zu verschwinden",
               f"geometrie={len(fahrt.geometrie or [])} Punkte")
        pruefe(bool(fahrt.energieprofil) and (fahrt.strecke_m or 0) > 1000,
               "mit Energieprofil und Strecke, also auswertbar",
               f"strecke_m={fahrt.strecke_m}")
    finally:
        db.close()


def teil_anbauten():
    """Fahrradträger und Dachbox - Zuschlag auf den Luftwiderstand.

    Zwei Dinge müssen gelten, und das zweite ist das wichtigere.
    """
    print("\nAussen am Auto: Fahrradträger und Dachbox")

    class FzStub:
        leermasse_kg = 2550.0
        zuladung_kg = 150.0
        masse_kg = 2700.0
        c_w = 0.29
        stirnflaeche_m2 = 2.90
        c_rr = 0.011
        eta_antrieb = 0.87
        eta_rekup = 0.68
        p_neben_w = 450.0
        waermepumpe = True
        akku_netto_kwh = 77.0
        reserve_soc = 10.0
        korrekturfaktor = 1.0

    class FahrtStub2:
        def __init__(self, faktor, zuladung=None):
            self.fahrzeug = FzStub()
            self.zuladung_kg = zuladung
            self.luftwiderstand_faktor = faktor

    ohne = Fahrzeugwerte.aus_fahrt(FahrtStub2(1.0))
    mit = Fahrzeugwerte.aus_fahrt(FahrtStub2(1.25))
    pruefe(abs(ohne.c_w - 0.29) < 1e-9,
           "ohne Anbau bleibt der Beiwert unverändert", str(ohne.c_w))
    pruefe(abs(mit.c_w - 0.29 * 1.25) < 1e-9,
           "ein Zuschlag von 25 % erhöht den Luftwiderstandsbeiwert",
           str(mit.c_w))
    pruefe(abs(mit.stirnflaeche_m2 - 2.90) < 1e-9,
           "die Stirnfläche bleibt, was sie ist - sie ist eine Abmessung des "
           "Autos und ändert sich nicht, wenn hinten Räder hängen")

    # Und die Wirkung muss beim Verbrauch ankommen, mit v².
    profil = [{"km": k, "lat": 48.0 + k * 0.009, "lon": 11.0, "hoehe": 100.0,
               "tempo_kmh": 120.0, "minuten": k * 0.5, "soc": 80 - k * 0.1,
               "kwh": k * 0.2} for k in range(0, 201, 5)]
    umgebung = lambda lat, lon: Umgebung(temp_c=15.0)      # noqa: E731
    a = umplanung.restprofil_physik(FahrtStub2(1.0), profil, 1.0, umgebung)
    b = umplanung.restprofil_physik(FahrtStub2(1.25), profil, 1.0, umgebung)
    pruefe(b.kwh[-1] > a.kwh[-1] * 1.05,
           "mit Träger braucht dieselbe Strecke spürbar mehr Energie",
           f"{b.kwh[-1]:.1f} gegen {a.kwh[-1]:.1f} kWh")

    # Die Zuladung wirkt weiter unabhängig davon.
    schwer = Fahrzeugwerte.aus_fahrt(FahrtStub2(1.0, zuladung=500.0))
    pruefe(abs(schwer.masse_kg - 3050.0) < 1e-9,
           "die Zuladung der Fahrt zählt unabhängig vom Anbau",
           str(schwer.masse_kg))


def teil_aufzeichnung():
    """Eine gefahrene Strecke ohne Planung - und was daraus entsteht.

    Der umgekehrte Weg zur geplanten Fahrt: losfahren, mitschreiben, und die
    Strecke hinterher aus den Messpunkten bauen. Gedacht für die
    Kalibrierung, wo eine bekannte kurze Strecke die sauberste Messung ist -
    und wo eine Route vorher zu planen umständlich genug wäre, dass man es
    bleiben lässt.
    """
    client = TestClient(app)
    print("\nFahrt aufzeichnen statt planen")

    fahrzeug = client.get("/api/fahrzeuge").json()[0]
    antwort = client.post("/api/live/aufzeichnung", json={
        "fahrzeug_id": fahrzeug["id"], "lat": 48.0, "lon": 11.0,
        "soc": 90.0, "name": "Runde um den Block"})
    pruefe(antwort.status_code == 200,
           "eine Aufzeichnung lässt sich ohne Route starten",
           f"HTTP {antwort.status_code}: {antwort.text[:120]}")
    start = antwort.json()
    sitzung_id = start["sitzung_id"]

    zustand = client.get(f"/api/live/{sitzung_id}").json()
    pruefe(zustand["laeuft"] is True,
           "und läuft, obwohl es weder Strecke noch Plan gibt")

    # Sechzig Kilometer nach Norden, eine Stunde lang, 12 Prozentpunkte
    # Verbrauch. Bei 0,009 Grad je Punkt sind das rund einen Kilometer.
    db = SessionLocal()
    try:
        sitzung = db.get(models.LiveSitzung, sitzung_id)
        beginn = datetime(2026, 1, 1, 8, 0)
        for i in range(61):
            live_sitzung.messpunkt_aufnehmen(
                db, sitzung, 48.0 + i * 0.009, 11.0,
                soc=90.0 - i * 0.2, aussentemp_c=7.0,
                zeit=beginn + timedelta(minutes=i),
                rohwerte={"soc_roh": round((90.0 - i * 0.2) * 2.5),
                          "hoehe_m": 500.0})
    finally:
        db.close()

    ende = client.post(f"/api/live/{sitzung_id}/ende").json()
    gebaut = ende.get("aufzeichnung") or {}
    pruefe(gebaut.get("ok") is True,
           "beim Beenden entsteht aus den Messpunkten eine Strecke",
           str(gebaut.get("grund")))
    pruefe(55 < (gebaut.get("strecke_km") or 0) < 65,
           "die Strecke stimmt mit dem überein, was gefahren wurde",
           f"{gebaut.get('strecke_km')} km statt rund 60")
    pruefe(abs((gebaut.get("aussentemp_c") or 0) - 7.0) < 0.1,
           "die **gemessene** Aussentemperatur gilt, nicht eine Vorhersage",
           str(gebaut.get("aussentemp_c")))

    fahrt = client.get(f"/api/fahrten/{start['fahrt_id']}").json()
    pruefe(len(fahrt.get("profil") or []) > 5,
           "die Fahrt hat hinterher ein Energieprofil",
           f"{len(fahrt.get('profil') or [])} Stützstellen")

    # Der Zweck der ganzen Betriebsart: Aus der Aufzeichnung muss sich der
    # Korrekturfaktor lernen lassen. Ohne Kilometerstand und Sollwert an den
    # Messpunkten findet die Kalibrierung nichts - und beides steht erst
    # fest, seit die Strecke gebaut wurde.
    pruefe(ende.get("gelernt") is not None,
           "und jolt lernt daraus einen Korrekturfaktor - genau dafür ist "
           "die Aufzeichnung da", str(ende.get("gelernt")))


def teil_kette_mit_ladestopp():
    """Die ganze Kette, aber diesmal wird unterwegs wirklich geladen.

    Der Simulator lädt nie - sein Ladestand fällt monoton bis null. Damit
    bleibt der Normalfall jeder echten Langstrecke ungeprüft: anhalten,
    laden, weiterfahren. Genau dort lag der Fehler, den dieser Teil festhält.
    """
    client = TestClient(app)
    route = fahrt_vorbereiten(client)
    fahrt_id = route["fahrt_id"]

    print("\nEin Ladestopp ist keine Verspätung")
    start = client.post(f"/api/live/start/{fahrt_id}",
                        params={"min_kw": 100, "radius_km": 10}).json()
    sitzung_id = start["sitzung_id"]

    LADEDAUER_MIN = 30.0
    LADEHUB_PP = 45.0

    db = SessionLocal()
    try:
        sitzung = db.get(models.LiveSitzung, sitzung_id)
        profil = sitzung.fahrt.energieprofil or []
        gesamt_km = profil[-1]["km"] if profil else 0.0
        beginn = datetime(2026, 1, 1, 8, 0)

        def melden(km, soc, minuten):
            eintrag = _profil_bei(profil, km)
            zustand = live_sitzung.messpunkt_aufnehmen(
                db, sitzung, eintrag["lat"], eintrag["lon"], round(soc, 2),
                zeit=beginn + timedelta(minutes=minuten))
            return live_sitzung.zustand_als_dict(zustand)

        # Erster Abschnitt: exakt nach Plan, damit keine andere Abweichung die
        # Aussage verwässert. Der Ladestand ist der des Profils, die Uhr die
        # des Profils.
        pause_km = min(150.0, gesamt_km / 3)
        letzter = None
        km = 0.0
        while km <= pause_km:
            eintrag = _profil_bei(profil, km)
            letzter = melden(km, eintrag["soc"], eintrag.get("minuten") or 0.0)
            km += 10.0

        vor_pause = letzter["ankunft_verschiebung_min"]
        pruefe(vor_pause is not None and abs(vor_pause) < 5.0,
               "vor der Pause liegt die Fahrt in der Zeit", f"{vor_pause} min")

        # Der Ladestopp: dreissig Minuten am selben Ort, der Ladestand steigt
        # um 45 Prozentpunkte.
        eintrag = _profil_bei(profil, pause_km)
        soc_ankunft = eintrag["soc"]
        uhr = eintrag.get("minuten") or 0.0
        for i in range(1, 4):
            letzter = melden(pause_km, soc_ankunft + LADEHUB_PP * i / 3,
                             uhr + LADEDAUER_MIN * i / 3)

        nach_pause = letzter["ankunft_verschiebung_min"]
        pruefe(nach_pause is not None
               and abs(nach_pause) < live_sitzung.SCHWELLE_ANKUNFT_MIN,
               "und direkt nach dem Laden immer noch - die Ladezeit stand so "
               "im Plan und ist keine Verspätung",
               f"{nach_pause} min (ohne Abzug wären es rund "
               f"{LADEDAUER_MIN:.0f})")

        # Weiterfahren. Der Ladestand liegt jetzt um den Ladehub über dem
        # Profil, die Uhr um die Ladedauer dahinter - beides muss die
        # Nachführung auseinanderhalten können.
        km = pause_km + 10.0
        weit = min(gesamt_km, pause_km + 150.0)
        while km <= weit:
            eintrag = _profil_bei(profil, km)
            letzter = melden(km, min(100.0, eintrag["soc"] + LADEHUB_PP),
                             (eintrag.get("minuten") or 0.0) + LADEDAUER_MIN)
            km += 10.0

        spaeter = letzter["ankunft_verschiebung_min"]
        pruefe(spaeter is not None
               and abs(spaeter) < live_sitzung.SCHWELLE_ANKUNFT_MIN,
               "auch 150 km danach wird die Ladezeit nicht als Verspätung "
               "nachgetragen", f"{spaeter} min")
        pruefe(abs(letzter["zeitfaktor"] - 1.0) < 0.15,
               "und der Zeitfaktor bleibt bei rund 1 - gefahren wurde ja "
               "nach Plan", f"×{letzter['zeitfaktor']}")
    finally:
        db.close()

    client.post(f"/api/live/{sitzung_id}/ende")


def teil_position_ohne_ladestand():
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
    route = fahrt_vorbereiten(client)
    fahrt_id = route["fahrt_id"]

    print("\nPosition ohne Ladestand")
    start = client.post(f"/api/live/start/{fahrt_id}",
                        params={"min_kw": 100, "radius_km": 10}).json()
    sitzung_id = start["sitzung_id"]

    db = SessionLocal()
    try:
        sitzung = db.get(models.LiveSitzung, sitzung_id)
        profil = sitzung.fahrt.energieprofil or []
        beginn = datetime(2026, 1, 1, 8, 0)

        def melden(km, soc=None):
            eintrag = _profil_bei(profil, km)
            zustand = live_sitzung.messpunkt_aufnehmen(
                db, sitzung, eintrag["lat"], eintrag["lon"], soc,
                zeit=beginn + timedelta(minutes=eintrag.get("minuten") or 0.0))
            return live_sitzung.zustand_als_dict(zustand)

        # Ein Anker beim Losfahren, danach nur noch Position.
        melden(0.0, soc=_profil_bei(profil, 0.0)["soc"])
        letzter = None
        for km in range(10, 101, 10):
            letzter = melden(float(km))

        pruefe(letzter["soc_gemeldet"] is False,
               "ein Punkt ohne Ladestand wird als gerechnet gekennzeichnet")
        soll_100 = _profil_bei(profil, 100.0)["soc"]
        pruefe(letzter["ist_soc"] is not None
               and abs(letzter["ist_soc"] - soll_100) < 1.0,
               "und der Ladestand wird aus dem Profil hochgerechnet",
               f"{letzter['ist_soc']} gegen Profil {soll_100}")
        pruefe(abs(letzter["verbrauchsfaktor"] - 1.0) < 1e-6,
               "die Schätzung selbst verändert den Verbrauchsfaktor nicht - "
               "sonst misst das Modell sich an sich selbst",
               f"×{letzter['verbrauchsfaktor']}")

        # Das ist der eigentliche Gewinn: Ohne diese Punkte gäbe es unterwegs
        # weder Zeitfaktor noch Ankunftsprognose.
        pruefe(letzter["ankunft_verschiebung_min"] is not None,
               "die Ankunftsprognose kommt allein aus Positionsmeldungen "
               "zustande", str(letzter["ankunft_verschiebung_min"]))

        # Jetzt der Anker an der Säule: acht Prozentpunkte weniger als gedacht.
        soc_start = _profil_bei(profil, 0.0)["soc"]
        anker = melden(100.0, soc=soll_100 - 8.0)
        pruefe(anker["soc_gemeldet"] is True,
               "ein eingetippter Ladestand ist eine Meldung, keine Schätzung")

        # Und zwar auf den richtigen Wert. Die Zahl ist hier der ganze Punkt:
        # Die acht Prozentpunkte sind über hundert Kilometer entstanden, nicht
        # über die letzten zwanzig. Wer die Schätzungen dazwischen für
        # Messungen hält, misst die Abweichung gegen die kurze Basis des
        # gleitenden Fensters und kommt auf ×2,08 statt ×1,23 - er verdoppelt
        # den gemessenen Mehrverbrauch und plant den Rest der Fahrt danach.
        # Eine Schranke wie "grösser als 1" fiele darauf herein.
        erwartet = ((soc_start - (soll_100 - 8.0))
                    / (soc_start - soll_100))
        pruefe(abs(anker["verbrauchsfaktor"] - erwartet) < 0.03,
               "und der Verbrauchsfaktor misst gegen den letzten *gemeldeten* "
               "Ladestand, nicht gegen die eigene Schätzung",
               f"×{anker['verbrauchsfaktor']} statt ×{erwartet:.3f}")

        # Und ab da rechnet die Schätzung mit dem neuen Faktor weiter.
        weiter = None
        for km in range(110, 161, 10):
            weiter = melden(float(km))
        soll_160 = _profil_bei(profil, 160.0)["soc"]
        pruefe(weiter["ist_soc"] < soll_160 - 8.0,
               "danach liegt die Schätzung unter dem Profil - der gemessene "
               "Mehrverbrauch wird fortgeschrieben, nicht vergessen",
               f"{weiter['ist_soc']} gegen Profil {soll_160}")
    finally:
        db.close()

    client.post(f"/api/live/{sitzung_id}/ende")


def teil_tempo_in_der_kette():
    """Kommt das gemessene Tempo in der laufenden Fahrt tatsächlich an?

    Die Physik dafür steht in `teil_tempo_neurechnen`. Hier geht es nur um
    die Verdrahtung: Solange niemand einen Ladestand gemeldet hat, gibt es
    keinen Verbrauchsfaktor - und dann muss der Plan auf dem gemessenen Tempo
    beruhen statt auf dem Reglerwert von vor der Abfahrt.
    """
    client = TestClient(app)
    route = fahrt_vorbereiten(client)
    fahrt_id = route["fahrt_id"]

    print("\nGemessenes Tempo schlägt bis in den Plan durch")
    start = client.post(f"/api/live/start/{fahrt_id}",
                        params={"min_kw": 100, "radius_km": 10}).json()
    sitzung_id = start["sitzung_id"]
    pruefe((start.get("plan") or {}).get("grundlage") in (None, "planung"),
           "der Startplan beruht noch auf der Planung - gemessen ist da "
           "nichts", str((start.get("plan") or {}).get("grundlage")))

    db = SessionLocal()
    try:
        sitzung = db.get(models.LiveSitzung, sitzung_id)
        profil = sitzung.fahrt.energieprofil or []
        beginn = datetime(2026, 1, 1, 8, 0)
        # Achtzehn Prozent schneller als geplant: Die Uhr läuft langsamer als
        # das Profil vorsah. Der Ladestand bleibt unbekannt - genau der Fall,
        # für den das gemessene Tempo gedacht ist.
        SCHNELLER = 0.82
        for km in range(0, 141, 10):
            eintrag = _profil_bei(profil, float(km))
            live_sitzung.messpunkt_aufnehmen(
                db, sitzung, eintrag["lat"], eintrag["lon"], None,
                zeit=beginn + timedelta(
                    minutes=(eintrag.get("minuten") or 0.0) * SCHNELLER))
        db.refresh(sitzung)
        plan = sitzung.plan or {}
    finally:
        db.close()

    pruefe(plan.get("grundlage") == "tempo gemessen",
           "unterwegs wird der Plan mit dem gemessenen Tempo neu gerechnet",
           str(plan.get("grundlage")))
    pruefe((plan.get("tempo_faktor") or 0) > 1.1,
           "und der Faktor entspricht dem, was tatsächlich gefahren wurde",
           f"×{plan.get('tempo_faktor')}")
    client.post(f"/api/live/{sitzung_id}/ende")



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
    teil_ladepausen()
    teil_tempo_neurechnen()
    teil_reststrecke()
    teil_planvergleich()
    teil_kette()
    teil_kette_mit_ladestopp()
    teil_anbauten()
    teil_verwaiste_fahrt()
    teil_abgeloeste_fahrt()
    teil_hoehenquelle()
    teil_odometer_strecke()
    teil_ladeplan_ein_weg()
    teil_laden_verfaelscht_nicht()
    teil_luecke_kilometer()
    teil_zustand_koordinate()
    teil_import_laengen()
    teil_aufzeichnung()
    teil_position_ohne_ladestand()
    teil_tempo_in_der_kette()

    return pruefe.bilanz()


if __name__ == "__main__":
    sys.exit(main())
