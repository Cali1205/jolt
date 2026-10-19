# VW MEB: what the car provides

Background to the `meb` platform profile (`frontend/platforms/meb.js`). How
profiles work in general is in [obd.md](obd.md).

An ELM327 dongle on a MEB vehicle (ID.3, ID.4, ID.Buzz, Enyaq,
Q4 e-tron, Cupra Born) reads **nothing** via the standardised OBD2 PIDs — they are
meant for combustion engines. Everything goes through manufacturer-specific
UDS queries, and the knowledge about them is in three sources that
partly contradict one another:

* [spot2000/Volkswagen-MEB-EV-CAN-parameters](https://github.com/spot2000/Volkswagen-MEB-EV-CAN-parameters)
  — 193 parameters with addresses; for many the conversion is missing
* [meatpiHQ/wican-fw](https://github.com/meatpiHQ/wican-fw/blob/main/vehicle_profiles/vw/ev_meb.json)
  — vehicle profile with formulas, names the ID.Buzz explicitly
* [codingABI/id3esp32obd2](https://github.com/codingABI/id3esp32obd2)
  — ESP32 logger, reads the CAN frames directly

Where they contradict one another, the section below states which version jolt follows and why.
The list itself is in `frontend/platforms/meb.js`; **that is the
reference**, this table is its explanation.

### Target addresses

A vehicle speaks on **two frame widths**. That is the reason why
climate and battery values did not arrive at all at first: the handshake sets `ATSP7`
— 29 bit —, and nobody listens on an 11-bit identifier then.

| Device | Protocol | ATCP | ATSH | ATCRA | ATFCSH |
|---|---|---|---|---|---|
| Battery (BMS) | 7 (29 bit) | `17` | `FC007B` | `17FE007B` | `17FC007B` |
| Vehicle | 7 (29 bit) | `17` | `FC0076` | `17FE0076` | `17FC0076` |
| DC/DC converter | 7 (29 bit) | `17` | `FC00B9` | `17FE00B9` | `17FC00B9` |
| Climate | **6 (11 bit)** | `00` | `746` | `7B0` | `746` |
| Battery (capacity) | **6 (11 bit)** | `00` | `710` | `77A` | `710` |

`0x746` and `0x710` fit in eleven bits, `0x17FC007B` only in 29. For the two
lower rows jolt briefly switches to `ATSP6` and back in the `finally`.

### Flow control — the trick without which half is missing

```
ATFCSH<header>   ATFCSD300000   ATFCSM1
```

If a response does not fit into one CAN frame, the asker must send back a
flow-control packet. The ELM327 does this itself — but only if
it knows the header, and with the MEB addresses it guesses wrong. **Without these three
commands every multi-part response fails silently.** Affected were
battery current, energy counters, range and compressor — four of the
most interesting values.

### The measured values

`b[0]` is the first byte **after** the acknowledgement (`62` + data identifier), i.e.
`g_dataBuffer[0]` at codingABI and `B4` at spot2000/WiCAN.

| Value | DID | Device | Rate | Conversion | Remark |
|---|---|---|---|---|---|
| State of charge (raw) | `22028C` | BMS | every | `b0/2.5` | Required — without it the round is discarded |
| Voltage | `221E3B` | BMS | every | `[b0:b1]/4` | ~377 V at 79 % |
| **Current** | `221E3D` | BMS | every | `([b0:b3]−150000)/100` | **multi-part**; negative = discharge |
| **Total discharged** | `221E32` | BMS | every | `\|[b12:b15]\|/8583.07` | **multi-part, signed** |
| Total charged | ↑ | BMS | every | `[b8:b11]/8583.07` | from the same response |
| Charge limit | `221E1B` | BMS | every | `[b0:b1]/5` | |
| Operating mode | `227448` | BMS | every | `b0` | Bit 2 = charging |
| Battery heater current | `221620` | BMS | every | `b0/4` | battery heater only (0 A on warm drives); cabin heating is derived: auxiliary loads − DC/DC power |
| Speed | `22F40D` | BMS | every | `b0` | |
| Battery temperature | `222A0B` | BMS | 10 | `b0/2−40` | more accurate than the outside temperature for the charging curve |
| Auxiliary loads | `220364` | Vehicle | every | `[b0:b1]/10` | everything except the drive |
| **Odometer** | `22295A` | Vehicle | every | `[b0:b2]` | whole km; corrects the GPS distance |
| DC/DC current | `22465B` | DC/DC | 10 | `[b0:b1]/16` | |
| Battery temperature max / min | `221E0E` / `221E0F` | BMS | 5 | `[b0:b1]/64` | spread of the pack; unverified |
| Coolant outlet / inlet | `22189D` | BMS | 5 | `[b0:b1]/64`, `[b2:b3]/64` | heat leaving the pack; unverified |
| Circulation pump | `22743B` | BMS | 5 | `b0` % | unverified |
| Discharge limit | `221E1C` | BMS | 10 | `[b0:b1]/5` | counterpart of the charge limit |
| Driving position | `22210E` | Vehicle | 2 | `b1` (08 P, 07 R, 06 N, 05 D, 0C B) | B = strong recuperation; unverified |
| Cell voltage highest / lowest | `221E33` / `221E34` | BMS | 5 | `[b0:b1]/4096` V | balancing state; scale from evDash, bounded 2.0-4.5 V |
| Motor torque rear / front | `22033B` / `220335` | Vehicle | every / 20 | signed `[b0:b1]` Nm | from danjulio/ev_info_display; front only on all-wheel cars |
| Charging state | `2215D5` | AKKU11 | 10 | `b0` (01 ready, 02 none, 03 error, 05 end, 16 AC, 18 trickle) | from codingABI |
| Cabin heater (PTC air) | `220801` | Climate | 4 | raw windows `ptc_air_b0/b1/b3/b5` | conversion missing in the list; collected raw |
| Energy content | `222AB8` | AKKU11 | 10 | `[b2:b3] / 40` kWh | OBDb (ID.4); the first attempt with four bytes gave a constant 0.001 |
| Average consumption, 7 categories | `222AB7` | AKKU11 | 40 | 16 bit each, `/10` | OBDb; categories unnamed |
| Charge level fine | `223DEF` | Vehicle | 2 | signed `[b0:b1] / 100` % | OBDb |
| Battery current (motor controller), HV auxiliary current | `22035E`, `220360` | Vehicle | 2 / 5 | signed `/10` A, `/100` A | OBDb |
| Compressor speed, refrigerant pressure, evaporator temperature | `222603`, `222605`, `222612` | Climate | 8 | `b0*100`, `b0*0.2`, signed `/10` | OBDb |
| Refrigerant pressure / temperature, sensor 1 | `224EEF` | Climate | 8 | `[b0:b1]*0.0088` bar, `[b2:b3]/10-40` | OBDb |
| 12 V battery | `222AF7` | AKKU11 | 20 | V `/1000+4`, A signed `/1000`, SoC `b7`, temp `b8-40` | OBDb |
| DC/DC low side, DC/DC voltage | `222A53`, `22465D` | AKKU11, DC/DC | 10 | `b0/10` V, `[b1:b2]-511` A, `/512` V | OBDb |
| **Test block** (53 values, every 6th-14th round) | many | all | 6-14 | see the block at the end of `frontend/platforms/meb.js` | Drives of 2026-10-14: arrived and plausible: energy content (0.68 kWh per %, falls with the charge level), fine charge level, HV auxiliary current (x voltage = auxiliary power), 18 module temperatures, cell SoC, HV voltages, 12 V battery, DC/DC, motor speed, torque request, acceleration, cooling duties, climate flaps/vents, AC charger (zero while not charging). Removed as dead or redundant: pedal (0.39/0.78 %), fans `222606`, outside humidity `2227C3`, motor speed `22F40C`, range reserve/estimate, BMS odometer. Open: evaporator temperature (47-53 degC, name doubtful), average consumption (constant, unit unknown), motor battery current (not comparable, read in a different round), motor power limit (0 so far). Raw values per point: up to 250 keys / 24 kB. |

| Battery capacity | `222AB2` | Battery | 40 | `[b0:b3]/1310.77/1000` | measured, not brochure |
| Range | `222AB6` | Battery | 10 | `[b0:b1]` | **multi-part** |
| Outside temperature | `222609` | Climate | 20 | `b0/2−50` | |
| Inside temperature | `222613` | Climate | 20 | `[b0:b1]/5−40` | |
| **Climate compressor** | `220800` | Climate | 20 | `[b5:b6]` W | **multi-part**; `b0` bit 0 = on, `[b3:b4]` = speed |

**State of health.** `222AB2` (battery capacity as the car reports it, read every 40th round) is compared with the profile value (new): 72.8 of 77 kWh is 94.5 %. The vehicle view shows it, `GET /api/vehicles` returns `soh_percent` and `capacity_history` (one row per change of 0.2 kWh, at least weekly, with the odometer reading; table `capacity_history`). The same capacity is also what all kWh conversions use.

"Rate" is the round count: `every` means every measurement, `20` every twentieth.
Values that change slowly or cost a protocol switch are read rarely.

### Where the sources contradict each other

| Value | jolt follows | discarded |
|---|---|---|
| **Current** `221E3D` | spot2000 + codingABI: `([b0:b3]−150000)/100` | WiCAN: `(150000−[b1:b5])/100` — yields −383,731 A on the vehicle |
| **Range** `222AB6` | codingABI: `[b0:b1]` → 297 km | WiCAN: `[b1:b2]` → 10,497 km |
| **Capacity** `222AB2` | codingABI: four bytes | WiCAN: two bytes × 50 — the same formula, coarser |

### What no source knew

The **compressor power** is in none of the three lists; spot2000 lists
`220800` with "equation missing". A differential measurement on the vehicle
settled it — once with and once without the compressor running:

```
          b0    b1b2   b3b4   b5b6   b7
off     0x10       0      0      0    0
on      0x51    9408   9408   2618   14
partial         3648   3712    935    5
```

`b5b6` is the power in watts (0 / 935 / 2618), `b1b2` and `b3b4` run
alike and much higher — setpoint and actual speed. As watts, 9.4 kW would be too much for
a climate compressor. The toolbox for this is under
`/obd` → „Klimakompressor eingrenzen" ("Narrow down climate compressor").

### Not implemented

* CO2 (`2242DB`), humidity (`2227C4`), seat heating (`223ADF`/`223AE0`) and
  recirculation (`22263B`) — not answered or constant on the ID.Buzz.
* Cell voltages and temperatures (spot2000 lists over a hundred of them) — irrelevant for
  route planning.

### Two states of charge

MEB vehicles deliver **two**: that of the battery management and that of the
display. `reserve_soc` and `target_soc` refer to the display one.

```
gross = b0 / 2.5
display = gross · 51/46 − 6.4        (clamped to 0…100)
```

Anyone who takes the wrong one is permanently off by the hidden buffer —
at 79 % display that is a good two percentage points.
