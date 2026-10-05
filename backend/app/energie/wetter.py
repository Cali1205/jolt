"""Wetter entlang der Route über Open-Meteo (ohne Schlüssel, ohne Anmeldung).

Nicht ein Wert für die ganze Fahrt: Hamburg-München sind 800 km, da liegen
zwischen Start und Ziel im Winter regelmässig zehn Grad und ein anderer Wind.
Abgefragt werden deshalb mehrere Stützpunkte in einer einzigen Anfrage -
Open-Meteo nimmt kommagetrennte Koordinatenlisten entgegen.
"""
import logging
from datetime import datetime, timedelta, timezone

import requests

from ..geo import haversine_m
from .modell import Umgebung

API = "https://api.open-meteo.com/v1/forecast"
STUETZPUNKTE = 6
TIMEOUT = 8

# Open-Meteo liefert stündliche Werte für 16 Tage. Eine Reserve von einem Tag,
# damit die letzte Stunde nicht gerade am Rand liegt.
PROGNOSE_TAGE = 15
# Eine Abfahrt in den nächsten Minuten ist "jetzt": Der Unterschied zwischen
# der aktuellen und der stündlichen Vorhersage ist dort kleiner als die
# Messunsicherheit, und die aktuelle Abfrage ist die genauere.
JETZT_TOLERANZ = timedelta(minutes=30)

log = logging.getLogger("uvicorn.error")


def _auswaehlen(punkte: list, anzahl: int) -> list:
    """Gleichmässig verteilte Stützpunkte, Start und Ziel immer dabei."""
    if len(punkte) <= anzahl:
        return list(punkte)
    schritt = (len(punkte) - 1) / (anzahl - 1)
    return [punkte[round(i * schritt)] for i in range(anzahl)]


def _prognose_fuer(abfahrt: datetime | None) -> bool:
    """Soll die stündliche Vorhersage gelten statt der aktuellen Messung?

    Ja, wenn die Abfahrt später als in einer halben Stunde liegt - und noch
    innerhalb dessen, was Open-Meteo kennt. Darüber hinaus wird mit dem
    aktuellen Wetter gerechnet und das im Log gesagt: Besser ein falsches
    Wetter, das man als solches erkennt, als gar keine Route.
    """
    if abfahrt is None:
        return False
    jetzt = datetime.now(timezone.utc)
    if abfahrt.tzinfo is None:
        abfahrt = abfahrt.replace(tzinfo=timezone.utc)
    if abfahrt <= jetzt + JETZT_TOLERANZ:
        return False
    if abfahrt > jetzt + timedelta(days=PROGNOSE_TAGE):
        log.warning("Abfahrt in mehr als %d Tagen - dafür gibt es keine "
                    "Wettervorhersage, es gilt das aktuelle Wetter.", PROGNOSE_TAGE)
        return False
    return True


def _stunde(zeiten: list, zeitpunkt: datetime) -> int:
    """Index der Stunde in `zeiten`, die dem Zeitpunkt am nächsten liegt.

    Open-Meteo liefert die Zeiten bei `timezone=UTC` als "2026-10-06T08:00"
    ohne Zone, aufsteigend und stündlich. Aus dem ersten Eintrag und dem
    Abstand ergibt sich der Index, ohne die Liste zu durchsuchen; er wird auf
    die vorhandenen Werte begrenzt.
    """
    if zeitpunkt.tzinfo is not None:
        zeitpunkt = zeitpunkt.astimezone(timezone.utc).replace(tzinfo=None)
    erste = datetime.fromisoformat(zeiten[0])
    index = round((zeitpunkt - erste).total_seconds() / 3600.0)
    return max(0, min(len(zeiten) - 1, index))


