"""Ladepunkte im Korridor um eine Route finden.

Das ist der einzige Geo-Query, den jolt braucht - und der Grund, warum es hier
kein PostGIS gibt. Gesucht wird nicht "alles im Umkreis von X", sondern "alles
nahe an dieser Polyline". Dafür genügt: die Route in Ankerpunkte zerlegen, je
Anker ein Rechteck abfragen (der Index auf (lat, lon) trägt das), und danach
mit Haversine genau nachmessen.

Bei rund 150.000 deutschen Ladepunkten kostet das Millisekunden - und der
SQLite-Fallback für die lokale Entwicklung bleibt erhalten.
"""
import math
from dataclasses import dataclass

from sqlalchemy import and_, or_

from .. import models
from ..geo import haversine_m

KM_PER_DEGREE_LAT = 111.32
# Zufahrt und Rückweg laufen nicht über die Autobahn. 45 km/h ist grosszügig
# gerechnet, aber die Zahl soll den Umweg eher über- als unterschätzen:
# Ein zu optimistisch geplanter Abstecher kostet unterwegs echte Minuten.
ACCESS_ROAD_KMH = 45.0
# Fixkosten jedes Stopps unabhängig von der Entfernung: abfahren, suchen,
# einparken, Kabel holen, am Ende wieder auffädeln.
FIXED_MINUTES = 4.0


@dataclass
class Candidate:
    charge_point: models.ChargePoint
    km_on_route: float
    spacing_m: float
    detour_minutes: float

    def as_dict(self) -> dict:
        lp = self.charge_point
        return {"id": lp.id, "name": lp.name, "operator": lp.operator,
                "lat": lp.lat, "lon": lp.lon, "city": lp.city, "address": lp.address,
                "max_kw": lp.max_kw, "point_count": lp.point_count,
                "connector_types": lp.connector_types, "source": lp.source,
                "km_on_route": round(self.km_on_route, 1),
                "spacing_m": round(self.spacing_m),
                "detour_minutes": round(self.detour_minutes, 1)}


def _chainage(points: list) -> list[float]:
    """Kumulierte Kilometer je Stützpunkt der Route."""
    km = [0.0]
    for i in range(len(points) - 1):
        km.append(km[-1] + haversine_m(points[i][1], points[i][0],
                                       points[i + 1][1], points[i + 1][0]) / 1000.0)
    return km


def _anchor(points: list, km_list: list[float],
           spacing_km: float) -> list[tuple[float, float, float, int]]:
    """Route auf Ankerpunkte ausdünnen: (lat, lon, km_auf_route, index).

    Die Anker dienen nur der Vorauswahl - dem Rechteck für die Datenbank und
    der Frage, *wo ungefähr* ein Ladepunkt an der Route liegt. Gemessen wird
    anschliessend an den echten Stützpunkten; siehe `suchen`.
    """
    if not points:
        return []
    anchor = [(points[0][1], points[0][0], 0.0, 0)]
    km_since_anchor = 0.0
    for i in range(len(points) - 1):
        km_since_anchor += km_list[i + 1] - km_list[i]
        if km_since_anchor >= spacing_km:
            anchor.append((points[i + 1][1], points[i + 1][0],
                          km_list[i + 1], i + 1))
            km_since_anchor = 0.0
    if anchor[-1][3] < len(points) - 1:
        anchor.append((points[-1][1], points[-1][0], km_list[-1], len(points) - 1))
    return anchor


