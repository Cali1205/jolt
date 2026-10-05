#!/usr/bin/env python3
"""Prüft, wie aus gefahrenen Strecken Routenkandidaten werden - `routing/eigene.py`.

Das Modul kennt weder Datenbank noch Netz: Es bekommt Punktlisten und gibt
Punktlisten zurück. Deshalb lässt sich hier vollständig prüfen, was sonst nur
auf einer echten Strecke auffiele - etwa dass ein Ladeplatz neben der
Autobahn nicht als Zwischenpunkt in der Route landet und die Route in einen
Abstecher zwingt.

Ohne Netz, ohne Postgres, ohne API-Schlüssel:

    ./tools/check_eigene.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pruefen import Pruefung, anwendung_bereitstellen  # noqa: E402

anwendung_bereitstellen("eigene", datenbank=False)

from app.geo import haversine_m                      # noqa: E402
from app.routing import eigene                       # noqa: E402

pruefe = Pruefung()

# Eine gerade Strecke nach Norden: 6 Grad Breite sind rund 667 km.
LON = 9.0
BREITE0 = 48.0


def strecke(breite_von=BREITE0, breite_bis=BREITE0 + 6.0, schritt=0.01,
            tempo=100.0, ladestopp_bei=None):
    """Pfad mit einem Punkt je ~1,1 km. `ladestopp_bei`: Breite, bei der die
    Fahrt abbiegt, steht (Tempo 0) und wieder zurückkehrt - der Ladeplatz liegt
    drei Kilometer abseits der Strasse."""
    pfad = []
    b = breite_von
    while b <= breite_bis + 1e-9:
        pfad.append((round(b, 5), LON, tempo))
        if ladestopp_bei is not None and abs(b - ladestopp_bei) < schritt / 2:
            for k in range(30):
                pfad.append((round(b, 5), LON + 0.04, 0.0))
        b += schritt
    return pfad


def main() -> int:
    pfad = strecke()
    start = (BREITE0, LON)
    ziel = (BREITE0 + 6.0, LON)

    pruefe.abschnitt("Radius")
    pruefe(eigene.radius_km(1000.0) == eigene.RADIUS_MAX_KM,
           "auf einer sehr langen Strecke gilt die Obergrenze",
           str(eigene.radius_km(1000.0)))
    pruefe(eigene.radius_km(500.0) >= 15.7,
           "und schon auf 500 km reicht der Radius für die Aufzeichnung vom "
           "4.9., die 15,7 km nach dem geplanten Start begann - "
           "Aufzeichnungen beginnen nach der Abfahrt, nicht davor",
           str(eigene.radius_km(500.0)))
    pruefe(eigene.radius_km(30.0) == eigene.RADIUS_MIN_KM,
           "auf einer kurzen die Untergrenze - zwei Stadtteile sind nicht "
           "dasselbe Ziel", str(eigene.radius_km(30.0)))

    pruefe.abschnitt("Abschnitt finden")
    a = eigene.passender_abschnitt(pfad, start, ziel)
    pruefe(a is not None and not a.gegenrichtung,
           "dieselbe Strecke vorwärts passt")
    pruefe(a is not None and abs(a.laenge_km - 667) < 15,
           "und ist so lang wie sie ist", str(a and a.laenge_km))

    r = eigene.passender_abschnitt(pfad, ziel, start)
    pruefe(r is not None and r.gegenrichtung,
           "andersherum gefahren passt auch - wer Gueugnon-Reutlingen kennt, "
           "kennt Reutlingen-Gueugnon")
    pruefe(r is not None and haversine_m(r.punkte[0][0], r.punkte[0][1],
                                         ziel[0], ziel[1]) < 2000,
           "und der Abschnitt beginnt dann beim Start der Anfrage, nicht des "
           "alten Pfads")

    t = eigene.passender_abschnitt(pfad, (BREITE0 + 1.0, LON), (BREITE0 + 4.0, LON))
    pruefe(t is not None and abs(t.laenge_km - 333) < 10,
           "ein Teilstück genügt: Wer weiter gefahren ist, kennt auch die "
           "kürzere Strecke", str(t and t.laenge_km))

    pruefe(eigene.passender_abschnitt(
        pfad, (BREITE0, LON + 0.8), ziel) is None,
           "liegt der Start 55 km neben dem Pfad, ist es eine andere Strecke")
    pruefe(eigene.passender_abschnitt(
        pfad, (BREITE0, LON + 0.1), (BREITE0 + 6.0, LON + 0.1)) is not None,
           "7 km daneben sind auf 667 km noch dasselbe")
    pruefe(eigene.passender_abschnitt(pfad, start, start) is None,
           "Start gleich Ziel ist keine Strecke")
    pruefe(eigene.passender_abschnitt(pfad[:1], start, ziel) is None,
           "ein einzelner Punkt ist kein Pfad")
    pruefe(eigene.passender_abschnitt([], start, ziel) is None,
           "und ein leerer erst recht")

    pruefe.abschnitt("Zwischenpunkte")
    z = eigene.zwischenpunkte(a)
    pruefe(5 <= len(z) <= eigene.MAX_ZWISCHENPUNKTE,
           f"auf 667 km eine Handvoll, nie mehr als {eigene.MAX_ZWISCHENPUNKTE}",
           str(len(z)))
    abstaende = [haversine_m(p[0][0], p[0][1], p[1][0], p[1][1])
                 for p in zip(z, z[1:])]
    mittel = sum(abstaende) / len(abstaende)
    pruefe(all(0.6 * mittel < d < 1.4 * mittel for d in abstaende),
           "gleichmässig verteilt - bei zu vielen Punkten wird der Abstand "
           "grösser, statt hinten abzubrechen",
           f"{min(abstaende) / 1000:.0f} bis {max(abstaende) / 1000:.0f} km")
    pruefe(all(p[0] > BREITE0 and p[0] < BREITE0 + 6.0 for p in z),
           "weder Start noch Ziel selbst sind dabei")
    pruefe(haversine_m(z[-1][0], z[-1][1], ziel[0], ziel[1]) / 1000 > 12,
           "und kein Punkt kurz vor dem Ziel - dort fährt die Route ohnehin hin",
           f"{haversine_m(z[-1][0], z[-1][1], ziel[0], ziel[1]) / 1000:.1f} km")

    kurz = eigene.passender_abschnitt(strecke(BREITE0, BREITE0 + 1.0),
                                      start, (BREITE0 + 1.0, LON))
    zk = eigene.zwischenpunkte(kurz)
    pruefe(len(zk) <= 3 and len(zk) >= 1,
           "auf 111 km nur wenige - bei 25 km Abstand also drei", str(len(zk)))

    pruefe.abschnitt("Ladeplätze und Pausen")
    # Der Ladeplatz liegt so, dass seine Stelle entlang des Pfads genau auf
    # einen Zwischenpunkt faellt: Abzweig bei 22 km, drei Kilometer abseits,
    # Pfadstrecke dort 25 km - und der Abstand der Zwischenpunkte ist 25 km.
    mit_stopp = strecke(ladestopp_bei=BREITE0 + 0.198)
    am = eigene.passender_abschnitt(mit_stopp, start, ziel)
    zs = eigene.zwischenpunkte(am, abstand_km=25.0, maximal=40)
    abseits = [p for p in zs if abs(p[1] - LON) >= 0.02]
    pruefe(am is not None and len(zs) >= 10, "mit Ladestopp gibt es weiter Kandidaten")
    pruefe(not abseits,
           "kein Zwischenpunkt auf dem Ladeplatz - er laege 3 km neben der "
           "Strasse und zwaenge die Route zu einem Abstecher", str(abseits))
    # Gegenprobe: Ohne die Tempo-Filterung wuerde genau dieser Punkt gewaehlt.
    # Sonst prueft der Test oben nichts.
    gespeichert = eigene.MINDEST_TEMPO_KMH
    eigene.MINDEST_TEMPO_KMH = -1.0
    try:
        ohne_filter = [p for p in eigene.zwischenpunkte(am, abstand_km=25.0, maximal=40)
                       if abs(p[1] - LON) >= 0.02]
    finally:
        eigene.MINDEST_TEMPO_KMH = gespeichert
    pruefe(bool(ohne_filter),
           "und der Test ist scharf: Ohne die Tempo-Grenze landet der "
           "Ladeplatz tatsaechlich in der Liste")

    ohne_tempo = [(b, lo, None) for b, lo, _ in pfad]
    zo = eigene.zwischenpunkte(eigene.passender_abschnitt(ohne_tempo, start, ziel))
    pruefe(len(zo) >= 5,
           "ein Pfad ohne Tempoangabe ist trotzdem brauchbar - manche Quellen "
           "liefern keines", str(len(zo)))

    nur_stand = [(b, lo, 0.0) for b, lo, _ in pfad]
    pruefe(eigene.zwischenpunkte(eigene.passender_abschnitt(nur_stand, start, ziel)) == [],
           "ein Pfad, auf dem nie gefahren wurde, liefert keinen Kandidaten")

    pruefe.abschnitt("Eingabe")
    pruefe(eigene.pfad_aus_messpunkten([(1.0, 2.0, 50.0), (None, 2.0, 50.0),
                                        (1.0, None, 50.0)])
           == [(1.0, 2.0, 50.0)],
           "Messpunkte ohne Koordinate fallen heraus")

    return pruefe.bilanz()


if __name__ == "__main__":
    sys.exit(main())
