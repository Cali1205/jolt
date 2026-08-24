#!/usr/bin/env python3
"""Prüft den Ladestopp-Optimierer an Fällen, deren Ergebnis man vorher kennt.

Kein Testframework, keine Netzverbindung, keine Datenbank - `python3
tools/check_optimierer.py` genügt.

Der Prüfstein ist bewusst nicht die Selbstauskunft des Optimierers, sondern ein
zweiter, unabhängiger Nachrechner: Er fährt den fertigen Plan Kilometer für
Kilometer ab und schaut, ob der Ladestand irgendwo unter die Reserve fällt.
Ein Planer, der sich selbst bestätigt, prüft nichts.

Geprüft wird ausserdem gegen die beiden Fälle, an denen ein gieriger Planer
laut Konzept systematisch scheitert - eine lange Lücke ohne Schnelllader und
die Wahl zwischen einer nahen schwachen und einer weiteren starken Säule.

    ./tools/check_optimierer.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "backend"))

from app.energie.modell import Fahrzeugwerte  # noqa: E402
from app.laden import optimierer  # noqa: E402

FEHLER: list[str] = []

# Eine Kurve mit deutlichem Knick: volle Leistung bis 40 %, danach fällt sie
# steil ab. Genau daran muss sich zeigen, ob der Optimierer die Ladehübe in
# den steilen Teil legt.
KURVE = [(0, 110), (10, 120), (30, 120), (50, 90), (70, 60), (80, 45),
         (90, 28), (100, 8)]


def pruefe(bedingung, text: str, zusatz: str = "") -> None:
    if bedingung:
        print(f"  ok    {text}")
    else:
        print(f"  FEHLT {text}   {zusatz}")
        FEHLER.append(text)


def abschnitt(titel: str) -> None:
    print(f"\n{titel}")


# ---------------------------------------------------------------------------
# Werkzeug
# ---------------------------------------------------------------------------

def profil_bauen(km_gesamt: float, kwh_je_km: float = 0.18,
                 tempo_kmh: float = 120.0, kwh_bei=None):
    """Ein synthetisches Streckenprofil, ein Stützpunkt je Kilometer.

    `kwh_bei` erlaubt einen beliebigen Verlauf der kumulierten Energie - so
    lässt sich ein Pass bauen, bei dem die Bilanz am Ende harmlos aussieht und
    unterwegs trotzdem nichts mehr im Akku ist.
    """
    km = [float(i) for i in range(int(km_gesamt) + 1)]
    kwh = [kwh_bei(k) for k in km] if kwh_bei else [k * kwh_je_km for k in km]
    return optimierer.Streckenprofil(
        km=km, kwh=kwh, minuten=[k / tempo_kmh * 60.0 for k in km])


def saeulen(kms, max_kw=150.0, umweg=5.0, anzahl_punkte=4, ab_id=1):
    return [optimierer.Ladeoption(id=ab_id + i, km_auf_route=float(k),
                                  umweg_minuten=umweg, max_kw=max_kw,
                                  anzahl_punkte=anzahl_punkte,
                                  name=f"Lader km {k:.0f}")
            for i, k in enumerate(kms)]


def nachrechnen(plan, profil, fz, start_soc: float) -> dict:
    """Den fertigen Plan unabhängig nachfahren.

    Der eigentliche Test: Der Optimierer behauptet Ankunfts- und Abfahrtswerte;
    hier wird nachgesehen, ob sie zum Streckenprofil passen und ob der
    Ladestand zwischen den Stopps irgendwo unter die Reserve rutscht - auch
    dort, wo gar kein Stopp geplant ist.
    """
    soc_je_kwh = 100.0 / fz.akku_netto_kwh
    soc = start_soc
    tiefster = soc
    groesste_abweichung = 0.0
    marke_km = 0.0
    marke_kwh = profil.wert(profil.kwh, 0.0)

    stationen = [(s.option.km_auf_route, s) for s in plan.stopps]
    stationen.append((profil.gesamt_km, None))

    for ziel_km, stopp in stationen:
        for i, k in enumerate(profil.km):
            if marke_km <= k <= ziel_km:
                tiefster = min(tiefster, soc - (profil.kwh[i] - marke_kwh) * soc_je_kwh)
        ankunft = soc - (profil.wert(profil.kwh, ziel_km) - marke_kwh) * soc_je_kwh
        tiefster = min(tiefster, ankunft)
        if stopp is None:
            soc = ankunft
            break
        groesste_abweichung = max(groesste_abweichung,
                                  abs(ankunft - stopp.ankunft_soc))
        soc = stopp.abfahrt_soc
        marke_km = ziel_km
        marke_kwh = profil.wert(profil.kwh, ziel_km)

    return {"tiefster_soc": tiefster, "soc_am_ziel": soc,
            "abweichung": groesste_abweichung}


def plan_pruefen(plan, profil, fz, start_soc, ziel_soc, name: str) -> dict:
    """Die Prüfungen, die für jeden machbaren Plan gelten müssen."""
    fakten = nachrechnen(plan, profil, fz, start_soc)
    pruefe(fakten["tiefster_soc"] >= fz.reserve_soc - 0.5,
           f"{name}: der Ladestand fällt nirgends unter die Reserve",
           f"tiefster {fakten['tiefster_soc']:.1f} %, Reserve {fz.reserve_soc} %")
    pruefe(fakten["soc_am_ziel"] >= ziel_soc - 0.5,
           f"{name}: der Ziel-Ladestand wird erreicht",
           f"{fakten['soc_am_ziel']:.1f} % statt {ziel_soc} %")
    pruefe(fakten["abweichung"] < 0.6,
           f"{name}: die ausgewiesenen Ankunftswerte stimmen mit der Strecke überein",
           f"grösste Abweichung {fakten['abweichung']:.2f} Prozentpunkte")
    summe = (plan.fahrzeit_minuten + plan.ladezeit_minuten
             + plan.umwegzeit_minuten)
    pruefe(abs(summe - plan.gesamt_minuten) < 0.5,
           f"{name}: Fahrzeit, Ladezeit und Umwege ergeben die Gesamtzeit")

    # Kein Stopp, der sich nicht lohnt. Ein Halt kostet allein an Fixkosten
    # rund vier Minuten; wer dafür zwei Prozentpunkte lädt, hätte dieselbe
    # Energie am nächsten Stopp in weniger Zeit bekommen. Solche Stopps
    # entstanden, als die Nachoptimierung die Ladehübe auf das gerade noch
    # Nötige herunterschliff - der Suche waren sie verboten, dem
    # Nachoptimierer nicht.
    huebe = [s.abfahrt_soc - s.ankunft_soc for s in plan.stopps]
    pruefe(all(h >= optimierer.MIN_LADEHUB - 0.5 for h in huebe),
           f"{name}: an keinem Stopp wird weniger als der Mindesthub geladen",
           f"Ladehübe {[round(h, 1) for h in huebe]}, "
           f"Mindesthub {optimierer.MIN_LADEHUB}")
    return fakten


# ---------------------------------------------------------------------------
# Die Fälle
# ---------------------------------------------------------------------------

def fall_kurze_strecke():
    abschnitt("Kurze Strecke - es braucht keinen Stopp")
    fz = Fahrzeugwerte(akku_netto_kwh=60.0, reserve_soc=10.0)
    profil = profil_bauen(150)
    plan = optimierer.planen(profil, saeulen([50, 100]), fz, KURVE,
                             start_soc=80.0, ziel_soc=20.0,
                             max_fahrzeug_kw=150.0)
    pruefe(plan.machbar, "machbar")
    pruefe(len(plan.stopps) == 0, "und zwar ohne Ladestopp",
           f"{len(plan.stopps)} Stopps geplant")
    pruefe(abs(plan.gesamt_minuten - plan.fahrzeit_minuten) < 0.5,
           "die Gesamtzeit ist dann die reine Fahrzeit")


def fall_langstrecke():
    abschnitt("Langstrecke mit dichten Ladepunkten")
    fz = Fahrzeugwerte(akku_netto_kwh=60.0, reserve_soc=10.0)
    profil = profil_bauen(600)
    plan = optimierer.planen(profil, saeulen(range(50, 600, 50)), fz, KURVE,
                             start_soc=90.0, ziel_soc=20.0,
                             max_fahrzeug_kw=150.0)
    pruefe(plan.machbar, "machbar", plan.grund)
    pruefe(len(plan.stopps) >= 2, "mehrere Stopps nötig",
           f"{len(plan.stopps)} geplant")
    plan_pruefen(plan, profil, fz, 90.0, 20.0, "Langstrecke")

    # Der Zeitgewinn aus Abschnitt 2.4: lieber zweimal kurz in den steilen
    # Teil der Kurve als einmal lang bis 90 %.
    hoechste_abfahrt = max(s.abfahrt_soc for s in plan.stopps)
    pruefe(hoechste_abfahrt <= 80.0,
           "kein Stopp lädt in den flachen Teil der Kurve, solange Säulen dicht stehen",
           f"höchster Abfahrtswert {hoechste_abfahrt:.0f} %")
    return plan


def fall_starke_saeule_gewinnt():
    abschnitt("Wahl zwischen naher schwacher und weiterer starker Säule")
    fz = Fahrzeugwerte(akku_netto_kwh=60.0, reserve_soc=10.0)
    profil = profil_bauen(450)
    optionen = [
        optimierer.Ladeoption(id=1, km_auf_route=200.0, umweg_minuten=5.0,
                              max_kw=50.0, anzahl_punkte=4, name="50 kW bei km 200"),
        optimierer.Ladeoption(id=2, km_auf_route=240.0, umweg_minuten=5.0,
                              max_kw=300.0, anzahl_punkte=4, name="300 kW bei km 240"),
    ]
    plan = optimierer.planen(profil, optionen, fz, KURVE, start_soc=90.0,
                             ziel_soc=20.0, max_fahrzeug_kw=300.0)
    pruefe(plan.machbar, "machbar", plan.grund)
    gewaehlt = [s.option.id for s in plan.stopps]
    pruefe(gewaehlt == [2],
           "die starke Säule 40 km weiter gewinnt gegen die nahe schwache",
           f"gewählt: {gewaehlt}")
    plan_pruefen(plan, profil, fz, 90.0, 20.0, "Säulenwahl")


def fall_luecke_vorher_fuellen():
    abschnitt("Lange Lücke ohne Schnelllader - vorher volltanken")
    fz = Fahrzeugwerte(akku_netto_kwh=60.0, reserve_soc=10.0)
    profil = profil_bauen(700)
    # Nach km 260 kommt bis km 540 nichts mehr. Wer dort nur so weit lädt, wie
    # es sich gerade lohnt, steht in der Lücke - genau der Fall, an dem ein
    # gieriger Planer scheitert.
    optionen = saeulen([100, 180, 260, 540, 620])
    plan = optimierer.planen(profil, optionen, fz, KURVE, start_soc=90.0,
                             ziel_soc=15.0, max_fahrzeug_kw=150.0)
    pruefe(plan.machbar, "machbar", plan.grund)
    plan_pruefen(plan, profil, fz, 90.0, 15.0, "Lücke")

    vor_luecke = [s for s in plan.stopps if abs(s.option.km_auf_route - 260.0) < 1]
    pruefe(bool(vor_luecke), "an der letzten Säule vor der Lücke wird gehalten")
    if vor_luecke:
        pruefe(vor_luecke[0].abfahrt_soc >= 90.0,
               "und dort weit in den flachen Teil der Kurve geladen - "
               "die Lücke lässt nichts anderes zu",
               f"Abfahrt mit {vor_luecke[0].abfahrt_soc:.0f} %")


def fall_pass():
    abschnitt("Pass - die Spitze zählt, nicht die Bilanz am Ende")
    fz = Fahrzeugwerte(akku_netto_kwh=60.0, reserve_soc=10.0)

    def kwh_bei(km: float) -> float:
        # Bis km 150 hinauf auf 40 kWh, dann holt die Rekuperation bis km 300
        # zehn kWh zurück. Am Ziel stehen 30 kWh - das sähe machbar aus.
        if km <= 150.0:
            return km / 150.0 * 40.0
        return 40.0 - (km - 150.0) / 150.0 * 10.0

    profil = profil_bauen(300, kwh_bei=kwh_bei)
    soc_je_kwh = 100.0 / fz.akku_netto_kwh
    netto_noetig = fz.reserve_soc + 30.0 * soc_je_kwh
    spitze_noetig = fz.reserve_soc + 40.0 * soc_je_kwh
    pruefe(netto_noetig <= 60.0 < spitze_noetig,
           "der Fall ist so gebaut, dass nur die Bilanz am Ziel machbar aussieht",
           f"netto {netto_noetig:.0f} %, Spitze {spitze_noetig:.0f} %, Start 60 %")

    ohne = optimierer.planen(profil, [], fz, KURVE, start_soc=60.0,
                             ziel_soc=10.0, max_fahrzeug_kw=150.0)
    pruefe(not ohne.machbar,
           "ohne Ladepunkt wird die Strecke abgelehnt, nicht durchgewinkt")
    pruefe("Ladepunkt" in ohne.grund,
           "und der Grund benennt das fehlende Angebot", ohne.grund)

    plan = optimierer.planen(profil, saeulen([80], max_kw=150.0), fz, KURVE,
                             start_soc=60.0, ziel_soc=10.0,
                             max_fahrzeug_kw=150.0)
    pruefe(plan.machbar, "mit einem Ladepunkt vor dem Anstieg geht es", plan.grund)
    pruefe(len(plan.stopps) == 1, "ein Stopp genügt",
           f"{len(plan.stopps)} geplant")
    fakten = plan_pruefen(plan, profil, fz, 60.0, 10.0, "Pass")
    pruefe(fakten["tiefster_soc"] >= fz.reserve_soc - 0.5,
           "auf der Passhöhe bleibt die Reserve stehen",
           f"tiefster {fakten['tiefster_soc']:.1f} %")


def fall_redundanz():
    abschnitt("Redundanz - acht Ladepunkte schlagen einen")
    fz = Fahrzeugwerte(akku_netto_kwh=60.0, reserve_soc=10.0)
    profil = profil_bauen(450)
    optionen = [
        optimierer.Ladeoption(id=1, km_auf_route=220.0, umweg_minuten=5.0,
                              max_kw=150.0, anzahl_punkte=1, name="einzeln"),
        optimierer.Ladeoption(id=2, km_auf_route=222.0, umweg_minuten=5.0,
                              max_kw=150.0, anzahl_punkte=8, name="acht Punkte"),
    ]
    plan = optimierer.planen(profil, optionen, fz, KURVE, start_soc=90.0,
                             ziel_soc=20.0, max_fahrzeug_kw=150.0)
    gewaehlt = [s.option.id for s in plan.stopps]
    pruefe(gewaehlt == [2],
           "bei sonst gleichen Standorten gewinnt der mit mehr Ladepunkten",
           f"gewählt: {gewaehlt}")


def fall_bevorzugter_betreiber():
    abschnitt("Bevorzugter Anbieter - kein Ausschluss, nur ein Vorteil")
    fz = Fahrzeugwerte(akku_netto_kwh=60.0, reserve_soc=10.0)
    profil = profil_bauen(450)
    # Zwei Standorte an derselben Stelle der Route (z.B. zwei Ladeparks an
    # derselben Abfahrt), der bevorzugte mit drei Minuten mehr Umweg - weniger
    # als der Bonus von vier Minuten, aber genug, um ohne Vorgabe zu verlieren.
    optionen = [
        optimierer.Ladeoption(id=1, km_auf_route=220.0, umweg_minuten=4.0,
                              max_kw=150.0, anzahl_punkte=1, name="fremd",
                              betreiber="Fremdanbieter GmbH"),
        optimierer.Ladeoption(id=2, km_auf_route=220.0, umweg_minuten=7.0,
                              max_kw=150.0, anzahl_punkte=1, name="bevorzugt",
                              betreiber="EnBW mobility+"),
    ]
    ohne = optimierer.planen(profil, optionen, fz, KURVE, start_soc=90.0,
                             ziel_soc=20.0, max_fahrzeug_kw=150.0)
    pruefe([s.option.id for s in ohne.stopps] == [1],
           "ohne Vorgabe gewinnt der Standort mit dem kleineren Umweg",
           f"gewählt: {[s.option.id for s in ohne.stopps]}")

    mit = optimierer.planen(profil, optionen, fz, KURVE, start_soc=90.0,
                            ziel_soc=20.0, max_fahrzeug_kw=150.0,
                            bevorzugte_betreiber=["EnBW"])
    pruefe([s.option.id for s in mit.stopps] == [2],
           "mit Vorgabe gleicht der Bonus den grösseren Umweg aus",
           f"gewählt: {[s.option.id for s in mit.stopps]}")

    # Ein Standort mit 30 Minuten Umweg ist auch als bevorzugter Anbieter kein
    # Schnäppchen - der Bonus wiegt den Umweg auf, macht ihn aber nie gratis.
    weit = [optimierer.Ladeoption(id=3, km_auf_route=210.0, umweg_minuten=30.0,
                                  max_kw=300.0, anzahl_punkte=8,
                                  name="weit, aber bevorzugt",
                                  betreiber="EnBW mobility+"),
            optimierer.Ladeoption(id=4, km_auf_route=215.0, umweg_minuten=6.0,
                                  max_kw=150.0, anzahl_punkte=4, name="nah")]
    plan_weit = optimierer.planen(profil, weit, fz, KURVE, start_soc=90.0,
                                  ziel_soc=20.0, max_fahrzeug_kw=300.0,
                                  bevorzugte_betreiber=["EnBW"])
    pruefe([s.option.id for s in plan_weit.stopps] == [4],
           "ein 30-Minuten-Umweg bleibt draussen, auch beim bevorzugten Anbieter",
           f"gewählt: {[s.option.id for s in plan_weit.stopps]}")


def fall_ausschluesse():
    abschnitt("Was nicht in den Plan darf")
    fz = Fahrzeugwerte(akku_netto_kwh=60.0, reserve_soc=10.0)
    profil = profil_bauen(450)

    nah_teuer = [
        optimierer.Ladeoption(id=1, km_auf_route=200.0, umweg_minuten=30.0,
                              max_kw=300.0, anzahl_punkte=8, name="30 min Umweg"),
        optimierer.Ladeoption(id=2, km_auf_route=210.0, umweg_minuten=6.0,
                              max_kw=150.0, anzahl_punkte=4, name="6 min Umweg"),
    ]
    plan = optimierer.planen(profil, nah_teuer, fz, KURVE, start_soc=90.0,
                             ziel_soc=20.0, max_fahrzeug_kw=300.0)
    gewaehlt = [s.option.id for s in plan.stopps]
    pruefe(gewaehlt == [2],
           "ein Standort mit 30 Minuten Umweg fällt raus, so stark er auch ist",
           f"gewählt: {gewaehlt}")

    gemeldet = [
        optimierer.Ladeoption(id=1, km_auf_route=210.0, umweg_minuten=5.0,
                              max_kw=300.0, anzahl_punkte=8, name="belegt",
                              gesperrt=True),
        optimierer.Ladeoption(id=2, km_auf_route=215.0, umweg_minuten=5.0,
                              max_kw=150.0, anzahl_punkte=4, name="frei"),
    ]
    plan = optimierer.planen(profil, gemeldet, fz, KURVE, start_soc=90.0,
                             ziel_soc=20.0, max_fahrzeug_kw=300.0)
    gewaehlt = [s.option.id for s in plan.stopps]
    pruefe(gewaehlt == [2],
           "ein als belegt gemeldeter Standort wird nicht eingeplant",
           f"gewählt: {gewaehlt}")

    leer = optimierer.planen(profil, [], fz, KURVE, start_soc=90.0,
                             ziel_soc=20.0, max_fahrzeug_kw=150.0)
    pruefe(not leer.machbar, "ohne Kandidaten ist die Strecke nicht machbar")
    pruefe("importiert" in leer.grund,
           "und der Grund weist auf die womöglich leere Tabelle hin", leer.grund)


def fall_ausweich():
    abschnitt("Ausweichstandorte")
    fz = Fahrzeugwerte(akku_netto_kwh=60.0, reserve_soc=10.0)
    profil = profil_bauen(600)
    plan = optimierer.planen(profil, saeulen(range(40, 600, 20)), fz, KURVE,
                             start_soc=90.0, ziel_soc=20.0,
                             max_fahrzeug_kw=150.0)
    pruefe(plan.machbar, "machbar", plan.grund)
    mit_ausweich = [s for s in plan.stopps if s.ausweich]
    pruefe(bool(mit_ausweich),
           "bei dicht stehenden Säulen hat mindestens ein Stopp einen Ausweichstandort",
           f"{len(mit_ausweich)} von {len(plan.stopps)}")

    soc_je_kwh = 100.0 / fz.akku_netto_kwh
    stimmig = True
    for stopp in mit_ausweich:
        ausweich = stopp.ausweich
        pruefe_km = ausweich["km_auf_route"] > stopp.option.km_auf_route
        bedarf = (profil.wert(profil.kwh, ausweich["km_auf_route"])
                  - profil.wert(profil.kwh, stopp.option.km_auf_route)) * soc_je_kwh
        # Ohne Nachladen erreichbar heisst: mit dem Ladestand bei Ankunft, und
        # dabei darf höchstens die halbe Reserve angebrochen werden.
        if not pruefe_km or stopp.ankunft_soc - bedarf < fz.reserve_soc / 2.0 - 0.5:
            stimmig = False
    pruefe(stimmig,
           "jeder Ausweichstandort liegt voraus und ist ohne Nachladen erreichbar")


def fall_dichte_saeulen():
    abschnitt("Dichte Säulen und hoher Verbrauch - keine Alibi-Stopps")
    # Der Fall, in dem der Mindestladehub gebraucht wird: viele erreichbare
    # Säulen, hoher Verbrauch, und darunter grosse Ladeparks mit kräftigem
    # Redundanzbonus. Solange der Bonus Umweg *und* Ladezeit aufwiegen durfte
    # und die Nachoptimierung den Mindesthub nicht kannte, entstand hier ein
    # Halt über fünf Prozentpunkte in anderthalb Minuten - rechnerisch billig,
    # in Wirklichkeit ein Umweg für nichts.
    fz = Fahrzeugwerte(akku_netto_kwh=60.0, reserve_soc=10.0)
    profil = profil_bauen(577, kwh_je_km=0.257)
    muster = [(150, 4), (300, 8), (350, 12), (150, 6)]
    optionen = []
    for i, km in enumerate(range(35, 577, 35)):
        kw, punkte = muster[i % len(muster)]
        optionen.append(optimierer.Ladeoption(
            id=i + 1, km_auf_route=float(km), umweg_minuten=5.1,
            max_kw=float(kw), anzahl_punkte=punkte, name=f"Rasthof km {km}"))

    plan = optimierer.planen(profil, optionen, fz, KURVE, start_soc=65.0,
                             ziel_soc=20.0, max_fahrzeug_kw=350.0)
    pruefe(plan.machbar, "machbar", plan.grund)
    pruefe(len(plan.stopps) >= 6, "die Strecke braucht viele Stopps",
           f"{len(plan.stopps)}")
    plan_pruefen(plan, profil, fz, 65.0, 20.0, "Dichte Säulen")

    kurz = [s for s in plan.stopps if s.ladezeit_minuten < 2.0]
    pruefe(not kurz,
           "kein Stopp dauert unter zwei Minuten - dafür lohnt kein Abstecher",
           str([(s.option.name, s.ladezeit_minuten) for s in kurz]))


def fall_laufzeit():
    abschnitt("Laufzeit")
    fz = Fahrzeugwerte(akku_netto_kwh=77.0, reserve_soc=10.0)
    profil = profil_bauen(900)
    # 180 Kandidaten, wie sie eine echte Korridorsuche entlang einer
    # Langstrecke liefert. Der Optimierer dünnt sie selbst aus.
    optionen = saeulen(range(20, 900, 5), max_kw=150.0, anzahl_punkte=4)
    begonnen = time.perf_counter()
    plan = optimierer.planen(profil, optionen, fz, KURVE, start_soc=90.0,
                             ziel_soc=20.0, max_fahrzeug_kw=150.0)
    dauer = time.perf_counter() - begonnen
    pruefe(plan.machbar, "900 km mit 176 Kandidaten sind planbar", plan.grund)
    pruefe(dauer < 2.0, "und zwar in unter zwei Sekunden",
           f"gebraucht: {dauer:.2f} s")
    pruefe(plan.gepruefte_kandidaten <= optimierer.HOECHSTENS_KANDIDATEN,
           "die Kandidaten werden auf die Obergrenze ausgedünnt",
           f"{plan.gepruefte_kandidaten} übrig")
    plan_pruefen(plan, profil, fz, 90.0, 20.0, "Laufzeit")
    print(f"        ({dauer * 1000:.0f} ms, {len(plan.stopps)} Stopps, "
          f"{plan.gesamt_minuten:.0f} min gesamt)")


def main() -> int:
    fall_kurze_strecke()
    fall_langstrecke()
    fall_starke_saeule_gewinnt()
    fall_luecke_vorher_fuellen()
    fall_pass()
    fall_redundanz()
    fall_bevorzugter_betreiber()
    fall_ausschluesse()
    fall_ausweich()
    fall_dichte_saeulen()
    fall_laufzeit()

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
