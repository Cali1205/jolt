"""The vehicle signals jolt knows: one name, one unit, one meaning.

Every source speaks this vocabulary at its boundary: the OBD platform
profiles (through `observations.LIVE_RAW`), the logger API, the manufacturer
clouds (`cloud/snapshot.py`, `cloud/stellantis.py`) and the VW history
export (`observations.HISTORY_FIELDS`). Nothing behind the boundary knows a
source's own names. docs/data-model.md shows this table; tools/check_signals.py
keeps the two in step and checks every source against it.

The tiers:

- required: recording, learning and the logbook do not work without it. That
  is the charge level alone. Time is implicit in every report, and the
  position comes from the phone while recording when the car gives none.
- recommended: plans and the logbook get accurate with it. Without the
  odometer a drive's distance comes from GPS, without the outside or battery
  temperature the consumption and charging models guess, without charge
  power or state a charge is only seen from the charge level.
- optional: shown and stored, nothing depends on it.
"""
from dataclasses import dataclass
from typing import Literal

Kind = Literal["number", "bool", "enum", "text"]
Tier = Literal["required", "recommended", "optional"]

TIERS: tuple[Tier, ...] = ("required", "recommended", "optional")


@dataclass(frozen=True)
class Signal:
    name: str
    unit: str | None
    kind: Kind
    tier: Tier
    label: str
    meaning: str
    values: tuple[str, ...] = ()


SIGNALS: tuple[Signal, ...] = (
    Signal("soc_pct", "%", "number", "required", "Ladestand",
           "Charge level as the dashboard shows it."),
    Signal("odometer_km", "km", "number", "recommended", "Kilometerstand",
           "The car's odometer; drive distances and the logbook."),
    Signal("speed_kmh", "km/h", "number", "recommended", "Tempo",
           "Speed; the consumption model's speed factor is checked against it."),
    Signal("outside_temp_c", "°C", "number", "recommended", "Außentemperatur",
           "Outside temperature for the consumption model."),
    Signal("batt_temp_c", "°C", "number", "recommended", "Batterietemperatur",
           "One battery temperature; limits charging power in the cold."),
    Signal("charge_power_kw", "kW", "number", "recommended", "Ladeleistung",
           "Power into the battery while charging; measured charging curve."),
    Signal("charge_state", None, "enum", "recommended", "Ladezustand",
           "Whether and how the car charges.",
           ("charging", "ready", "not_ready", "purpose_reached",
            "conservation", "error")),
    Signal("plugged", None, "bool", "recommended", "Stecker",
           "Charging cable plugged in; ends a drive in the logbook."),
    Signal("lat", "°", "number", "optional", "Breitengrad",
           "Latitude (WGS84). While recording the phone supplies it."),
    Signal("lon", "°", "number", "optional", "Längengrad",
           "Longitude (WGS84). While recording the phone supplies it."),
    Signal("position_at", None, "text", "optional", "Position von",
           "When the car fixed its position (ISO 8601, UTC)."),
    Signal("range_km", "km", "number", "optional", "Reichweite",
           "The car's own range estimate."),
    Signal("energy_kwh", "kWh", "number", "optional", "Energieinhalt",
           "Energy now in the battery."),
    Signal("capacity_kwh", "kWh", "number", "optional", "Kapazität",
           "Usable capacity the car reports; state of health."),
    Signal("battery_health_pct", "%", "number", "optional", "Akku-Gesundheit",
           "State of health as the car reports it."),
    Signal("batt_temp_min_c", "°C", "number", "optional", "Batterie kältester Punkt",
           "Coldest battery cell or sensor."),
    Signal("batt_temp_max_c", "°C", "number", "optional", "Batterie heißester Punkt",
           "Hottest battery cell or sensor."),
    Signal("charge_type", None, "enum", "optional", "Ladeart",
           "AC or DC while charging.", ("AC", "DC")),
    Signal("charge_remaining_min", "min", "number", "optional", "Restladezeit",
           "Minutes until the charge target, as the car estimates."),
    Signal("target_soc_pct", "%", "number", "optional", "Ladeziel",
           "Charge limit set in the car."),
    Signal("voltage_v", "V", "number", "optional", "Spannung",
           "High-voltage pack voltage."),
    Signal("current_a", "A", "number", "optional", "Strom",
           "Pack current; positive means discharging."),
    Signal("climate_state", None, "text", "optional", "Klimatisierung",
           "Climate control state in lower case, as the car says it."),
    Signal("locked", None, "bool", "optional", "Verriegelt",
           "Doors locked."),
)

BY_NAME: dict[str, Signal] = {s.name: s for s in SIGNALS}
CHARGE_STATES = BY_NAME["charge_state"].values

# Names in the logbook, in /api/vehicles/{id}/signals and in the UI.
SOURCE_LABELS = {
    "jolt": "jolt (Dongle/App)",
    "vw_portal": "VW-Portal",
    "vw_history": "VW-Export",
    "stellantis": "Stellantis",
}


def unknown(names) -> list[str]:
    """The names that are not catalog signals, sorted."""
    return sorted(set(names) - BY_NAME.keys())


def markdown_table() -> str:
    """The catalog as it stands in docs/data-model.md."""
    lines = ["| Signal | Unit | Kind | Tier | Label | Meaning |",
             "|---|---|---|---|---|---|"]
    for tier in TIERS:
        for s in SIGNALS:
            if s.tier != tier:
                continue
            kind = s.kind + (": " + ", ".join(f"`{v}`" for v in s.values)
                             if s.values else "")
            lines.append(f"| `{s.name}` | {s.unit or ''} | {kind} | {s.tier} "
                         f"| {s.label} | {s.meaning} |")
    return "\n".join(lines)
