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

from .provider import Ort, Route

# Grobe Koordinaten einiger Städte, damit die Ortssuche offline etwas
# zurückgeben kann. Keine Geokodierung, nur eine Handvoll Stützpunkte.
ORTE = {
    "hamburg": (53.5511, 9.9937), "münchen": (48.1351, 11.5820),
    "muenchen": (48.1351, 11.5820), "berlin": (52.5200, 13.4050),
    "köln": (50.9375, 6.9603), "koeln": (50.9375, 6.9603),
    "frankfurt": (50.1109, 8.6821), "stuttgart": (48.7758, 9.1829),
    "hannover": (52.3759, 9.7320), "leipzig": (51.3397, 12.3731),
    "nürnberg": (49.4521, 11.0767), "nuernberg": (49.4521, 11.0767),
    "bremen": (53.0793, 8.8017), "dortmund": (51.5136, 7.4653),
    "kassel": (51.3127, 9.4797), "würzburg": (49.7913, 9.9534),
}

PUNKTABSTAND_KM = 1.0


class DemoRouting:
    ist_demo = True

    def route(self, start, ziel, zwischenstopps=None,
             praeferenz: str = "recommended") -> Route:
        # Es gibt kein echtes Strassennetz, aus dem sich schnellste, kürzeste
        # und empfohlene Route unterscheiden liessen - alle drei ergäben die
        # exakt gleiche Luftlinie. `praeferenz` wird deshalb entgegengenommen
        # und ignoriert; /api/route erkennt die identischen Ergebnisse selbst
        # und zeigt sie zusammengefasst als eine Variante mit drei Etiketten.
        stationen = [start] + list(zwischenstopps or []) + [ziel]
        punkte: list[list[float]] = []
        tempo: list[float] = []

        for a, b in zip(stationen, stationen[1:]):
            teil_punkte, teil_tempo = self._abschnitt(a, b, erster=not punkte)
            punkte.extend(teil_punkte)
            tempo.extend(teil_tempo)

        strecke = sum(self._abstand_m(punkte[i][1], punkte[i][0],
                                      punkte[i + 1][1], punkte[i + 1][0])
                      for i in range(len(punkte) - 1))
        fahrzeit = sum(
            self._abstand_m(punkte[i][1], punkte[i][0],
                            punkte[i + 1][1], punkte[i + 1][0]) / max(1.0, tempo[i])
            for i in range(len(punkte) - 1))

        return Route(punkte=punkte, tempo_ms=tempo, strecke_m=strecke,
                     fahrzeit_s=fahrzeit)

    def hoehen(self, punkte: list) -> list | None:
        """Das Demo-Routing erfindet Routen, aber keine Höhen.

        Eine erfundene Höhe wäre hier schädlicher als gar keine: Sie sähe
        aus wie eine Messung und ginge in den Korrekturfaktor ein.
        """
        return None

    def suchen(self, text: str, land: str = "") -> list[Ort]:
        schluessel = (text or "").strip().lower()
        for name, (lat, lon) in ORTE.items():
            if schluessel and schluessel in name:
                return [Ort(name=f"{name.capitalize()} (Demo)", lat=lat, lon=lon)]
        return []

    # ---------- intern ----------

    @staticmethod
    def _abstand_m(lat1, lon1, lat2, lon2) -> float:
        from ..energie.modell import haversine_m
        return haversine_m(lat1, lon1, lat2, lon2)

    def _abschnitt(self, a, b, erster: bool):
        gesamt_m = self._abstand_m(a[0], a[1], b[0], b[1])
        anzahl = max(2, int(gesamt_m / 1000.0 / PUNKTABSTAND_KM))

        punkte, tempo = [], []
        for i in range(anzahl + 1):
            t = i / anzahl
            lat = a[0] + (b[0] - a[0]) * t
            lon = a[1] + (b[1] - a[1]) * t
            # Zwei überlagerte Wellen: ein langes Mittelgebirge und kleinere
            # Kuppen. Damit hat das Höhenprofil Steigung und Gefälle, und die
            # Rekuperation wird tatsächlich durchlaufen.
            hoehe = (120.0 + 220.0 * math.sin(math.pi * t)
                     + 45.0 * math.sin(t * 14.0))
            if i > 0 or erster:
                punkte.append([round(lon, 6), round(lat, 6), round(hoehe, 1)])
            if i < anzahl:
                # Auffahrt und Abfahrt langsamer, dazwischen Autobahn.
                rand = min(t, 1.0 - t)
                v = 16.0 + 20.0 * min(1.0, rand / 0.04)
                tempo.append(round(v, 2))
        return punkte, tempo
