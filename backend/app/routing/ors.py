"""Adapter für openrouteservice.

Gewählt, weil es als einziger kostenlose Dienst ein **Höhenprofil** zur Route
mitliefert (`elevation=true`). Ohne Höhe wäre das Verbrauchsmodell auf die
Ebene beschränkt, und damit genau in den Fällen blind, in denen ein Ladeplaner
sich lohnt.

Kontingent des freien Zugangs: 2.500 Anfragen/Tag, 40.000/Monat.
Schlüssel: https://openrouteservice.org/dev/#/signup
"""
import logging
import os

import requests

from .provider import Ort, Route, RoutingFehler

BASIS = "https://api.openrouteservice.org"
TIMEOUT = 25
# Fällt ein Teilstück ohne Geschwindigkeitsangabe an (kommt an Kreuzungen und
# beim Zielpunkt vor), wird mit diesem Wert weitergerechnet statt abgebrochen.
TEMPO_ERSATZ_MS = 22.0        # ~80 km/h

log = logging.getLogger("uvicorn.error")


class ORS:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("ORS_API_KEY", "")

    # ---------- intern ----------

    def _kopf(self) -> dict:
        if not self.api_key:
            raise RoutingFehler(
                "Kein ORS_API_KEY gesetzt - ohne Schlüssel lässt sich keine "
                "Route rechnen. Kostenlos unter openrouteservice.org/dev")
        return {"Authorization": self.api_key,
                "Content-Type": "application/json; charset=utf-8"}

    @staticmethod
    def _tempo_je_teilstueck(eigenschaften: dict, anzahl_punkte: int) -> list:
        """Aus den Routing-Schritten eine Geschwindigkeit je Teilstück machen.

        ORS gibt Distanz und Dauer je Schritt sowie die Indizes der zugehörigen
        Geometriepunkte (`way_points`). Daraus wird die Durchschnitts-
        geschwindigkeit dieses Schritts auf alle seine Teilstücke verteilt.

        Das ist genauer als "Gesamtstrecke durch Gesamtzeit": Ein Plan, der die
        Ortsdurchfahrt mit Autobahntempo rechnet, unterschätzt den Verbrauch
        auf der Autobahn - und dort entscheidet er sich.
        """
        tempo = [0.0] * max(0, anzahl_punkte - 1)
        for abschnitt in eigenschaften.get("segments", []):
            for schritt in abschnitt.get("steps", []):
                dauer = schritt.get("duration") or 0.0
                strecke = schritt.get("distance") or 0.0
                wp = schritt.get("way_points") or []
                if dauer <= 0 or strecke <= 0 or len(wp) != 2:
                    continue
                v = strecke / dauer
                for i in range(wp[0], min(wp[1], len(tempo))):
                    tempo[i] = v
        return [v if v > 0 else TEMPO_ERSATZ_MS for v in tempo]

    # ---------- öffentlich ----------

    def route(self, start: tuple[float, float], ziel: tuple[float, float],
              zwischenstopps: list[tuple[float, float]] | None = None,
              praeferenz: str = "recommended") -> Route:
        koordinaten = [[start[1], start[0]]]
        for stopp in (zwischenstopps or []):
            koordinaten.append([stopp[1], stopp[0]])
        koordinaten.append([ziel[1], ziel[0]])

        try:
            antwort = requests.post(
                f"{BASIS}/v2/directions/driving-car/geojson",
                headers=self._kopf(), timeout=TIMEOUT,
                json={"coordinates": koordinaten, "elevation": True,
                      "instructions": True, "units": "m",
                      "preference": praeferenz})
        except requests.RequestException as fehler:
            raise RoutingFehler(f"Routing nicht erreichbar: {fehler}") from fehler

        if antwort.status_code == 401:
            raise RoutingFehler("ORS_API_KEY wird abgelehnt - Schlüssel prüfen.")
        if antwort.status_code == 429:
            raise RoutingFehler(
                "Tageskontingent von openrouteservice erschöpft (2.500 Anfragen).")
        if antwort.status_code >= 400:
            raise RoutingFehler(f"Routing meldet HTTP {antwort.status_code}: "
                                f"{antwort.text[:200]}")

        daten = antwort.json()
        merkmale = daten.get("features") or []
        if not merkmale:
            raise RoutingFehler("Keine Route gefunden - Start oder Ziel prüfen.")

        geometrie = merkmale[0].get("geometry", {}).get("coordinates") or []
        eigenschaften = merkmale[0].get("properties", {})
        zusammenfassung = eigenschaften.get("summary", {})

        if geometrie and len(geometrie[0]) < 3:
            log.warning("Route ohne Höhenwerte erhalten - Verbrauch wird in "
                        "der Ebene gerechnet und fällt bergig zu niedrig aus.")

        return Route(punkte=geometrie,
                     tempo_ms=self._tempo_je_teilstueck(eigenschaften, len(geometrie)),
                     strecke_m=float(zusammenfassung.get("distance") or 0.0),
                     fahrzeit_s=float(zusammenfassung.get("duration") or 0.0))

    def suchen(self, text: str, land: str = "DE") -> list[Ort]:
        try:
            antwort = requests.get(f"{BASIS}/geocode/search", timeout=TIMEOUT,
                                   params={"api_key": self.api_key, "text": text,
                                           "boundary.country": land, "size": 6})
            antwort.raise_for_status()
        except requests.RequestException as fehler:
            raise RoutingFehler(f"Ortssuche nicht erreichbar: {fehler}") from fehler

        treffer = []
        for merkmal in antwort.json().get("features", []):
            koord = merkmal.get("geometry", {}).get("coordinates") or []
            if len(koord) < 2:
                continue
            treffer.append(Ort(name=merkmal.get("properties", {}).get("label", text),
                               lat=koord[1], lon=koord[0]))
        return treffer
