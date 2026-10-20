# Vehicle data model

jolt works with any electric car that can tell it its charge level. Everything
else a car or a cloud reports makes plans and the logbook more accurate, but
is not needed. This page lists the signals jolt understands, what each source
has to deliver, and how a new car platform or a new cloud source plugs in.

The catalog lives in one place, `backend/app/signals.py`. Every source
translates its own names into these signal names at its boundary; nothing
behind it knows a source's own vocabulary.

## The minimum set

| Tier | Signals | Why |
|---|---|---|
| Required | `soc_pct` | Recording, learning the consumption and the logbook all start from the charge level. Time is part of every report. A car without a position is fine: while recording, the phone supplies `lat` and `lon`. |
| Recommended | `odometer_km`, `speed_kmh`, `outside_temp_c`, `batt_temp_c`, `charge_power_kw`, `charge_state`, `plugged` | Accuracy. Without the odometer a drive's distance comes from GPS; without temperatures the consumption and charging models assume; without charge power a charge is only seen from the rise of the charge level, and the measured charging curve stays empty. |
| Optional | everything else | Shown and stored; no calculation depends on it. |

The tab "Fahrzeug" shows the card "Daten": for every required and recommended
signal whether jolt has seen it for this car, when, and from which source. A
missing required signal is marked red. The same comes from
`GET /api/vehicles/{id}/signals`.

## Signals

The table is generated from `backend/app/signals.py`
(`python tools/check_signals.py --write`); `tools/check_signals.py` fails when
the two differ.

<!-- signals:start -->
| Signal | Unit | Kind | Tier | Label | Meaning |
|---|---|---|---|---|---|
| `soc_pct` | % | number | required | Ladestand | Charge level as the dashboard shows it. |
| `odometer_km` | km | number | recommended | Kilometerstand | The car's odometer; drive distances and the logbook. |
| `speed_kmh` | km/h | number | recommended | Tempo | Speed; the consumption model's speed factor is checked against it. |
| `outside_temp_c` | °C | number | recommended | Außentemperatur | Outside temperature for the consumption model. |
| `batt_temp_c` | °C | number | recommended | Batterietemperatur | One battery temperature; limits charging power in the cold. |
| `charge_power_kw` | kW | number | recommended | Ladeleistung | Power into the battery while charging; measured charging curve. |
| `charge_state` |  | enum: `charging`, `ready`, `not_ready`, `purpose_reached`, `conservation`, `error` | recommended | Ladezustand | Whether and how the car charges. |
| `plugged` |  | bool | recommended | Stecker | Charging cable plugged in; ends a drive in the logbook. |
| `lat` | ° | number | optional | Breitengrad | Latitude (WGS84). While recording the phone supplies it. |
| `lon` | ° | number | optional | Längengrad | Longitude (WGS84). While recording the phone supplies it. |
| `position_at` |  | text | optional | Position von | When the car fixed its position (ISO 8601, UTC). |
| `range_km` | km | number | optional | Reichweite | The car's own range estimate. |
| `energy_kwh` | kWh | number | optional | Energieinhalt | Energy now in the battery. |
| `capacity_kwh` | kWh | number | optional | Kapazität | Usable capacity the car reports; state of health. |
| `battery_health_pct` | % | number | optional | Akku-Gesundheit | State of health as the car reports it. |
| `batt_temp_min_c` | °C | number | optional | Batterie kältester Punkt | Coldest battery cell or sensor. |
| `batt_temp_max_c` | °C | number | optional | Batterie heißester Punkt | Hottest battery cell or sensor. |
| `charge_type` |  | enum: `AC`, `DC` | optional | Ladeart | AC or DC while charging. |
| `charge_remaining_min` | min | number | optional | Restladezeit | Minutes until the charge target, as the car estimates. |
| `target_soc_pct` | % | number | optional | Ladeziel | Charge limit set in the car. |
| `voltage_v` | V | number | optional | Spannung | High-voltage pack voltage. |
| `current_a` | A | number | optional | Strom | Pack current; positive means discharging. |
| `climate_state` |  | text | optional | Klimatisierung | Climate control state in lower case, as the car says it. |
| `locked` |  | bool | optional | Verriegelt | Doors locked. |
<!-- signals:end -->

## Where the signals come from

