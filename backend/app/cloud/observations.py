"""One stream of timed observations, whatever source the data came from.

Drive and charge detection (`sessions.py`) should not care whether a value
came from the VW history export, from a live portal report or from jolt's
own dongle. Each source gets an adapter that turns its rows into
`Observation(t, field, value)` with one canonical field name and one unit;
detection only ever sees that.

The canonical fields:

    odometer_km      float   the car's odometer
    soc_pct          float   displayed charge level
    range_km         float
    energy_kwh       float   energy in the battery (portal energy content)
    energy_net_kwh   float   lifetime discharged minus charged (dongle counter)
    charge_state     str     charging | ready | not_ready | purpose_reached |
                             conservation | error
    charge_power_kw  float
    charge_type      str     AC | DC
    plug_connected   bool
    climate_state    str     lower case, as the car says it

Everything here is pure.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from . import snapshot

Value = float | str | bool


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


# VW spells the same state in two ways (CHARGE_PURPOSE_REACHED_... in older
# reports, chargePurposeReached... in newer ones) and writes "conversation"
# where it means "conservation". Keys are lower case without underscores.
CHARGE_STATES = {
    "charging": "charging",
    "readyforcharging": "ready",
    "notreadyforcharging": "not_ready",
    "chargepurposereachedandnotconversationcharging": "purpose_reached",
    "chargepurposereachedandnotconservationcharging": "purpose_reached",
    "chargepurposereachedandconversation": "conservation",
    "chargepurposereachedandconservation": "conservation",
    "error": "error",
}


def charge_state(text) -> str:
    key = str(text).strip().lower().replace("_", "")
    key = key.removeprefix("chargestate")
    if key not in CHARGE_STATES:
        raise ValueError(text)
    return CHARGE_STATES[key]


def charge_type(text) -> str:
    kind = str(text).strip().upper()
    if kind not in ("AC", "DC"):
        raise ValueError(text)
    return kind


def plug_connected(text) -> bool:
    state = str(text).strip().lower()
    if state not in ("connected", "disconnected"):
        raise ValueError(text)
    return state == "connected"


def _lower(text) -> str:
    return str(text).strip().lower()


# dataFieldName in the history export -> (canonical field, converter)
HISTORY_FIELDS: dict[str, tuple[str, Callable]] = {
    "mileage": ("odometer_km", _number),
    "currentSOCInPct": ("soc_pct", _number),
    "cruisingRangeElectricInKm": ("range_km", _number),
    "chargingState": ("charge_state", charge_state),
    "chargePowerInKW": ("charge_power_kw", _number),
    "chargeType": ("charge_type", charge_type),
    "plugConnectionState": ("plug_connected", plug_connected),
    "climatisationState": ("climate_state", _lower),
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


# Snapshot value name -> (canonical field, converter). The snapshot already
# converted the units (snapshot.FIELDS).
SNAPSHOT_FIELDS: dict[str, tuple[str, Callable]] = {
    "odometer_km": ("odometer_km", _number),
    "soc_pct": ("soc_pct", _number),
    "range_km": ("range_km", _number),
    "energy_kwh": ("energy_kwh", _number),
    "charge_power_kw": ("charge_power_kw", _number),
    "charge_state": ("charge_state", charge_state),
    "climatisation_state": ("climate_state",
                            lambda v: _lower(v).removeprefix("climatisation_state_")),
}

ODOMETER_NAMES = ("mileage", "mileage.value")


def _aware(t: datetime) -> datetime:
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def snapshot_values(values: dict | None, raw: dict | None) -> dict:
    """What one stored portal report says, as snapshot value names.

    `values` is the merged state: a field the report left out keeps its
    value from an earlier report, which is stale by the time of this one. So
    when the report's own points (`raw`) are there, only they count. The
    odometer is the largest reading in the report: a switch-off report can
    carry the same field twice, the reading from mid-drive and the final one.
    """
    if not raw:
        return dict(values or {})
    points = [snapshot.Point(key=str(ident), name=str(entry[0] or ""),
                             value=str(entry[1]), timestamp=None)
              for ident, entry in raw.items()
              if isinstance(entry, list) and len(entry) >= 2
              and entry[1] not in (None, "")]
    delivered = {}
    for name, idents, convert in snapshot.FIELDS:
        point = snapshot.lookup(points, idents)
        if point is None:
            continue
        try:
            delivered[name] = convert(point.value)
        except ValueError:
            continue
    readings = []
    for p in points:
        if p.name in ODOMETER_NAMES or p.key in ODOMETER_NAMES:
            try:
                readings.append(_number(p.value))
            except ValueError:
                pass
    if readings:
        delivered["odometer_km"] = max(readings)
    return delivered


def from_snapshots(rows: Iterable) -> list[Observation]:
    """Stored portal reports (`vehicle_snapshots` rows, or anything with
    `captured_at`, `values` and `raw`), each at the time the car reported."""
    out = []
    for row in rows:
        t = _aware(row.captured_at)
        for name, value in snapshot_values(row.values, row.raw).items():
            known = SNAPSHOT_FIELDS.get(name)
            if known is None:
                continue
            field, convert = known
            try:
                out.append(Observation(t, field, convert(value)))
            except (TypeError, ValueError):
                continue
    return out


def from_live_points(points: Iterable) -> list[Observation]:
    """jolt's own measurement points (`LivePoint`): charge level, and from
    the dongle's raw values the odometer and the energy counters."""
    out = []
    for p in points:
        t = _aware(p.timestamp)
        if p.soc is not None:
            out.append(Observation(t, "soc_pct", float(p.soc)))
        raw = p.raw_values if isinstance(p.raw_values, dict) else {}
        odometer = raw.get("odometer_km")
        if isinstance(odometer, (int, float)) and odometer > 0:
            out.append(Observation(t, "odometer_km", float(odometer)))
        discharged = raw.get("discharge_kwh")
        if isinstance(discharged, (int, float)):
            charged = raw.get("charged_kwh")
            charged = charged if isinstance(charged, (int, float)) else 0.0
            out.append(Observation(t, "energy_net_kwh", float(discharged - charged)))
    return out
