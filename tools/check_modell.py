#!/usr/bin/env python3
"""Prüft das Verbrauchsmodell an Fällen, deren Ergebnis man vorher kennt.

Kein Testframework, keine Netzverbindung, keine Datenbank - `python3
tools/check_modell.py` genügt. Geprüft wird nicht auf exakte Zahlen (die
hängen an Parametern, die sich ändern dürfen), sondern auf die Verhältnisse,
die physikalisch gelten müssen. Genau die sind es, deren Verletzung einen
unterwegs an der falschen Säule stehen lässt.

    ./tools/check_modell.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pruefen import Pruefung, anwendung_bereitstellen  # noqa: E402

# Dieses Skript rührt keine Datenbank an - es rechnet nur Physik.
anwendung_bereitstellen("modell", datenbank=False)

from app.energie.modell import (Fahrzeugwerte, Umgebung, hvac_leistung_w,  # noqa: E402
                                luftdichte, profil_rechnen)
from app.laden.kurven import ladezeit_minuten, leistung_bei  # noqa: E402

pruefe = Pruefung()


def gerade_strecke(km: float, hoehe_ende_m: float = 0.0, punkte_je_km: int = 1):
    """Eine synthetische Route: gerade nach Norden, linear ansteigend.

    Ein Grad Breite sind rund 111,32 km - daraus lässt sich eine Strecke
    beliebiger Länge exakt konstruieren, ohne ein Routing zu brauchen.
    """
    anzahl = max(2, int(km * punkte_je_km))
    punkte = []
    for i in range(anzahl + 1):
        t = i / anzahl
        punkte.append([10.0, 50.0 + (km * t) / 111.32, hoehe_ende_m * t])
    return punkte, [0.0] * anzahl


def pass_strecke(km: float, gipfel_m: float, punkte_je_km: int = 1):
    """Hinauf und wieder hinunter: dieselbe Höhe am Anfang und am Ende."""
    anzahl = max(2, int(km * punkte_je_km))
    if anzahl % 2:
        anzahl += 1
    punkte = []
    for i in range(anzahl + 1):
        t = i / anzahl
        hoehe = gipfel_m * (2 * t if t <= 0.5 else 2 * (1 - t))
        punkte.append([10.0, 50.0 + (km * t) / 111.32, hoehe])
    return punkte, [0.0] * anzahl


def tempo(kmh: float, anzahl: int):
    return [kmh / 3.6] * anzahl


def lauf(fz, km, kmh, hoehe=0.0, temp_c=20.0, start_soc=100.0, wind_ms=0.0,
         wind_grad=0.0):
    punkte, _ = gerade_strecke(km, hoehe)
    umgebung = Umgebung(temp_c=temp_c, windgeschwindigkeit_ms=wind_ms,
                        windrichtung_grad=wind_grad)
    return profil_rechnen(fz, punkte, tempo(kmh, len(punkte) - 1), start_soc,
                          lambda lat, lon: umgebung)


def main() -> int:
    fz = Fahrzeugwerte()      # 1950 kg, c_w 0.28, 2.3 m², 60 kWh netto

    pruefe.abschnitt("Luftdichte")
    pruefe(abs(luftdichte(15.0, 0.0) - 1.225) < 0.01,
           "bei 15 °C auf Meereshöhe rund 1,225 kg/m³",
           f"ist {luftdichte(15.0, 0.0):.3f}")
    pruefe(luftdichte(-5.0, 0.0) > luftdichte(20.0, 0.0) * 1.07,
           "kalte Luft ist mindestens 7 % dichter als warme")
    pruefe(luftdichte(15.0, 1500.0) < luftdichte(15.0, 0.0) * 0.87,
           "auf 1500 m mindestens 13 % dünner")

    print("\nHeizung und Klima")
    pruefe(hvac_leistung_w(20.0) < 300.0, "bei 20 °C fast nichts")
    pruefe(2000.0 < hvac_leistung_w(-5.0, waermepumpe=True) < 4500.0,
           "bei -5 °C mit Wärmepumpe zwischen 2 und 4,5 kW",
           f"ist {hvac_leistung_w(-5.0, True):.0f} W")
    pruefe(hvac_leistung_w(-5.0, False) > hvac_leistung_w(-5.0, True) * 1.5,
           "ohne Wärmepumpe deutlich mehr")

    print("\nGrundfall: 100 km eben, 20 °C, 120 km/h")
    eben = lauf(fz, 100.0, 120.0)
    pruefe(15.0 < eben.verbrauch_kwh_100km < 26.0,
           "Verbrauch im plausiblen Bereich 15-26 kWh/100 km",
           f"ist {eben.verbrauch_kwh_100km}")
    pruefe(abs(eben.strecke_km - 100.0) < 1.0,
           "Streckenlänge stimmt", f"ist {eben.strecke_km}")
    pruefe(abs(eben.minuten - 50.0) < 1.0,
           "Fahrzeit rund 50 Minuten", f"ist {eben.minuten}")

    print("\nTempo geht quadratisch ein")
    langsam = lauf(fz, 100.0, 110.0)
    schnell = lauf(fz, 100.0, 130.0)
    pruefe(schnell.kwh_gesamt > langsam.kwh_gesamt * 1.10,
           "130 km/h braucht über 10 % mehr als 110 km/h",
           f"{langsam.kwh_gesamt} -> {schnell.kwh_gesamt} kWh")
    # Bei reinem Luftwiderstand waeren es (130/110)^2 = 1,40. Roll- und
    # Nebenverbrauch daempfen das, deshalb muss der Zuwachs darunter liegen.
    pruefe(schnell.kwh_gesamt < langsam.kwh_gesamt * 1.40,
           "aber weniger als der reine v²-Faktor von 1,40")

    print("\nSteigung")
    berg = lauf(fz, 100.0, 120.0, hoehe=800.0)
    hub_kwh = fz.masse_kg * 9.80665 * 800.0 / 3.6e6 / fz.eta_antrieb
    zuwachs = berg.kwh_gesamt - eben.kwh_gesamt
    pruefe(zuwachs > 0, "800 Höhenmeter kosten Energie",
           f"+{zuwachs:.2f} kWh")
    pruefe(abs(zuwachs - hub_kwh) < hub_kwh * 0.25,
           "und zwar ungefähr die Hubarbeit geteilt durch den Wirkungsgrad",
           f"erwartet ~{hub_kwh:.2f} kWh, gemessen {zuwachs:.2f} kWh")

    print("\nPass: hinauf und wieder hinunter")
    hinauf = lauf(fz, 50.0, 100.0, hoehe=600.0)
    hinab = lauf(fz, 50.0, 100.0, hoehe=-600.0)
    pruefe(hinab.kwh_gesamt < hinauf.kwh_gesamt,
           "bergab weniger als bergauf")

    # 1200 m auf 25 km sind knapp 5 % Steigung. Erst ab dieser Grössenordnung
    # übersteigt die Hangabtriebskraft den Fahrwiderstand, sodass es bergab
    # überhaupt etwas zu rekuperieren gibt. Bei sanftem Gefälle zieht das Auto
    # weiterhin - dort ist ein Pass tatsächlich ein Nullsummenspiel, und das
    # Modell sagt das zu Recht.
    pass_punkte, _ = pass_strecke(50.0, 1200.0)
    umgebung = Umgebung(temp_c=20.0)
    ueber_den_pass = profil_rechnen(fz, pass_punkte,
                                    tempo(70.0, len(pass_punkte) - 1), 100.0,
                                    lambda lat, lon: umgebung)
    durch_die_ebene = lauf(fz, 50.0, 70.0)
    pruefe(ueber_den_pass.kwh_gesamt > durch_die_ebene.kwh_gesamt * 1.2,
           "über den Pass kostet über 20 % mehr als die Ebene, obwohl man "
           "wieder auf Ausgangshöhe ankommt - Rekuperation holt nur ~70 % zurück",
           f"{ueber_den_pass.kwh_gesamt:.2f} kWh gegen "
           f"{durch_die_ebene.kwh_gesamt:.2f} kWh")
    pruefe(abs(ueber_den_pass.punkte[-1].hoehe_m) < 1.0,
           "und der Pass endet wirklich wieder auf null Metern")

    print("\nKälte")
    warm = lauf(fz, 200.0, 120.0, temp_c=20.0)
    kalt = lauf(fz, 200.0, 120.0, temp_c=-5.0)
    pruefe(kalt.kwh_gesamt > warm.kwh_gesamt * 1.12,
           "bei -5 °C mindestens 12 % mehr als bei 20 °C",
           f"{warm.kwh_gesamt} -> {kalt.kwh_gesamt} kWh")

    print("\nWind")
    gegen = lauf(fz, 100.0, 120.0, wind_ms=10.0, wind_grad=0.0)    # aus Norden
    ruecken = lauf(fz, 100.0, 120.0, wind_ms=10.0, wind_grad=180.0)
    pruefe(gegen.kwh_gesamt > eben.kwh_gesamt > ruecken.kwh_gesamt,
           "Gegenwind kostet, Rückenwind spart - die Route führt nach Norden",
           f"{gegen.kwh_gesamt} / {eben.kwh_gesamt} / {ruecken.kwh_gesamt} kWh")

    print("\nReserve-Marke")
    weit = lauf(fz, 500.0, 120.0, start_soc=80.0)
    pruefe(weit.reserve_bei_km is not None,
           "60-kWh-Auto schafft 500 km bei 80 % nicht ohne Nachladen")
    erwartet_km = (80.0 - fz.reserve_soc) / 100.0 * fz.akku_netto_kwh \
        / eben.verbrauch_kwh_100km * 100.0
    pruefe(abs((weit.reserve_bei_km or 0) - erwartet_km) < erwartet_km * 0.15,
           "Reserve wird ungefähr dort erreicht, wo die Überschlagsrechnung sagt",
           f"erwartet ~{erwartet_km:.0f} km, gerechnet {weit.reserve_bei_km} km")

    kurz = lauf(fz, 80.0, 120.0, start_soc=80.0)
    pruefe(kurz.reserve_bei_km is None,
           "80 km bei 80 % greifen die Reserve nicht an")

    print("\nAnhänger: eigene Masse, eigene Luftwiderstandsfläche")
    gespann = Fahrzeugwerte(**{**fz.__dict__, "masse_kg": fz.masse_kg + 1300.0,
                               "cwa_zusatz_m2": 1.1})
    mit_anhaenger = lauf(gespann, 100.0, 100.0)
    ohne_anhaenger = lauf(fz, 100.0, 100.0)
    pruefe(mit_anhaenger.kwh_gesamt > ohne_anhaenger.kwh_gesamt * 1.30,
           "ein Wohnwagen kostet bei 100 km/h mindestens ein Drittel mehr",
           f"{ohne_anhaenger.kwh_gesamt} -> {mit_anhaenger.kwh_gesamt} kWh")
    nur_masse = Fahrzeugwerte(**{**fz.__dict__, "masse_kg": fz.masse_kg + 1300.0})
    nur_flaeche = Fahrzeugwerte(**{**fz.__dict__, "cwa_zusatz_m2": 1.1})
    pruefe(lauf(nur_flaeche, 100.0, 130.0).kwh_gesamt
           - lauf(nur_flaeche, 100.0, 100.0).kwh_gesamt
           > (lauf(nur_masse, 100.0, 130.0).kwh_gesamt
              - lauf(nur_masse, 100.0, 100.0).kwh_gesamt) * 1.5,
           "die Fläche wirkt mit v², die Masse nicht - schneller fahren "
           "bestraft den Luftwiderstand des Anhängers, nicht sein Gewicht")
    berg_ohne = lauf(fz, 100.0, 100.0, hoehe=600.0)
    berg_mit = lauf(nur_masse, 100.0, 100.0, hoehe=600.0)
    pruefe(berg_mit.kwh_gesamt - lauf(nur_masse, 100.0, 100.0).kwh_gesamt
           > (berg_ohne.kwh_gesamt - ohne_anhaenger.kwh_gesamt) * 1.4,
           "und umgekehrt: Die Masse kostet am Berg, die Fläche kaum")

    class FzStub:
        leermasse_kg, zuladung_kg, masse_kg = 1800.0, 150.0, 1950.0
        c_w, stirnflaeche_m2, c_rr = 0.28, 2.3, 0.010
        eta_antrieb, eta_rekup, p_neben_w = 0.88, 0.70, 350.0
        waermepumpe, akku_netto_kwh, reserve_soc = True, 60.0, 10.0
        korrekturfaktor = 1.0
        max_tempo_kmh = None

    class FahrtStub:
        def __init__(self, **kw):
            self.fahrzeug = FzStub()
            self.zuladung_kg = None
            self.luftwiderstand_faktor = 1.0
            self.__dict__.update(kw)

    w = Fahrzeugwerte.aus_fahrt(FahrtStub(anhaenger_kg=1300.0,
                                          anhaenger_cwa_m2=1.1))
    pruefe(abs(w.masse_kg - 3250.0) < 1e-9 and abs(w.cwa_zusatz_m2 - 1.1) < 1e-9,
           "aus_fahrt addiert die Masse des Anhängers und merkt sich seine Fläche",
           f"{w.masse_kg} kg, {w.cwa_zusatz_m2} m²")
    pruefe(abs(w.c_w - 0.28) < 1e-9,
           "der cw-Wert des Autos bleibt unberührt - der Anhänger ist keine "
           "Verbiegung des Fahrzeugs")
    pruefe(Fahrzeugwerte.aus_fahrt(FahrtStub()).cwa_zusatz_m2 == 0.0,
           "ohne Anhänger ändert sich nichts")

    print("\nHöchstgeschwindigkeit")
    gedeckelt = Fahrzeugwerte(**{**fz.__dict__, "tempo_max_ms": 100.0 / 3.6})
    a = lauf(gedeckelt, 100.0, 130.0)
    b = lauf(fz, 100.0, 100.0)
    pruefe(abs(a.kwh_gesamt - b.kwh_gesamt) < 0.05,
           "bei 130 km/h Annahme und 100 km/h Grenze wird mit 100 gerechnet",
           f"{a.kwh_gesamt} gegen {b.kwh_gesamt} kWh")
    pruefe(all(p.tempo_kmh <= 100.1 for p in a.punkte),
           "und das Profil führt nirgends mehr als 100 km/h")
    pruefe(abs(a.minuten - b.minuten) < 0.2,
           "die Fahrzeit folgt der Grenze", f"{a.minuten} gegen {b.minuten} min")
    pruefe(a.minuten_ohne_deckel < a.minuten * 0.80,
           "ohne Grenze wäre sie deutlich kürzer - daraus lässt sich ablesen, "
           "wie sehr die Grenze die Fahrt verlängert",
           f"{a.minuten_ohne_deckel} gegen {a.minuten} min")
    unter = lauf(gedeckelt, 100.0, 80.0)
    pruefe(abs(unter.kwh_gesamt - lauf(fz, 100.0, 80.0).kwh_gesamt) < 0.01,
           "wer ohnehin langsamer fährt, merkt von der Grenze nichts")
    frei_punkte, _ = gerade_strecke(100.0, 0.0)
    frei = profil_rechnen(gedeckelt, frei_punkte,
                          tempo(130.0, len(frei_punkte) - 1), 100.0,
                          lambda lat, lon: Umgebung(temp_c=20.0),
                          tempo_deckel=False)
    pruefe(abs(frei.kwh_gesamt - lauf(fz, 100.0, 130.0).kwh_gesamt) < 0.05,
           "mit tempo_deckel=False bleibt gefahrenes Tempo gefahrenes Tempo - "
           "eine Aufzeichnung wird nicht nachträglich zurechtgestutzt")

    w = Fahrzeugwerte.aus_fahrt(FahrtStub(tempo_max_kmh=100.0))
    pruefe(abs(w.tempo_max_ms * 3.6 - 100.0) < 1e-6,
           "die Grenze einer Fahrt kommt in den Fahrzeugwerten an")
    FzStub.max_tempo_kmh = 145.0
    w = Fahrzeugwerte.aus_fahrt(FahrtStub())
    pruefe(abs(w.tempo_max_ms * 3.6 - 145.0) < 1e-6,
           "die Grenze des Fahrzeugs gilt auch ohne Grenze der Fahrt")
    w = Fahrzeugwerte.aus_fahrt(FahrtStub(tempo_max_kmh=100.0))
    pruefe(abs(w.tempo_max_ms * 3.6 - 100.0) < 1e-6,
           "sind beide gesetzt, gilt die kleinere (Gespann am Auto mit 145)")
    w = Fahrzeugwerte.aus_fahrt(FahrtStub(tempo_max_kmh=180.0))
    pruefe(abs(w.tempo_max_ms * 3.6 - 145.0) < 1e-6,
           "und auch umgekehrt: Die Fahrt kann das Auto nicht schneller machen")
    FzStub.max_tempo_kmh = None

    print("\nLadekurve")
    kurve = [(0, 110), (10, 120), (30, 120), (50, 90), (70, 60), (80, 45),
             (90, 28), (100, 8)]
    pruefe(abs(leistung_bei(kurve, 20.0) - 120.0) < 1.0,
           "bei 20 % SoC volle 120 kW", f"ist {leistung_bei(kurve, 20.0):.1f}")
    pruefe(abs(leistung_bei(kurve, 60.0) - 75.0) < 1.0,
           "bei 60 % interpoliert auf 75 kW",
           f"ist {leistung_bei(kurve, 60.0):.1f}")
    pruefe(leistung_bei(kurve, 20.0, max_saeule_kw=50.0) == 50.0,
           "eine 50-kW-Säule begrenzt die Kurve")

    unten = ladezeit_minuten(kurve, 10.0, 55.0, 60.0)
    oben = ladezeit_minuten(kurve, 55.0, 100.0, 60.0)
    pruefe(oben > unten * 2,
           "die oberen 45 Prozentpunkte dauern über doppelt so lang wie die "
           "unteren - der Grund für zwei kurze statt einem langen Stopp",
           f"{unten:.0f} min gegen {oben:.0f} min")
    pruefe(ladezeit_minuten(kurve, 50.0, 50.0, 60.0) == 0.0,
           "kein Ladehub, keine Zeit")

    return pruefe.bilanz()


if __name__ == "__main__":
    sys.exit(main())