def seek(db, points: list, radius_km: float = 8.0, min_kw: float = 50.0,
           connector_type: str = "CCS", at_most: int = 400) -> list[Candidate]:
    """Alle passenden Ladepunkte entlang der Route, geordnet nach Fortschritt.

    `radius_km` ist Luftlinie zur Route. Acht Kilometer klingen viel, sind an
    einer Autobahn aber schnell erreicht, wenn die nächste Abfahrt spät kommt -
    entschieden wird ohnehin über `umweg_minuten`, nicht über die Luftlinie.
    """
    if not points:
        return []

    km_list = _chainage(points)
    anchor = _anchor(points, km_list, max(3.0, radius_km * 0.75))
    d_lat = radius_km / KM_PER_DEGREE_LAT

    rectangles = []
    for lat, lon, _, _ in anchor:
        d_lon = radius_km / (KM_PER_DEGREE_LAT * max(0.1, math.cos(math.radians(lat))))
        rectangles.append(and_(models.ChargePoint.lat.between(lat - d_lat, lat + d_lat),
                              models.ChargePoint.lon.between(lon - d_lon, lon + d_lon)))

    lookup = db.query(models.ChargePoint).filter(or_(*rectangles))
    if min_kw > 0:
        lookup = lookup.filter(models.ChargePoint.max_kw >= min_kw)
    if connector_type:
        lookup = lookup.filter(models.ChargePoint.connector_types.contains(connector_type))
    # Was die Quelle ausdrücklich als ausser Betrieb meldet, gehört nicht in
    # einen Plan. `isnot(False)` und nicht `is_(True)`: NULL heisst
    # **unbekannt**, und das ist für den grössten Teil der Datenbank der
    # Fall. Wer Unbekanntes wie Ausgeschlossenes behandelt, verliert fast
    # alle Kandidaten und plant dann gar nicht mehr.
    lookup = lookup.filter(models.ChargePoint.operational.isnot(False))

    candidates: list[Candidate] = []
    for lp in lookup.limit(at_most * 5).all():
        # Genau nachmessen, und zwar an den echten Stützpunkten - nicht am
        # nächsten Anker. Die Anker stehen bei 25 km Radius rund 19 km
        # auseinander; eine Säule unmittelbar an der Strasse wäre von einem
        # Anker aus bis zu 9 km entfernt und bekäme daraus 25 Minuten Umweg
        # angerechnet. Damit fiele sie aus jeder Planung, obwohl sie direkt am
        # Weg liegt. Der Anker sagt also nur, *welches Stück* der Route zu
        # prüfen ist; gemessen wird im Fenster bis zu den Nachbarankern.
        upcoming = min(range(len(anchor)),
                        key=lambda i: haversine_m(lp.lat, lp.lon,
                                                  anchor[i][0], anchor[i][1]))
        begin = anchor[upcoming - 1][3] if upcoming > 0 else 0
        upto = (anchor[upcoming + 1][3] if upcoming + 1 < len(anchor)
               else len(points) - 1)

        spacing = float("inf")
        km_on_route = anchor[upcoming][2]
        for i in range(begin, upto + 1):
            d = haversine_m(lp.lat, lp.lon, points[i][1], points[i][0])
            if d < spacing:
                spacing, km_on_route = d, km_list[i]

        if spacing > radius_km * 1000:
            continue
        detour = FIXED_MINUTES + (2 * spacing / 1000.0) / ACCESS_ROAD_KMH * 60.0
        candidates.append(Candidate(lp, km_on_route, spacing, detour))

    candidates.sort(key=lambda k: k.km_on_route)
    return candidates[:at_most]


def point_on_route(points: list, lat: float, lon: float) -> tuple[float, float]:
    """Wie weit ist eine Position auf der Route fortgeschritten?

    Rückgabe: (km_auf_route, abstand_m). Wird von der Live-Nachführung
    gebraucht, um Ist- und Soll-SoC an derselben Stelle zu vergleichen -
    und um zu erkennen, dass jemand die Route verlassen hat.
    """
    if not points:
        return 0.0, 0.0
    best_km, best_spacing = 0.0, float("inf")
    km_cum = 0.0
    for i, p in enumerate(points):
        if i > 0:
            km_cum += haversine_m(points[i - 1][1], points[i - 1][0],
                                  p[1], p[0]) / 1000.0
        spacing = haversine_m(lat, lon, p[1], p[0])
        if spacing < best_spacing:
            best_spacing, best_km = spacing, km_cum
    return best_km, best_spacing
