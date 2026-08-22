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
from ..energie.modell import haversine_m

KM_JE_GRAD_LAT = 111.32
# Zufahrt und Rückweg laufen nicht über die Autobahn. 45 km/h ist grosszügig
# gerechnet, aber die Zahl soll den Umweg eher über- als unterschätzen:
# Ein zu optimistisch geplanter Abstecher kostet unterwegs echte Minuten.
ZUFAHRT_KMH = 45.0
# Fixkosten jedes Stopps unabhängig von der Entfernung: abfahren, suchen,
# einparken, Kabel holen, am Ende wieder auffädeln.
FIXE_MINUTEN = 4.0


@dataclass
class Kandidat:
    ladepunkt: models.Ladepunkt
    km_auf_route: float
    abstand_m: float
    umweg_minuten: float

    def als_dict(self) -> dict:
        lp = self.ladepunkt
        return {"id": lp.id, "name": lp.name, "betreiber": lp.betreiber,
                "lat": lp.lat, "lon": lp.lon, "ort": lp.ort, "adresse": lp.adresse,
                "max_kw": lp.max_kw, "anzahl_punkte": lp.anzahl_punkte,
                "steckertypen": lp.steckertypen, "quelle": lp.quelle,
                "km_auf_route": round(self.km_auf_route, 1),
                "abstand_m": round(self.abstand_m),
                "umweg_minuten": round(self.umweg_minuten, 1)}


def _anker(punkte: list, abstand_km: float) -> list[tuple[float, float, float]]:
    """Route auf Ankerpunkte ausdünnen: (lat, lon, km_auf_route)."""
    if not punkte:
        return []
    anker = [(punkte[0][1], punkte[0][0], 0.0)]
    km_kum = 0.0
    km_seit_anker = 0.0
    for i in range(len(punkte) - 1):
        d = haversine_m(punkte[i][1], punkte[i][0],
                        punkte[i + 1][1], punkte[i + 1][0]) / 1000.0
        km_kum += d
        km_seit_anker += d
        if km_seit_anker >= abstand_km:
            anker.append((punkte[i + 1][1], punkte[i + 1][0], km_kum))
            km_seit_anker = 0.0
    letzter = punkte[-1]
    if anker[-1][2] < km_kum:
        anker.append((letzter[1], letzter[0], km_kum))
    return anker


def suchen(db, punkte: list, radius_km: float = 8.0, min_kw: float = 50.0,
           steckertyp: str = "CCS", hoechstens: int = 400) -> list[Kandidat]:
    """Alle passenden Ladepunkte entlang der Route, geordnet nach Fortschritt.

    `radius_km` ist Luftlinie zur Route. Acht Kilometer klingen viel, sind an
    einer Autobahn aber schnell erreicht, wenn die nächste Abfahrt spät kommt -
    entschieden wird ohnehin über `umweg_minuten`, nicht über die Luftlinie.
    """
    if not punkte:
        return []

    anker = _anker(punkte, max(3.0, radius_km * 0.75))
    d_lat = radius_km / KM_JE_GRAD_LAT

    rechtecke = []
    for lat, lon, _ in anker:
        d_lon = radius_km / (KM_JE_GRAD_LAT * max(0.1, math.cos(math.radians(lat))))
        rechtecke.append(and_(models.Ladepunkt.lat.between(lat - d_lat, lat + d_lat),
                              models.Ladepunkt.lon.between(lon - d_lon, lon + d_lon)))

    abfrage = db.query(models.Ladepunkt).filter(or_(*rechtecke))
    if min_kw > 0:
        abfrage = abfrage.filter(models.Ladepunkt.max_kw >= min_kw)
    if steckertyp:
        abfrage = abfrage.filter(models.Ladepunkt.steckertypen.contains(steckertyp))

    kandidaten: list[Kandidat] = []
    for lp in abfrage.limit(hoechstens * 5).all():
        # Genau nachmessen: das Rechteck ist grosszügiger als der Kreis, und
        # an den Ecken liegt der Ladepunkt bis zu 41 % weiter weg als erlaubt.
        naechster = min(anker, key=lambda a: haversine_m(lp.lat, lp.lon, a[0], a[1]))
        abstand = haversine_m(lp.lat, lp.lon, naechster[0], naechster[1])
        if abstand > radius_km * 1000:
            continue
        umweg = FIXE_MINUTEN + (2 * abstand / 1000.0) / ZUFAHRT_KMH * 60.0
        kandidaten.append(Kandidat(lp, naechster[2], abstand, umweg))

    kandidaten.sort(key=lambda k: k.km_auf_route)
    return kandidaten[:hoechstens]


def punkt_auf_route(punkte: list, lat: float, lon: float) -> tuple[float, float]:
    """Wie weit ist eine Position auf der Route fortgeschritten?

    Rückgabe: (km_auf_route, abstand_m). Wird von der Live-Nachführung
    gebraucht, um Ist- und Soll-SoC an derselben Stelle zu vergleichen -
    und um zu erkennen, dass jemand die Route verlassen hat.
    """
    if not punkte:
        return 0.0, 0.0
    bester_km, bester_abstand = 0.0, float("inf")
    km_kum = 0.0
    for i, p in enumerate(punkte):
        if i > 0:
            km_kum += haversine_m(punkte[i - 1][1], punkte[i - 1][0],
                                  p[1], p[0]) / 1000.0
        abstand = haversine_m(lat, lon, p[1], p[0])
        if abstand < bester_abstand:
            bester_abstand, bester_km = abstand, km_kum
    return bester_km, bester_abstand
