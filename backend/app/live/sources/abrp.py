"""The telemetry format of Iternio (A Better Routeplanner).

Why this one of all: it is the only one that is something like a de-facto
standard among electric-car loggers. Whoever gets live data out of a car is
quite likely speaking this format - the ABRP app itself, ESP32 dongles such
as the WiCAN, OVMS, Home Assistant integrations, a number of hobby scripts. A
translator for it is therefore not an adapter for one vendor, but for half an
ecosystem.

It is deliberately independent of *how* the data arrives: the same fields
appear in a POST to `/1/tlm/send` as in the response to
`/1/tlm/get_telemetry`. Whether jolt is sent them or fetches them is a matter
of transport and does not belong here.

Format (the fields jolt needs - there are more):

    utc          seconds since epoch, time of the measurement
    soc          state of charge in percentage points, 0 to 100
    lat, lon     position, decimal degrees
    speed        km/h
    ext_temp     outside temperature in °C
    is_charging  0/1
"""
from datetime import datetime, timezone

from . import (SourcesError, RawPoint, limits, required, truth, num)

# From this raw value on, `utc` means milliseconds and not seconds. Seconds
# only reach this magnitude in the year 5138; milliseconds are above it today.
# Mixing the two up is the most common error at this spot, and without
# correction it only shows when the time factor turns out nonsensical.
MILLISECONDS_FROM = 1e11

# A timestamp outside this range is not a measurement but an unset clock -
# usually 1970, when a microcontroller without network starts up.
EARLIEST = datetime(2020, 1, 1)
LATEST = datetime(2100, 1, 1)


def _unpack(records: dict) -> dict:
    """Get the payload out of its envelope.

    When sending it sits under `tlm`, when fetching under `result` - and
    whoever passes on a recorded dataset by hand has usually already
    unpacked it. All three cases are the same format in different packaging,
    and nobody should fail on that.
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
    # Continue without time zone, because jolt consistently works with naive
    # UTC (`datetime.utcnow()`); one point with a time zone among points
    # without would make every comparison fail with a TypeError.
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

        # Deliberately no conversion of a fraction (0 to 1) to percentage
        # points: a `soc` of 0.4 is meant either as "40 %" or as "0.4 %", and
        # both occur. Guessing would mean guessing precisely when the battery
        # is almost empty - where a wrong number leaves the driver stranded.
        soc = limits(required(payload, "soc"), 0, 100, "Ladestand")

        return RawPoint(
            lat=limits(required(payload, "lat"), -90, 90, "Breitengrad"),
            lon=limits(required(payload, "lon"), -180, 180, "Längengrad"),
            soc=soc,
            speed_kmh=num(payload, "speed"),
            outside_temp_c=num(payload, "ext_temp", "extTemp"),
            timestamp=_time(num(payload, "utc")),
            charges=truth(payload, "is_charging", "isCharging"))
