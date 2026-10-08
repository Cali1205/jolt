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

from . import (SourcesError, RawPoint, limits, required, truth, num)

# Ab diesem Rohwert ist `utc` in Millisekunden gemeint und nicht in Sekunden.
# Sekunden erreichen diese Grösse erst im Jahr 5138; Millisekunden liegen
# heute darüber. Die Verwechslung ist der häufigste Fehler an dieser Stelle,
# und sie fällt ohne Korrektur erst auf, wenn der Zeitfaktor Unsinn ergibt.
MILLISECONDS_FROM = 1e11

# Ein Zeitstempel ausserhalb dieser Spanne ist keine Messung, sondern eine
# ungestellte Uhr - meist 1970, wenn ein Kleinstrechner ohne Netz startet.
EARLIEST = datetime(2020, 1, 1)
LATEST = datetime(2100, 1, 1)


def _unpack(records: dict) -> dict:
    """Die Nutzlast aus ihrer Hülle holen.

    Beim Senden steht sie unter `tlm`, beim Abholen unter `result` - und wer
    einen mitgeschnittenen Datensatz von Hand weiterreicht, hat sie meist
    schon ausgepackt. Alle drei Fälle sind dasselbe Format in einer anderen
    Verpackung, und daran soll niemand scheitern.
    """
    for shell in ("result", "tlm"):
        inneres = records.get(shell)
        if isinstance(inneres, dict):
            return inneres
    return records


def _time(raw: float | None) -> datetime | None:
    if raw is None:
        return None
    seconds = raw / 1000.0 if raw >= MILLISECONDS_FROM else raw
    try:
        read = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise SourcesError(f"Zeitstempel unbrauchbar: {raw!r}")
    # Ohne Zeitzone weiter, weil jolt durchgehend mit naiver UTC rechnet
    # (`datetime.utcnow()`); ein Punkt mit Zeitzone unter lauter Punkten ohne
    # liesse jeden Vergleich mit einer TypeError-Ausnahme scheitern.
    without_zone = read.replace(tzinfo=None)
    if not EARLIEST <= without_zone <= LATEST:
        raise SourcesError(
            f"Zeitstempel liegt ausserhalb jeder Plausibilität: "
            f"{without_zone.isoformat()} - steht die Uhr des Loggers?")
    return without_zone


class AbrpFormat:
    name = "abrp"

    def normalize(self, records: dict) -> RawPoint:
        payload = _unpack(records)

        # Bewusst keine Umrechnung eines Anteils (0 bis 1) auf Prozentpunkte:
        # Ein `soc` von 0,4 ist als "40 %" gemeint oder als "0,4 %", und beides
        # kommt vor. Zu raten hiesse, ausgerechnet bei fast leerem Akku zu
        # raten - dort, wo eine falsche Zahl den Fahrer stehen lässt.
        soc = limits(required(payload, "soc"), 0, 100, "Ladestand")

        return RawPoint(
            lat=limits(required(payload, "lat"), -90, 90, "Breitengrad"),
            lon=limits(required(payload, "lon"), -180, 180, "Längengrad"),
            soc=soc,
            speed_kmh=num(payload, "speed"),
            outside_temp_c=num(payload, "ext_temp", "extTemp"),
            timestamp=_time(num(payload, "utc")),
            charges=truth(payload, "is_charging", "isCharging"))
