"""Ein Routing-Adapter ohne Netz und ohne Schlüssel - nur für die Entwicklung.

Er erfindet eine Route: Luftlinie zwischen Start und Ziel, dazu ein
synthetisches Höhenprofil und ein Tempoverlauf, der langsam beginnt, auf
Autobahntempo geht und am Ziel wieder abfällt.

Wozu das gut ist: Das Verbrauchsmodell, die Korridor-Suche, das Live-Gerüst
und die Oberfläche lassen sich damit vollständig durchspielen, ohne dass ein
ORS-Schlüssel vorliegt oder das Tageskontingent belastet wird. Genau das
braucht man beim Entwickeln am häufigsten.

Er springt **nur** ein, wenn kein ORS_API_KEY gesetzt ist, und sagt in jeder
Antwort, dass er es war - eine erfundene Route darf nie unbemerkt für eine
echte gehalten werden.

Wichtige Einschränkung: Die Strecke ist die Luftlinie und damit rund ein
Fünftel kürzer als jede echte Strasse. Zum Prüfen der Rechenkette reicht das;
als Aussage über eine reale Fahrt taugt sie nicht.
"""
import math

from ..geo import haversine_m
from .provider import City, Route

# Grobe Koordinaten einiger Städte, damit die Ortssuche offline etwas
# zurückgeben kann. Keine Geokodierung, nur eine Handvoll Stützpunkte.
PLACES = {
    "hamburg": (53.5511, 9.9937), "münchen": (48.1351, 11.5820),
    "munich": (48.1351, 11.5820), "berlin": (52.5200, 13.4050),
    "köln": (50.9375, 6.9603), "koeln": (50.9375, 6.9603),
    "frankfurt": (50.1109, 8.6821), "stuttgart": (48.7758, 9.1829),
    "hannover": (52.3759, 9.7320), "leipzig": (51.3397, 12.3731),
    "nürnberg": (49.4521, 11.0767), "nuernberg": (49.4521, 11.0767),
    "bremen": (53.0793, 8.8017), "dortmund": (51.5136, 7.4653),
    "kassel": (51.3127, 9.4797), "würzburg": (49.7913, 9.9534),
}

POINT_DISTANCE_KM = 1.0


class DemoRouting:
    is_demo = True

    def route(self, start, destination, intermediate_stops=None,
             preference: str = "recommended",
             toll_free: bool = False) -> Route:
        # Es gibt kein echtes Strassennetz, aus dem sich schnellste und
        # empfohlene Route unterscheiden liessen, und eine erfundene
        # Luftlinie hat auch keine Mautstrassen. `praeferenz` und `mautfrei`
        # werden deshalb entgegengenommen und ignoriert; /api/route erkennt
        # die identischen Ergebnisse selbst und legt sie zu einer Variante
        # mit mehreren Etiketten zusammen.
        stations = [start] + list(intermediate_stops or []) + [destination]
        points: list[list[float]] = []
        velocity: list[float] = []

        for a, b in zip(stations, stations[1:]):
            part_points, part_speed = self._section(a, b, first=not points)
            points.extend(part_points)
            velocity.extend(part_speed)

        distance = sum(self._spacing_m(points[i][1], points[i][0],
                                      points[i + 1][1], points[i + 1][0])
                      for i in range(len(points) - 1))
        drive_time = sum(
            self._spacing_m(points[i][1], points[i][0],
                            points[i + 1][1], points[i + 1][0]) / max(1.0, velocity[i])
            for i in range(len(points) - 1))

        return Route(points=points, speed_ms=velocity, distance_m=distance,
                     drive_time_s=drive_time)

    def elevations(self, points: list) -> list | None:
        """Das Demo-Routing erfindet Routen, aber keine Höhen.

        Eine erfundene Höhe wäre hier schädlicher als gar keine: Sie sähe
        aus wie eine Messung und ginge in den Korrekturfaktor ein.
        """
        return None

    def seek(self, text: str, country: str = "") -> list[City]:
        keyname = (text or "").strip().lower()
        for name, (lat, lon) in PLACES.items():
            if keyname and keyname in name:
                return [City(name=f"{name.capitalize()} (Demo)", lat=lat, lon=lon)]
        return []

    # ---------- intern ----------

    @staticmethod
    def _spacing_m(lat1, lon1, lat2, lon2) -> float:
        return haversine_m(lat1, lon1, lat2, lon2)

    def _section(self, a, b, first: bool):
        total_m = self._spacing_m(a[0], a[1], b[0], b[1])
        count = max(2, int(total_m / 1000.0 / POINT_DISTANCE_KM))

        points, velocity = [], []
        for i in range(count + 1):
            t = i / count
            lat = a[0] + (b[0] - a[0]) * t
            lon = a[1] + (b[1] - a[1]) * t
            # Zwei überlagerte Wellen: ein langes Mittelgebirge und kleinere
            # Kuppen. Damit hat das Höhenprofil Steigung und Gefälle, und die
            # Rekuperation wird tatsächlich durchlaufen.
            elevation = (120.0 + 220.0 * math.sin(math.pi * t)
                     + 45.0 * math.sin(t * 14.0))
            if i > 0 or first:
                points.append([round(lon, 6), round(lat, 6), round(elevation, 1)])
            if i < count:
                # Auffahrt und Abfahrt langsamer, dazwischen Autobahn.
                edge = min(t, 1.0 - t)
                v = 16.0 + 20.0 * min(1.0, edge / 0.04)
                velocity.append(round(v, 2))
        return points, velocity
