"""Wetter entlang der Route über Open-Meteo (ohne Schlüssel, ohne Anmeldung).

Nicht ein Wert für die ganze Fahrt: Hamburg-München sind 800 km, da liegen
zwischen Start und Ziel im Winter regelmässig zehn Grad und ein anderer Wind.
Abgefragt werden deshalb mehrere Stützpunkte in einer einzigen Anfrage -
Open-Meteo nimmt kommagetrennte Koordinatenlisten entgegen.
"""
import logging

import requests

from .modell import Umgebung, haversine_m

API = "https://api.open-meteo.com/v1/forecast"
STUETZPUNKTE = 6
TIMEOUT = 8

log = logging.getLogger("uvicorn.error")


def _auswaehlen(punkte: list, anzahl: int) -> list:
    """Gleichmässig verteilte Stützpunkte, Start und Ziel immer dabei."""
    if len(punkte) <= anzahl:
        return list(punkte)
    schritt = (len(punkte) - 1) / (anzahl - 1)
    return [punkte[round(i * schritt)] for i in range(anzahl)]


def entlang_route(punkte: list, anzahl: int = STUETZPUNKTE):
    """Gibt eine Funktion (lat, lon) -> Umgebung zurück.

    Fällt die Abfrage aus, wird nicht abgebrochen, sondern mit 15 °C und
    Windstille weitergerechnet: Eine Route ohne Wetter ist deutlich besser
    als gar keine Route, und die Live-Nachführung korrigiert den Fehler
    ohnehin innerhalb der ersten Kilometer.
    """
    proben = _auswaehlen(punkte, anzahl)
    if not proben:
        return lambda lat, lon: Umgebung()

    lats = ",".join(f"{p[1]:.4f}" for p in proben)
    lons = ",".join(f"{p[0]:.4f}" for p in proben)
    try:
        antwort = requests.get(API, params={
            "latitude": lats, "longitude": lons,
            "current": "temperature_2m,wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "ms"}, timeout=TIMEOUT)
        antwort.raise_for_status()
        roh = antwort.json()
    except (requests.RequestException, ValueError) as fehler:
        log.warning("Wetterabfrage fehlgeschlagen (%s) - rechne mit 15 °C.", fehler)
        return lambda lat, lon: Umgebung()

    # Bei einer einzelnen Koordinate liefert Open-Meteo ein Objekt, bei
    # mehreren eine Liste. Beides auf dieselbe Form bringen.
    eintraege = roh if isinstance(roh, list) else [roh]

    messungen: list[tuple[float, float, Umgebung]] = []
    for probe, eintrag in zip(proben, eintraege):
        jetzt = eintrag.get("current") or {}
        messungen.append((probe[1], probe[0], Umgebung(
            temp_c=float(jetzt.get("temperature_2m", 15.0)),
            windgeschwindigkeit_ms=float(jetzt.get("wind_speed_10m", 0.0)),
            windrichtung_grad=float(jetzt.get("wind_direction_10m", 0.0)))))

    if not messungen:
        return lambda lat, lon: Umgebung()

    def nachschlagen(lat: float, lon: float) -> Umgebung:
        beste = min(messungen, key=lambda m: haversine_m(lat, lon, m[0], m[1]))
        return beste[2]

    return nachschlagen


def mittelwert(punkte: list) -> Umgebung:
    """Ein einzelner Wert für die Anzeige ("bei 4 °C gerechnet")."""
    hole = entlang_route(punkte)
    proben = _auswaehlen(punkte, STUETZPUNKTE)
    werte = [hole(p[1], p[0]) for p in proben] or [Umgebung()]
    return Umgebung(
        temp_c=round(sum(w.temp_c for w in werte) / len(werte), 1),
        windgeschwindigkeit_ms=round(
            sum(w.windgeschwindigkeit_ms for w in werte) / len(werte), 1),
        windrichtung_grad=werte[len(werte) // 2].windrichtung_grad)
