"""Das Interface zwischen jolt und dem Routing-Dienst.

Warum überhaupt ein Interface bei genau einem Adapter: Der kostenlose
openrouteservice-Zugang hat 2.500 Anfragen am Tag. Sobald die Live-Neuplanung
(Stufe 3) regelmässig rechnet, ist das eng, und dann tritt ein selbstgehostetes
Valhalla an dieselbe Stelle. Diese Ablösung soll ein zweiter Adapter sein und
kein Umbau - deshalb steht die Grenze jetzt schon fest, solange sie billig ist.

Was ein Adapter liefern muss, ist von jolts Verbrauchsmodell diktiert:
Geometrie **mit Höhe** und eine Geschwindigkeit je Teilstück. Eine reine
Distanz-plus-Dauer-Antwort reicht nicht - damit liesse sich weder eine Steigung
noch der v²-Anteil des Luftwiderstands rechnen.
"""
from dataclasses import dataclass, field
from typing import Protocol


class RoutingFehler(RuntimeError):
    """Das Routing konnte keine Route liefern - mit einem Grund für den Nutzer."""


@dataclass
class Route:
    # [[lon, lat, hoehe_m], ...] - die Reihenfolge lon/lat ist die von GeoJSON,
    # und Abweichen davon wäre eine dauerhafte Fehlerquelle.
    punkte: list = field(default_factory=list)
    # Geschwindigkeit in m/s je Teilstück; Länge len(punkte) - 1.
    tempo_ms: list = field(default_factory=list)
    strecke_m: float = 0.0
    fahrzeit_s: float = 0.0


@dataclass
class Ort:
    name: str
    lat: float
    lon: float


# Die drei Vorgaben, die openrouteservice als "preference" kennt und die
# /api/route parallel abfragt: Zeit, Distanz, und das, was ORS ohne weitere
# Angabe für die beste Abwägung hält. Verbrauchsoptimal ist bewusst keine
# eigene Anfrage - das kann ORS nicht als Kantengewicht, siehe unten.
PRAEFERENZEN = ("fastest", "shortest", "recommended")


class RoutingProvider(Protocol):
    def route(self, start: tuple[float, float], ziel: tuple[float, float],
              zwischenstopps: list[tuple[float, float]] | None = None,
              praeferenz: str = "recommended") -> Route:
        """Route von Start nach Ziel. Koordinaten als (lat, lon).

        `praeferenz` ist eine der drei Werte aus PRAEFERENZEN. Ein Adapter, der
        keine echte Unterscheidung treffen kann (siehe demo.py), darf den
        Parameter ignorieren und immer dieselbe Route liefern - /api/route
        erkennt gleiche Ergebnisse und zeigt sie nur einmal, mit mehreren
        Kennzeichnungen.
        """
        ...

    def suchen(self, text: str, land: str = "DE") -> list[Ort]:
        """Ortsnamen zu Koordinaten auflösen."""
        ...
