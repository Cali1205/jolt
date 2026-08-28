"""Geometrie auf der Erdkugel. Zwei Funktionen, keine Abhängigkeiten.

Diese beiden standen in `energie/modell.py`, und das war die einzige
Schichtverletzung im Backend: `routing/korridor.py` musste für eine
Entfernung zwischen zwei Punkten in die Physik greifen. Eine Entfernung ist
aber keine Aussage über Energie, und `routing` liegt unter `energie`.

Warum hier und nicht in `routing/geo.py`, wie zunächst vorgeschlagen: Dann
hinge `energie` an `routing`, und die Verletzung wäre nur umgedreht. Beide
Schichten brauchen diese Funktionen, also gehören sie **unter** beide - auf
dieselbe Ebene wie `models` und `security`.

Bewusst ohne einen einzigen Import aus dem Projekt. Ein Modul, das nichts
kennt, kann von überall benutzt werden, ohne je einen Zyklus zu bilden.
"""
import math

# Mittlerer Erdradius nach WGS84. Für Entfernungen entlang einer Route ist
# die Kugelnäherung genau genug: Der Fehler gegenüber dem Ellipsoid liegt
# unter einem halben Prozent, und die Stützpunkte einer Route stehen ohnehin
# nur alle paar hundert Meter.
ERDRADIUS_M = 6371008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Entfernung zweier Punkte auf der Erdkugel in Metern."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * ERDRADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def peilung_grad(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Fahrtrichtung von Punkt 1 nach Punkt 2, 0 = Norden."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