| Source | Path into jolt | Names at the boundary |
|---|---|---|
| OBD2 dongle (platform profile) | `live_points`: `soc`, `speed_kmh`, `outside_temp_c` columns, the rest in `raw_values` | Catalog names, except the few in `observations.LIVE_RAW` (below) |
| Logger in the car | `POST /api/live/report`, same `live_points` | `soc`, `lat`, `lon`, `speed_kmh`, `outside_temp_c`, `raw_values` |
| VW EU Data Act portal | `vehicle_snapshots.values` (source `vw_portal`) | `cloud/snapshot.py` `FIELDS` |
| VW history export | logbook only (`tools/import_vw_history.py`) | `cloud/observations.py` `HISTORY_FIELDS` |
| Stellantis (Opel, Peugeot, Citroën, DS) | `vehicle_snapshots.values` (source `stellantis`) | `cloud/stellantis.py` `parse_status` |

The dongle has two names of its own. `soc_raw` is the platform's raw
charge-level value; the platform converts it, and the result is stored as the
point's `soc`, which is the catalog's `soc_pct`. The counters
`discharge_kwh` and `charged_kwh` are not signals: the logbook derives the
energy a drive used from them. A handful of older dongle names map onto
catalog names (`observations.LIVE_RAW`):

| Dongle name | Signal |
|---|---|
| `batterie_c` | `batt_temp_c` |
| `battery_kwh` | `capacity_kwh` |
| `energy_content_kwh` | `energy_kwh` |
| `soh_pct` | `battery_health_pct` |

## Adding an OBD2 platform

1. Write the profile in `frontend/platforms/` as [obd.md](obd.md#adding-a-platform)
   describes. The one required row is `soc_raw`.
2. Name every other row after the catalog signal it reads (`odometer_km`,
   `speed_kmh`, `outside_temp_c`, `batt_temp_c`, ...). A value the catalog
   does not have keeps a name of its own; it is stored and shown in the
   diagnostics, and nothing evaluates it.
3. Run `node tools/check_platforms.js` and `python tools/check_signals.py`.
   The second prints which catalog signals each platform delivers.
4. Record a drive. The card "Daten" in the tab "Fahrzeug" shows what arrived.

## Adding a cloud source

1. Write a client in `backend/app/cloud/<source>.py` with `vehicles()`
   (the account's VINs) and `updates(vin)` (new `Snapshot`s, oldest first).
   Translate the source's fields into catalog names in that file, and
   normalise enums there (`charge_state` to the catalog's values), so a
   stored snapshot never holds a source's own spelling. Declare the signals
   it can deliver as `PROVIDES`.
2. Raise `cloud.SourceError` (or a subclass) for anything the owner can act
   on. Its text goes into the status and the log, so it must not carry
   passwords, tokens or a full VIN (`cloud.mask`).
3. Register it in `SOURCES` in `backend/app/cloud/__init__.py`: a function
   that reads its environment variables and returns a `Config`, or `None`
   when the source is off. Sources whose tokens rotate keep them in
   `cloud_credentials` (`cloud/credentials.py`), never in a file or the
   environment.
4. Add the source to the preferences in `cloud/logbook.py` and a label to
   `signals.SOURCE_LABELS`. Its snapshots then feed the logbook like the VW
   portal's.
5. Pass its variables through `docker-compose.yml`, document them in
   `.env.example`, and write a check against a fake API in
   `tools/check_<source>.py`. `tools/check_signals.py` checks `PROVIDES`
   against the catalog.

[vw-portal.md](vw-portal.md) and [stellantis.md](stellantis.md) describe the
two cloud sources there are.

## The static vehicle profile

Signals are what the car says while it lives. The profile in the tab
"Fahrzeug" is what jolt needs to know before the first kilometre. The
minimum for a usable plan:

| Field | Used for |
|---|---|
| `battery_net_kwh` | Usable capacity: charge level to kWh and back |
| `curb_mass_kg`, `payload_kg` | Mass for climbing and accelerating |
| `c_w`, `frontal_area_m2` | Air drag |
| `c_rr` | Rolling resistance |
| `eta_drive`, `eta_regen` | Drivetrain and regeneration efficiency |
| `charge_curve` or a measured curve | Charging time per stop; the logbook measures one from DC charges with `charge_power_kw` |
| `max_charge_power_kw` | Caps the charging curve |
| `connector_type` | Which chargers fit |

The templates in `backend/app/charging/curves.py` fill all of them for the
cars jolt knows. Driving and charging refine `correction_factor`, the
measured capacity (`capacity_kwh`) and the measured charging curve.
