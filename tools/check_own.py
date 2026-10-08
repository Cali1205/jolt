#!/usr/bin/env python3
"""Prüft, wie aus gefahrenen Strecken Routenkandidaten werden - `routing/own.py`.

Das Modul kennt weder Datenbank noch Netz: Es bekommt Punktlisten und gibt
Punktlisten zurück. Deshalb lässt sich hier vollständig prüfen, was sonst nur
auf einer echten Strecke auffiele - etwa dass ein Ladeplatz neben der
Autobahn nicht als Zwischenpunkt in der Route landet und die Route in einen
Abstecher zwingt.

Ohne Netz, ohne Postgres, ohne API-Schlüssel:

    ./tools/check_own.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from examine import Check, application_provide  # noqa: E402

application_provide("eigene", db_name=False)

from app.geo import haversine_m                      # noqa: E402
from app.routing import own                       # noqa: E402

verify = Check()

# Eine gerade Strecke nach Norden: 6 Grad Breite sind rund 667 km.
LON = 9.0
BREITE0 = 48.0


def distance(width_from=BREITE0, width_until=BREITE0 + 6.0, step=0.01,
            velocity=100.0, charge_stop_at=None):
    """Pfad mit einem Punkt je ~1,1 km. `charge_stop_at`: Breite, bei der die
    Fahrt abbiegt, steht (Tempo 0) und wieder zurückkehrt - der Ladeplatz liegt
    drei Kilometer abseits der Strasse."""
    fs_path = []
    b = width_from
    while b <= width_until + 1e-9:
        fs_path.append((round(b, 5), LON, velocity))
        if charge_stop_at is not None and abs(b - charge_stop_at) < step / 2:
            for k in range(30):
                fs_path.append((round(b, 5), LON + 0.04, 0.0))
        b += step
    return fs_path


def main() -> int:
    fs_path = distance()
    start = (BREITE0, LON)
    destination = (BREITE0 + 6.0, LON)

    verify.section("Radius")
    verify(own.radius_km(1000.0) == own.RADIUS_MAX_KM,
           "auf einer sehr langen Strecke gilt die Obergrenze",
           str(own.radius_km(1000.0)))
    verify(own.radius_km(500.0) >= 15.7,
           "und schon auf 500 km reicht der Radius für die Aufzeichnung vom "
           "4.9., die 15,7 km nach dem geplanten Start begann - "
           "Aufzeichnungen beginnen nach der Abfahrt, nicht davor",
           str(own.radius_km(500.0)))
    verify(own.radius_km(30.0) == own.RADIUS_MIN_KM,
           "auf einer kurzen die Untergrenze - zwei Stadtteile sind nicht "
           "dasselbe Ziel", str(own.radius_km(30.0)))

    verify.section("Abschnitt finden")
    a = own.fitting_section(fs_path, start, destination)
    verify(a is not None and not a.opposite,
           "dieselbe Strecke vorwärts passt")
    verify(a is not None and abs(a.length_km - 667) < 15,
           "und ist so lang wie sie ist", str(a and a.length_km))

    r = own.fitting_section(fs_path, destination, start)
    verify(r is not None and r.opposite,
           "andersherum gefahren passt auch - wer Gueugnon-Reutlingen kennt, "
           "kennt Reutlingen-Gueugnon")
    verify(r is not None and haversine_m(r.points[0][0], r.points[0][1],
                                         destination[0], destination[1]) < 2000,
           "und der Abschnitt beginnt dann beim Start der Anfrage, nicht des "
           "alten Pfads")

    t = own.fitting_section(fs_path, (BREITE0 + 1.0, LON), (BREITE0 + 4.0, LON))
    verify(t is not None and abs(t.length_km - 333) < 10,
           "ein Teilstück genügt: Wer weiter gefahren ist, kennt auch die "
           "kürzere Strecke", str(t and t.length_km))

    verify(own.fitting_section(
        fs_path, (BREITE0, LON + 0.8), destination) is None,
           "liegt der Start 55 km neben dem Pfad, ist es eine andere Strecke")
    verify(own.fitting_section(
        fs_path, (BREITE0, LON + 0.1), (BREITE0 + 6.0, LON + 0.1)) is not None,
           "7 km daneben sind auf 667 km noch dasselbe")
    verify(own.fitting_section(fs_path, start, start) is None,
           "Start gleich Ziel ist keine Strecke")
    verify(own.fitting_section(fs_path[:1], start, destination) is None,
           "ein einzelner Punkt ist kein Pfad")
    verify(own.fitting_section([], start, destination) is None,
           "und ein leerer erst recht")

    verify.section("Zwischenpunkte")
    z = own.waypoints(a)
    verify(5 <= len(z) <= own.MAX_WAYPOINTS,
           f"auf 667 km eine Handvoll, nie mehr als {own.MAX_WAYPOINTS}",
           str(len(z)))
    distances = [haversine_m(p[0][0], p[0][1], p[1][0], p[1][1])
                 for p in zip(z, z[1:])]
    avg = sum(distances) / len(distances)
    verify(all(0.6 * avg < d < 1.4 * avg for d in distances),
           "gleichmässig verteilt - bei zu vielen Punkten wird der Abstand "
           "grösser, statt hinten abzubrechen",
           f"{min(distances) / 1000:.0f} bis {max(distances) / 1000:.0f} km")
    verify(all(p[0] > BREITE0 and p[0] < BREITE0 + 6.0 for p in z),
           "weder Start noch Ziel selbst sind dabei")
    verify(haversine_m(z[-1][0], z[-1][1], destination[0], destination[1]) / 1000 > 12,
           "und kein Punkt kurz vor dem Ziel - dort fährt die Route ohnehin hin",
           f"{haversine_m(z[-1][0], z[-1][1], destination[0], destination[1]) / 1000:.1f} km")

    short = own.fitting_section(distance(BREITE0, BREITE0 + 1.0),
                                      start, (BREITE0 + 1.0, LON))
    zk = own.waypoints(short)
    verify(len(zk) <= 3 and len(zk) >= 1,
           "auf 111 km nur wenige - bei 25 km Abstand also drei", str(len(zk)))

    verify.section("Ladeplätze und Pausen")
    # Der Ladeplatz liegt so, dass seine Stelle entlang des Pfads genau auf
    # einen Zwischenpunkt faellt: Abzweig bei 22 km, drei Kilometer abseits,
    # Pfadstrecke dort 25 km - und der Abstand der Zwischenpunkte ist 25 km.
    with_stop = distance(charge_stop_at=BREITE0 + 0.198)
    at = own.fitting_section(with_stop, start, destination)
    zs = own.waypoints(at, spacing_km=25.0, maximal=40)
    off_route = [p for p in zs if abs(p[1] - LON) >= 0.02]
    verify(at is not None and len(zs) >= 10, "mit Ladestopp gibt es weiter Kandidaten")
    verify(not off_route,
           "kein Zwischenpunkt auf dem Ladeplatz - er laege 3 km neben der "
           "Strasse und zwaenge die Route zu einem Abstecher", str(off_route))
    # Gegenprobe: Ohne die Tempo-Filterung wuerde genau dieser Punkt gewaehlt.
    # Sonst prueft der Test oben nichts.
    saved = own.MIN_SPEED_KMH
    own.MIN_SPEED_KMH = -1.0
    try:
        without_filter = [p for p in own.waypoints(at, spacing_km=25.0, maximal=40)
                       if abs(p[1] - LON) >= 0.02]
    finally:
        own.MIN_SPEED_KMH = saved
    verify(bool(without_filter),
           "und der Test ist scharf: Ohne die Tempo-Grenze landet der "
           "Ladeplatz tatsaechlich in der Liste")

    without_speed = [(b, lo, None) for b, lo, _ in fs_path]
    zo = own.waypoints(own.fitting_section(without_speed, start, destination))
    verify(len(zo) >= 5,
           "ein Pfad ohne Tempoangabe ist trotzdem brauchbar - manche Quellen "
           "liefern keines", str(len(zo)))

    only_as_of = [(b, lo, 0.0) for b, lo, _ in fs_path]
    verify(own.waypoints(own.fitting_section(only_as_of, start, destination)) == [],
           "ein Pfad, auf dem nie gefahren wurde, liefert keinen Kandidaten")

    verify.section("Eingabe")
    verify(own.path_from_samples([(1.0, 2.0, 50.0), (None, 2.0, 50.0),
                                        (1.0, None, 50.0)])
           == [(1.0, 2.0, 50.0)],
           "Messpunkte ohne Koordinate fallen heraus")

    return verify.balance()


if __name__ == "__main__":
    sys.exit(main())
