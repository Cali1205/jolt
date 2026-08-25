"""Das Telemetrieformat von Iternio (A Better Routeplanner).

Warum ausgerechnet dieses: Es ist das einzige, das im Umfeld der
Elektroauto-Logger so etwas wie ein Quasi-Standard ist. Wer Live-Daten aus
einem Auto herausbekommt, spricht mit einiger Wahrscheinlichkeit dieses
Format - die ABRP-App selbst, ESP32-Dongles wie der WiCAN, OVMS,
Home-Assistant-Integrationen, eine Reihe von Bastelskripten. Ein Übersetzer
dafür ist deshalb nicht ein Adapter für einen Anbieter, sondern einer für ein
halbes Ökosystem.

Er ist absichtlich unabhängig davon, *wie* die Daten hereinkommen: Dieselben
Felder stehen in einem POST an `/1/tlm/send` wie in der Antwort auf
`/1/tlm/get_telemetry`. Ob jolt sie geschickt bekommt oder abholt, ist eine
Frage des Transports und steht nicht hier.

Format (die Felder, die jolt braucht - es gibt mehr):

    utc          Sekunden seit Epoche, Zeitpunkt der Messung
    soc          Ladestand in Prozentpunkten, 0 bis 100
    lat, lon     Position, Dezimalgrad
    speed        km/h
    ext_temp     Aussentemperatur in °C
    is_charging  0/1
"""
from datetime import datetime, timezone

from . import (QuellenFehler, Rohpunkt, grenzen, pflicht, wahrheit, zahl)

# Ab diesem Rohwert ist `utc` in Millisekunden gemeint und nicht in Sekunden.
# Sekunden erreichen diese Grösse erst im Jahr 5138; Millisekunden liegen
# heute darüber. Die Verwechslung ist der häufigste Fehler an dieser Stelle,
# und sie fällt ohne Korrektur erst auf, wenn der Zeitfaktor Unsinn ergibt.
MILLISEKUNDEN_AB = 1e11

# Ein Zeitstempel ausserhalb dieser Spanne ist keine Messung, sondern eine
# ungestellte Uhr - meist 1970, wenn ein Kleinstrechner ohne Netz startet.
FRUEHESTENS = datetime(2020, 1, 1)
SPAETESTENS = datetime(2100, 1, 1)


def _auspacken(daten: dict) -> dict:
    """Die Nutzlast aus ihrer Hülle holen.

    Beim Senden steht sie unter `tlm`, beim Abholen unter `result` - und wer
    einen mitgeschnittenen Datensatz von Hand weiterreicht, hat sie meist
    schon ausgepackt. Alle drei Fälle sind dasselbe Format in einer anderen
    Verpackung, und daran soll niemand scheitern.
    """
    for huelle in ("result", "tlm"):
        inneres = daten.get(huelle)
        if isinstance(inneres, dict):
            return inneres
    return daten


def _zeit(roh: float | None) -> datetime | None:
    if roh is None:
        return None
    sekunden = roh / 1000.0 if roh >= MILLISEKUNDEN_AB else roh
    try:
        gelesen = datetime.fromtimestamp(sekunden, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise QuellenFehler(f"Zeitstempel unbrauchbar: {roh!r}")
    # Ohne Zeitzone weiter, weil jolt durchgehend mit naiver UTC rechnet
    # (`datetime.utcnow()`); ein Punkt mit Zeitzone unter lauter Punkten ohne
    # liesse jeden Vergleich mit einer TypeError-Ausnahme scheitern.
    ohne_zone = gelesen.replace(tzinfo=None)
    if not FRUEHESTENS <= ohne_zone <= SPAETESTENS:
        raise QuellenFehler(
            f"Zeitstempel liegt ausserhalb jeder Plausibilität: "
            f"{ohne_zone.isoformat()} - steht die Uhr des Loggers?")
    return ohne_zone


class AbrpFormat:
    name = "abrp"

    def normalisieren(self, daten: dict) -> Rohpunkt:
        nutzlast = _auspacken(daten)

        # Bewusst keine Umrechnung eines Anteils (0 bis 1) auf Prozentpunkte:
        # Ein `soc` von 0,4 ist als "40 %" gemeint oder als "0,4 %", und beides
        # kommt vor. Zu raten hiesse, ausgerechnet bei fast leerem Akku zu
        # raten - dort, wo eine falsche Zahl den Fahrer stehen lässt.
        soc = grenzen(pflicht(nutzlast, "soc"), 0, 100, "Ladestand")

        return Rohpunkt(
            lat=grenzen(pflicht(nutzlast, "lat"), -90, 90, "Breitengrad"),
            lon=grenzen(pflicht(nutzlast, "lon"), -180, 180, "Längengrad"),
            soc=soc,
            tempo_kmh=zahl(nutzlast, "speed"),
            aussentemp_c=zahl(nutzlast, "ext_temp", "extTemp"),
            zeit=_zeit(zahl(nutzlast, "utc")),
            laedt=wahrheit(nutzlast, "is_charging", "isCharging"))
