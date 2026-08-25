"""jolts eigenes Meldeformat.

Der schlichteste Fall - und trotzdem ein Übersetzer wie jeder andere. Zwei
Gründe, ihn nicht zu überspringen:

Erstens hätte ein Interface mit genau einem Implementierer keine Kanten, an
denen sich zeigt, ob es taugt. Zweitens durchläuft damit auch jolts eigene
Meldung dieselben Prüfungen wie eine fremde. Ein Kurzbefehl auf dem Telefon
ist ebenso gut in der Lage, `soc` als Anteil statt als Prozentpunkte zu
schicken wie ein fremder Dienst.
"""
from datetime import datetime

from . import Rohpunkt, grenzen, pflicht, wahrheit, zahl


class JoltFormat:
    name = "jolt"

    def normalisieren(self, daten: dict) -> Rohpunkt:
        zeit = daten.get("zeit")
        return Rohpunkt(
            lat=grenzen(pflicht(daten, "lat"), -90, 90, "Breitengrad"),
            lon=grenzen(pflicht(daten, "lon"), -180, 180, "Längengrad"),
            soc=grenzen(pflicht(daten, "soc"), 0, 100, "Ladestand"),
            tempo_kmh=zahl(daten, "tempo_kmh"),
            aussentemp_c=zahl(daten, "aussentemp_c"),
            zeit=datetime.fromisoformat(zeit) if isinstance(zeit, str) and zeit
            else None,
            laedt=wahrheit(daten, "laedt"))
