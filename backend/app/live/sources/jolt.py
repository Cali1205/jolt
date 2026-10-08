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

from . import RawPoint, limits, required, truth, num


# Phones that already post (an iOS shortcut, a logger app) still use the field
# names from before the switch to English; keep accepting them.
LEGACY_KEYS = {"tempo_kmh": "speed_kmh", "aussentemp_c": "outside_temp_c",
               "zeit": "timestamp", "laedt": "charges", "rohwerte": "raw_values"}


class JoltFormat:
    name = "jolt"

    def normalize(self, records: dict) -> RawPoint:
        records = {LEGACY_KEYS.get(key, key): value for key, value in records.items()}
        timestamp = records.get("timestamp")
        return RawPoint(
            lat=limits(required(records, "lat"), -90, 90, "Breitengrad"),
            lon=limits(required(records, "lon"), -180, 180, "Längengrad"),
            soc=limits(required(records, "soc"), 0, 100, "Ladestand"),
            speed_kmh=num(records, "speed_kmh"),
            outside_temp_c=num(records, "outside_temp_c"),
            timestamp=datetime.fromisoformat(timestamp) if isinstance(timestamp, str) and timestamp
            else None,
            charges=truth(records, "charges"),
            # Unverändert übernommen: Ein Übersetzer, der hier aufräumt,
            # wirft genau das weg, wofür das Feld da ist.
            raw_values=records.get("raw_values") if isinstance(
                records.get("raw_values"), dict) else None)
