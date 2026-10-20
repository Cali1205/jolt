"""One stream of timed observations, whatever source the data came from.

Drive and charge detection (`sessions.py`) should not care whether a value
came from the VW history export, from a cloud snapshot or from jolt's own
dongle. Each source gets an adapter that turns its rows into
`Observation(t, field, value)`; the field is a catalog signal
(app/signals.py) with its unit, numbers as float. Detection only ever sees
that.

One field is not a signal: `energy_net_kwh`, lifetime discharged minus
charged energy, derived from the dongle's two counters.

Everything here is pure.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from .. import signals
from . import snapshot

Value = float | str | bool

ENERGY_NET = "energy_net_kwh"
DERIVED = frozenset({ENERGY_NET})


@dataclass(frozen=True, slots=True)
class Observation:
    t: datetime          # aware UTC
    field: str
    value: Value


def _number(text) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(text)
    return number


def charge_type(text) -> str:
    kind = str(text).strip().upper()
    if kind not in ("AC", "DC"):
        raise ValueError(text)
    return kind


def plugged(text) -> bool:
    state = str(text).strip().lower()
    if state not in ("connected", "disconnected"):
        raise ValueError(text)
    return state == "connected"


# The VW history export's adapter: dataFieldName -> (signal, converter)
HISTORY_FIELDS: dict[str, tuple[str, Callable]] = {
    "mileage": ("odometer_km", _number),
    "currentSOCInPct": ("soc_pct", _number),
    "cruisingRangeElectricInKm": ("range_km", _number),
    "chargingState": ("charge_state", snapshot.charge_state),
    "chargePowerInKW": ("charge_power_kw", _number),
    "chargeType": ("charge_type", charge_type),
    "plugConnectionState": ("plugged", plugged),
    "climatisationState": ("climate_state", snapshot.climate_state),
}


def history_point(raw: dict) -> Observation | None:
    """One `Data` entry of the history export, or None when jolt does not
    use the field or the entry has no usable time or value."""
    known = HISTORY_FIELDS.get(raw.get("dataFieldName"))
    if known is None:
        return None
    t = snapshot.parse_time(raw.get("timestampUtc"))
    # Some entries carry 0001-01-01 or "N/A" instead of a time.
    if t is None or t.year < 2000:
        return None
    field, convert = known
    try:
        return Observation(t, field, convert(raw.get("value")))
    except (TypeError, ValueError):
        return None


def from_history(points: Iterable[dict]) -> list[Observation]:
    return [o for o in map(history_point, points) if o is not None]


def _aware(t: datetime) -> datetime:
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def _observation(t: datetime, name: str, value) -> Observation | None:
    """A catalog value as an observation; None for anything else."""
    signal = signals.BY_NAME.get(name)
    if signal is None or value is None:
        return None
    if signal.kind == "number":
        if isinstance(value, bool):
            return None
        try:
            value = _number(value)
        except (TypeError, ValueError):
            return None
    return Observation(t, name, value)


# A stored snapshot holds catalog values. Only the VW portal keeps the merged
# state in `values` and the report itself in `raw`; its own reading applies.
REPORT_VALUES: dict[str, Callable[[dict | None, dict | None], dict]] = {
    "vw_portal": snapshot.report_values,
}


def snapshot_values(row) -> dict:
    """What one stored snapshot says, as catalog signals."""
    own = REPORT_VALUES.get(row.source)
    return own(row.values, row.raw) if own else dict(row.values or {})


def from_snapshots(rows: Iterable) -> list[Observation]:
    """Stored snapshots (`vehicle_snapshots` rows, or anything with `source`,
    `captured_at`, `values` and `raw`), each at the time the car reported."""
    out = []
    for row in rows:
        t = _aware(row.captured_at)
        for name, value in snapshot_values(row).items():
            o = _observation(t, name, value)
            if o is not None:
                out.append(o)
    return out


# Dongle names that predate the catalog. `soc_raw` is not here: the platform
# converts it, and the result is the point's `soc` column.
LIVE_RAW = {
    "batterie_c": "batt_temp_c",
    "battery_kwh": "capacity_kwh",
    "energy_content_kwh": "energy_kwh",
    "soh_pct": "battery_health_pct",
}


def live_values(point) -> dict:
    """One `LivePoint` as catalog signals: its columns, and its raw values
    under their catalog names. The position is the phone's while recording."""
    values = {"lat": point.lat, "lon": point.lon, "soc_pct": point.soc,
              "speed_kmh": point.speed_kmh, "outside_temp_c": point.outside_temp_c}
    raw = point.raw_values if isinstance(point.raw_values, dict) else {}
    for name, value in raw.items():
        name = LIVE_RAW.get(name, name)
        if name in signals.BY_NAME and values.get(name) is None:
            values[name] = value
    return {k: v for k, v in values.items() if v is not None}


def from_live_points(points: Iterable) -> list[Observation]:
    """jolt's own measurement points (`LivePoint`): the charge level, the
    odometer and the dongle's energy counters."""
    out = []
    for p in points:
        t = _aware(p.timestamp)
        values = live_values(p)
        soc = _observation(t, "soc_pct", values.get("soc_pct"))
        if soc is not None:
            out.append(soc)
        odometer = _observation(t, "odometer_km", values.get("odometer_km"))
        if odometer is not None and odometer.value > 0:
            out.append(odometer)
        raw = p.raw_values if isinstance(p.raw_values, dict) else {}
        discharged = raw.get("discharge_kwh")
        if isinstance(discharged, (int, float)):
            charged = raw.get("charged_kwh")
            charged = charged if isinstance(charged, (int, float)) else 0.0
            out.append(Observation(t, ENERGY_NET, float(discharged - charged)))
    return out
