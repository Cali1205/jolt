"""What the manufacturer cloud last heard from the car, as jolt's own values.

The VW EU Data Act portal delivers a flat list of data points. Each point has
a UUID `key`, a field `name`, a string `value` and its own timestamp. The same
field arrives under different keys and names depending on the model (an ID.3
reports its range under the generic name "value", an Enyaq its charge level
under a key of its own), so a value is looked up by a list of candidates, the
same ones evcc uses (vehicle/vw/eudataact/types.go), with names as fallback.

Everything here is pure: points in, `Snapshot` out. The network lives in
`vw_portal.py`.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

Value = float | int | str | bool


@dataclass(frozen=True)
class Point:
    key: str
    name: str
    value: str
    timestamp: datetime | None
    # Delivery order of the dataset the point came from; higher is newer.
    # The point's own timestamp is not reliable enough to decide that.
    seq: int = 0

    @property
    def id(self) -> str:
        return self.key or self.name


@dataclass(frozen=True)
class Snapshot:
    captured_at: datetime
    values: dict[str, Value]


def parse_time(text: str | None) -> datetime | None:
    """An ISO time from the portal as aware UTC - or None."""
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def decode(data: Iterable[dict]) -> list[Point]:
    """The `Data` list of one dataset document, newest entry per id."""
    points: dict[str, Point] = {}
    for raw in data:
        value = raw.get("value")
        if value is None or value == "":
            continue
        point = Point(key=str(raw.get("key") or ""),
                      name=str(raw.get("dataFieldName") or ""),
                      value=str(value),
                      timestamp=parse_time(raw.get("timestampUtc")))
        if not point.id:
            continue
        earlier = points.get(point.id)
        if earlier is None or _not_after(earlier.timestamp, point.timestamp):
            points[point.id] = point
    return list(points.values())


def _not_after(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return True
    return a <= b


def merge(merged: list[Point], newer: list[Point], seq: int) -> list[Point]:
    """Fold a newer dataset into the merged state; a dataset may carry only
    some of the fields, so the others keep their last delivered value."""
    by_id = {p.id: p for p in merged}
    for point in newer:
        by_id[point.id] = Point(point.key, point.name, point.value,
                                point.timestamp, seq)
    return list(by_id.values())


def _find(points: list[Point], ident: str) -> Point | None:
    return next((p for p in points if p.key == ident or p.name == ident), None)


def lookup(points: list[Point], idents: tuple[str, ...]) -> Point | None:
    """The freshest of the candidates; on equal delivery the earlier
    candidate in the list wins."""
    best = None
    for ident in idents:
        point = _find(points, ident)
        if point is not None and (best is None or point.seq > best.seq):
            best = point
    return best


def _number(text: str) -> float | int:
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(text)
    return int(number) if number.is_integer() else number


def _tenth_kwh(text: str) -> float:
    return round(float(_number(text)) * 0.1, 2)


def _text(text: str) -> str:
    return text.strip()


def _flag(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered not in ("true", "false"):
        raise ValueError(text)
    return lowered == "true"


# evcc's identifiers (types.go)
KEY_RANGE_ID3 = "0ca40e18-0564-3eda-bcc0-7aee9ef44f04"
KEY_BATTERY_STATE_REPORT_SOC = "506cb83e-f99f-3af3-bbeb-0429b69a78d9"
KEY_ENYAQ_SOC = "0a18a053-b4b0-3db1-be44-a6c5dba629b1"

CAPTURED_AT = "car_captured_time"

FIELDS: list[tuple[str, tuple[str, ...], Callable[[str], Value]]] = [
    ("soc_pct", ("battery_level_HV.value", "state_of_charge", "hv_soc",
                 KEY_BATTERY_STATE_REPORT_SOC, KEY_ENYAQ_SOC,
                 "battery_state_report.soc"), _number),
    ("range_km", ("cruising_range_secondary_engine",
                  "cruising_range_primary_engine", "cruising_range_combined",
                  KEY_RANGE_ID3), _number),
    ("odometer_km", ("mileage", "mileage.value"), _number),
    # The portal counts energy in tenths of a kWh.
    ("energy_kwh", ("energy_contents.current_energy_content.physical_value",),
     _tenth_kwh),
    ("capacity_kwh", ("energy_contents.maximal_energy_content.physical_value",),
     _tenth_kwh),
    ("batt_temp_min_c", ("min_temperature",), _number),
    ("batt_temp_max_c", ("max_temperature",), _number),
    ("charge_power_kw", ("battery_state_report.charge_power",), _number),
    ("charge_state", ("charging_state_report.current_charge_state",
                      "charging_state"), _text),
    ("target_soc_pct", ("settings.target_soc",), _number),
    ("locked", ("locked",), _flag),
    ("climatisation_state", ("climatisation_state",), _text),
]


def from_points(points: list[Point]) -> Snapshot | None:
    """The values jolt knows, or None when there is nothing to keep.

    The snapshot time is when the car reported, not when the portal was
    asked: `car_captured_time` when present, else the newest point.
    """
    values: dict[str, Value] = {}
    for name, idents, convert in FIELDS:
        point = lookup(points, idents)
        if point is None:
            continue
        try:
            values[name] = convert(point.value)
        except ValueError:
            continue
    if not values:
        return None
    captured = lookup(points, (CAPTURED_AT,))
    captured_at = parse_time(captured.value) if captured else None
    if captured_at is None:
        captured_at = max((p.timestamp for p in points if p.timestamp),
                          default=None)
    if captured_at is None:
        return None
    return Snapshot(captured_at=captured_at, values=values)