def entlang_route(punkte: list, anzahl: int = STUETZPUNKTE,
                  vorgabe: Umgebung | None = None,
                  abfahrt: datetime | None = None, dauer_s: float = 0.0):
    """Gibt eine Funktion (lat, lon) -> Umgebung zurück.

    `abfahrt` und `dauer_s`: Liegt die Abfahrt später als in einer halben
    Stunde, gilt die stündliche Vorhersage - und zwar an jedem Stützpunkt für
    die Stunde, in der man dort ankommt: Abfahrt plus Anteil der Fahrzeit,
    gleichmässig auf die Strecke verteilt. Eine Fahrt morgen früh um sechs
    soll nicht mit dem Nachmittagswetter von heute gerechnet werden; die
    Heizung ist der grösste Einzelposten der Kälte.

    Fällt die Abfrage aus, wird nicht abgebrochen, sondern mit `vorgabe`
    weitergerechnet: Eine Route ohne Wetter ist deutlich besser als gar keine
    Route, und die Live-Nachführung korrigiert den Fehler ohnehin innerhalb
    der ersten Kilometer.

    `vorgabe` ist beim Planen sinnvollerweise leer - dann gelten 15 °C und
    Windstille. **Unterwegs** ist genau das die falsche Annahme: Wer eine im
    Winter bei -5 °C gerechnete Fahrt neu plant und dabei auf 15 °C
    zurückfällt, verliert die Heizlast - den grössten Einzelposten der Kälte -
    und rechnet die Reststrecke zu optimistisch. Deshalb reicht die Umplanung
    dort die Temperatur der Fahrt herein statt sich auf die Vorgabe zu
    verlassen.
    """
    ersatz = vorgabe or Umgebung()
    proben = _auswaehlen(punkte, anzahl)
    if not proben:
        return lambda lat, lon: ersatz

    lats = ",".join(f"{p[1]:.4f}" for p in proben)
    lons = ",".join(f"{p[0]:.4f}" for p in proben)
    prognose = _prognose_fuer(abfahrt)
    grossen = "temperature_2m,wind_speed_10m,wind_direction_10m"
    anfrage = {"latitude": lats, "longitude": lons, "wind_speed_unit": "ms"}
    if prognose:
        anfrage.update({"hourly": grossen, "timezone": "UTC", "forecast_days": 16})
    else:
        anfrage["current"] = grossen
    try:
        antwort = requests.get(API, params=anfrage, timeout=TIMEOUT)
        antwort.raise_for_status()
        roh = antwort.json()
    except (requests.RequestException, ValueError) as fehler:
        log.warning("Wetterabfrage fehlgeschlagen (%s) - rechne mit %.0f °C.",
                    fehler, ersatz.temp_c)
        return lambda lat, lon: ersatz

    # Bei einer einzelnen Koordinate liefert Open-Meteo ein Objekt, bei
    # mehreren eine Liste. Beides auf dieselbe Form bringen.
    eintraege = roh if isinstance(roh, list) else [roh]

    messungen: list[tuple[float, float, Umgebung]] = []
    for nr, (probe, eintrag) in enumerate(zip(proben, eintraege)):
        if prognose:
            try:
                werte = _stundenwerte(eintrag, abfahrt, dauer_s, nr, len(proben))
            except (KeyError, IndexError, ValueError, TypeError) as fehler:
                # Eine Antwort, die nicht so aussieht wie erwartet, ist kein
                # Grund, die Route zu verwerfen.
                log.warning("Wettervorhersage nicht lesbar (%s) - rechne mit "
                            "%.0f °C.", type(fehler).__name__, ersatz.temp_c)
                return lambda lat, lon: ersatz
        else:
            werte = eintrag.get("current") or {}
        messungen.append((probe[1], probe[0], Umgebung(
            temp_c=float(werte.get("temperature_2m", 15.0)),
            windgeschwindigkeit_ms=float(werte.get("wind_speed_10m", 0.0)),
            windrichtung_grad=float(werte.get("wind_direction_10m", 0.0)))))

    if not messungen:
        return lambda lat, lon: ersatz

    def nachschlagen(lat: float, lon: float) -> Umgebung:
        beste = min(messungen, key=lambda m: haversine_m(lat, lon, m[0], m[1]))
        return beste[2]

    return nachschlagen


def _stundenwerte(eintrag: dict, abfahrt: datetime, dauer_s: float,
                  nr: int, anzahl: int) -> dict:
    """Die Werte der Stunde, in der man am Stützpunkt `nr` ankommt.

    Die Stützpunkte liegen gleichmässig auf der Strecke; angenommen wird
    gleichmässige Fahrt. Das ist grob - eine Pause oder ein Ladestopp
    verschiebt die Ankunft -, aber eine Stunde Abweichung ändert die
    Temperatur um ein, zwei Grad und nicht um zehn.
    """
    anteil = nr / (anzahl - 1) if anzahl > 1 else 0.0
    zeitpunkt = abfahrt + timedelta(seconds=anteil * max(0.0, dauer_s))
    stuendlich = eintrag["hourly"]
    i = _stunde(stuendlich["time"], zeitpunkt)
    return {name: stuendlich[name][i] for name in
            ("temperature_2m", "wind_speed_10m", "wind_direction_10m")
            if stuendlich[name][i] is not None}


def mittelwert(punkte: list, abfahrt: datetime | None = None,
               dauer_s: float = 0.0) -> Umgebung:
    """Ein einzelner Wert für die Anzeige ("bei 4 °C gerechnet")."""
    hole = entlang_route(punkte, abfahrt=abfahrt, dauer_s=dauer_s)
    proben = _auswaehlen(punkte, STUETZPUNKTE)
    werte = [hole(p[1], p[0]) for p in proben] or [Umgebung()]
    return Umgebung(
        temp_c=round(sum(w.temp_c for w in werte) / len(werte), 1),
        windgeschwindigkeit_ms=round(
            sum(w.windgeschwindigkeit_ms for w in werte) / len(werte), 1),
        windrichtung_grad=werte[len(werte) // 2].windrichtung_grad)
