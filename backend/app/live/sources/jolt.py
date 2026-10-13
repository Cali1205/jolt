"""jolt's own reporting format.

The simplest case - and still a translator like any other. Two reasons not to
skip it:

First, an interface with exactly one implementer would have no edges that
show whether it is any good. Second, it means jolt's own message goes through
the same checks as a foreign one. A shortcut on the phone is just as capable
of sending `soc` as a fraction rather than as percentage points as a foreign
service is.
"""
from datetime import datetime

from . import (RawPoint, SourcesError, limits, required, truth, num,
               raw_values_limited)


# Phones that already post (an iOS shortcut, a logger app) still use the field
# names from before the switch to English; keep accepting them.
LEGACY_KEYS = {"tempo_kmh": "speed_kmh", "aussentemp_c": "outside_temp_c",
               "zeit": "timestamp", "laedt": "charges", "rohwerte": "raw_values"}


def _raw_values(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    try:
        return raw_values_limited(value)
    except ValueError as error:
        raise SourcesError(str(error))


class JoltFormat:
    name = "jolt"

    def normalize(self, records: dict) -> RawPoint:
        records = {LEGACY_KEYS.get(key, key): value for key, value in records.items()}
        timestamp = records.get("timestamp")
        try:
            timestamp = (datetime.fromisoformat(timestamp)
                         if isinstance(timestamp, str) and timestamp else None)
        except ValueError:
            raise SourcesError(f"Zeitstempel unbrauchbar: {timestamp!r}")
        speed = num(records, "speed_kmh")
        temperature = num(records, "outside_temp_c")
        return RawPoint(
            lat=limits(required(records, "lat"), -90, 90, "Breitengrad"),
            lon=limits(required(records, "lon"), -180, 180, "Längengrad"),
            soc=limits(required(records, "soc"), 0, 100, "Ladestand"),
            # The same limits as the authenticated Sample model: a logger
            # is not allowed to send what the app would reject.
            speed_kmh=None if speed is None else limits(speed, 0, 500, "Geschwindigkeit"),
            outside_temp_c=None if temperature is None
            else limits(temperature, -80, 70, "Außentemperatur"),
            timestamp=timestamp,
            charges=truth(records, "charges"),
            # Taken over unchanged: a translator that tidies up here throws
            # away exactly what the field is for.
            raw_values=_raw_values(records.get("raw_values")))
