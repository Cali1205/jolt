# The OBD2 dongle and vehicle platforms

jolt reads the car through a Bluetooth OBD2 dongle: charge level, energy
counters, odometer, temperatures and more. Planning works without a dongle;
recordings then rely on GPS and charge levels typed in by hand.

## What you need

- **A Bluetooth Low Energy ELM327 adapter.** Tested is the Vgate iCar Pro 2S
  (ELM327 v2.3). The app looks for devices whose name starts with `IOS-Vlink`,
  `Vlink`, `OBD`, `Vgate` or `VEEPEAK` and otherwise shows all devices.
  Classic-Bluetooth adapters (SPP) and Wi-Fi adapters do not work: neither
  browsers nor iOS can talk to them.
- **A browser with Web Bluetooth, over HTTPS.** Chrome or Edge on Android and
  desktop. Safari on iOS has no Web Bluetooth; use the
  [Bluefy](https://apps.apple.com/app/bluefy-web-ble-browser/id1492822055)
  browser or build the [iOS app](ios-app.md).
- **The right platform in the vehicle profile** (tab "Fahrzeug", field
  "Plattform (OBD2)"). It decides which control units and data identifiers the
  dongle asks.

Plug the dongle in, open the tab "Live" or start a recording, and allow the
Bluetooth device once. After that it reconnects without asking.

The dongle only asks the car while you drive: asking a locked car can set off
the alarm. The details (movement detection, the 12 V voltage via `ATRV`) are
in [features.md](features.md).

## Platforms

| Platform | Cars | State |
|---|---|---|
| `meb` | VW ID.3, ID.4, ID.5, ID.7, ID.Buzz, Skoda Enyaq, Cupra Born, Audi Q4 e-tron | Used on an ID.Buzz since 2026; about 100 values. Background: [obd-meb.md](obd-meb.md) |
| `ecmp` | Stellantis e-CMP: Opel Corsa-e / Corsa Electric, Peugeot e-208, e-2008, Opel Mokka-e | From public sources (OBDb, OVMS), **not yet tried on a car** |
| `obd2` | Anything else: standard OBD2 (SAE J1979) | Standard formulas, **not tried on a car**. Only works if the car reports PID `5B` (battery charge level), which many electric cars do not |

The diagnostics page shows what arrives: tab "Einstellungen", section
"Dongle-Diagnose", or the separate page `/obd`. Each value lists hits, empty
answers and failures. "Bericht kopieren" copies a report you can attach to an
issue.

## Without a dongle

Volkswagen group cars can send their last reported values (charge level,
range, odometer) to the VW EU Data Act portal, and jolt can fetch them from
there. It is no live data: the values are as old as the car's last report,
usually a few minutes to hours. jolt uses them as the start charge level of a
recording when the dongle gives none, and shows them in the tab "Fahrzeug".
Setup: [vw-portal.md](vw-portal.md).

## Adding a platform

A platform is one file in `frontend/platforms/` that registers itself:

```js
(window.joltPlatforms = window.joltPlatforms || {}).myplatform = {
  id: "myplatform",
  title: "Brand platform (models)",
  handshake: ["ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATSP6", "ATCAF1",
              "ATST FF", "ATSH7E4", "ATCRA7EC"],
  protocol: "6",                 // ATSP the session returns to after a switch
  soc: { bms: (raw) => raw / 2, hmi: (bms) => bms },
  addresses: {
    BMS: { sh: "7E4", cra: "7EC", fcsh: "7E4" },
  },
  vals: [
    { name: "soc_raw", title: "Rohwert SoC", did: "220101", address: "BMS",
      required: true, downhill: 6, len_total: 1 },
  ],
};
```

The header of `frontend/platforms/meb.js` documents every field: the byte
position (`downhill`, `len_total`), the conversion (`divider`, `factor`,
`offset`, `pre_offset`, `sign`, `mask`), plausibility bounds (`min`, `max`),
how often a value is read (`rarely`), several values from one answer
(`also`), and the address fields (`cp` for 29-bit identifiers, `sh`, `cra`,
`fcsh`, `trace_log` for a protocol switch).

Rules the rest of the app relies on:

- Exactly one row has `required: true`, and it is named `soc_raw`. It holds
  the platform's raw charge-level value; `soc.bms` turns it into battery
  percent, `soc.hmi` (optional) into the percent the dashboard shows.
- Name every value the car has after its signal in the catalog
  ([data-model.md](data-model.md#signals)): `odometer_km`, `speed_kmh`,
  `outside_temp_c`, `batt_temp_c`, `voltage_v`, `current_a`, and so on. The
  catalog also says which signals jolt needs and which make it more accurate.
  `soc_raw` is the dongle-side raw value; the converted percent is the
  catalog's `soc_pct`. A few dongle names predate the catalog and are mapped
  onto it (`batterie_c`, `battery_kwh`, `energy_content_kwh`, `soh_pct`, see
  [data-model.md](data-model.md#where-the-signals-come-from)); use the
  catalog name in a new profile.
- These dongle-only names are evaluated too:

| Name | Unit | Used for |
|---|---|---|
| `soc_raw` | raw | Charge level (required) |
| `discharge_kwh`, `charged_kwh` | kWh | Lifetime energy counters: consumption per minute |
| `aux_load_kw`, `dcdc_current_a` | kW, A | Heating and auxiliary loads (derived) |
| `ptc_current_a`, `compressor_w` | A, W | Battery heater and A/C compressor in the live view |

Any other name is stored with the measurement point and shown in the
diagnostics, but nothing evaluates it.

The `id` is stored with the vehicle and may only contain lower-case letters,
digits and `_` (at most 20 characters).

To make the file load, add it in the same places as the existing profiles:
the `<script>` tags in `frontend/index.html` and `frontend/obd.html`, all
three file lists in `backend/app/main.py` (search for `platforms/obd2.js`),
`SCAFFOLD` in `frontend/sw.js` (and count up `CACHE` there). Then add decode tests with example answers to
`tools/check_platforms.js`; it also checks that every profile loads without
errors and has its `soc_raw` row.

Good sources for data identifiers are [OBDb](https://github.com/OBDb) (one
repository per model), the vehicle modules of
[OVMS](https://github.com/openvehicles/Open-Vehicle-Monitoring-System-3) and
the [WiCAN vehicle profiles](https://github.com/meatpiHQ/wican-fw). Say in a
comment where each value comes from and whether it was tried on a car.

## A logger in the car

A device installed permanently in the car (for example an ESP32 with a CAN
interface) can send measurements without the app. Tab "Fahrzeug", section
"Logger im Auto", creates a token for the vehicle. The logger posts to
`POST /api/live/report`; jolt finds the running trip itself and answers
`"recorded": false` while no trip is running.

```json
{
  "token": "<logger token>",
  "lat": 48.49, "lon": 9.20, "soc": 63.5,
  "speed_kmh": 87, "outside_temp_c": 9.5,
  "timestamp": "2026-10-19T08:15:00Z",
  "raw_values": { "odometer_km": 61234, "voltage_v": 371.2 }
}
```

`lat`, `lon` and `soc` (percent) are required. With `"format": "abrp"` the
endpoint also takes the telemetry format of A Better Routeplanner
(`backend/app/live/sources/abrp.py`).
