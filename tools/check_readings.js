#!/usr/bin/env node
/* The table must deliver the same numbers as the functions before it.
 *
 * Until this refactoring, every conversion was a function of its own in
 * obd-core.js. Now it is a row in readings.js, and an
 * interpreter applies it. Precisely at such a changeover, signs,
 * scaling and byte order silently go wrong - `soc_raw` is one byte
 * divided by 2.5, the battery current `(Rohwert - 150000)/100` over four
 * bytes, and both would still have looked plausible when wrong.
 *
 * That is why the **old functions are kept here unchanged** and
 * serve as the reference. Comparison is over random and deliberately
 * chosen byte sequences, including those measured on the vehicle. If a
 * row deviates, the output names the name, the bytes and both numbers.
 *
 * This file is deliberately redundant. It may only disappear once the
 * table has been confirmed in the car once.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FRONTEND = path.join(__dirname, "..", "frontend");

/* ---------- The reference: the state before the refactoring ---------- */

const OLD = {
  soc_raw: (b) => b[0],
  voltage_v: (b) => (b.length >= 2 ? (b[0] * 256 + b[1]) / 4 : null),
  current_a: (b) => (b.length >= 4
    ? ((b[0] * 16777216) + (b[1] * 65536) + (b[2] * 256) + b[3] - 150000) / 100
    : null),
  discharge_kwh: (b) => {
    if (b.length < 16) return null;
    const raw = (b[12] * 16777216) + (b[13] * 65536) + (b[14] * 256) + b[15];
    return Math.abs((raw >= 2147483648 ? raw - 4294967296 : raw) / 8583.07);
  },
  charged_kwh: (b) => (b.length >= 12
    ? ((b[8] * 16777216) + (b[9] * 65536) + (b[10] * 256) + b[11]) / 8583.07
    : null),
  charge_limit_a: (b) => (b.length >= 2 ? (b[0] * 256 + b[1]) / 5 : null),
  mode: (b) => b[0],
  ptc_current_a: (b) => (b.length ? b[0] / 4 : null),
  speed_kmh: (b) => b[0],
  aux_load_kw: (b) => (b.length >= 2 ? (b[0] * 256 + b[1]) / 10 : null),
  odometer_km: (b) => (b.length >= 3
    ? (b[0] * 65536) + (b[1] * 256) + b[2] : null),
  dcdc_current_a: (b) => (b.length >= 2 ? (b[0] * 256 + b[1]) / 16 : null),
  battery_kwh: (b) => {
    if (b.length < 4) return null;
    const raw = (b[0] * 16777216) + (b[1] * 65536) + (b[2] * 256) + b[3];
    const kwh = raw / 1310.77 / 1000;
    return (kwh >= 10 && kwh <= 200) ? kwh : null;
  },
  range_km: (b) => {
    if (b.length < 2) return null;
    const km = (b[0] * 256) + b[1];
    return (km >= 0 && km <= 999) ? km : null;
  },
  batterie_c: (b) => (b.length ? (b[0] / 2) - 40 : null),
  compressor_w: (b) => {
    if (b.length < 7) return null;
    const w = (b[5] * 256) + b[6];
    return (w >= 0 && w <= 8000) ? w : null;
  },
  compressor_upm: (b) => (b.length >= 5 ? (b[3] * 256) + b[4] : null),
  compressor_at: (b) => (b.length ? (b[0] & 1) : null),
  outside_temp_c: (b) => (b.length ? b[0] / 2 - 50 : null),
  inside_temp_c: (b) => (b.length >= 2 ? ((b[0] * 256 + b[1]) / 5) - 40 : null),
};

/* The target address and the data identifier are part of the conversion: a row
 * with the right formula and wrong address delivers nothing at all, and one
 * with the wrong data identifier the number of a different parameter. */
const OLD_ADDRESSES = {
  soc_raw: ["22028C", "FC007B", true, 0],
  voltage_v: ["221E3B", "FC007B", false, 0],
  current_a: ["221E3D", "FC007B", false, 0],
  discharge_kwh: ["221E32", "FC007B", false, 0],
  charge_limit_a: ["221E1B", "FC007B", false, 0],
  mode: ["227448", "FC007B", false, 0],
  ptc_current_a: ["221620", "FC007B", false, 0],
  speed_kmh: ["22F40D", "FC007B", false, 0],
  aux_load_kw: ["220364", "FC0076", false, 0],
  odometer_km: ["22295A", "FC0076", false, 0],
  dcdc_current_a: ["22465B", "FC00B9", false, 10],
  battery_kwh: ["222AB2", "710", false, 40],
  range_km: ["222AB6", "710", false, 10],
  batterie_c: ["222A0B", "FC007B", false, 10],
  compressor_w: ["220800", "746", false, 20],
  outside_temp_c: ["222609", "746", false, 20],
  inside_temp_c: ["222613", "746", false, 20],
};

/* ---------- Load the new one ---------- */

function newCharging() {
  const timeframe = {
    console, TextEncoder, TextDecoder, DataView, Uint8Array,
    setTimeout, clearTimeout, navigator: {}, localStorage: {
      getItem: () => null, setItem: () => {},
    },
  };
  timeframe.window = timeframe;
  const context = vm.createContext(timeframe);
  for (const file of ["readings.js", "obd-ble-native.js", "obd-core.js"]) {
    vm.runInContext(fs.readFileSync(path.join(FRONTEND, file), "utf8"),
                    context, { filename: file });
  }
  return timeframe;
}

/* ---------- Byte sequences to compare ---------- */

/* First the edge cases, then chance. The edge cases are the more important ones:
 * that is where it is decided whether a too-short response yields null instead of a
 * number computed from `undefined`. */
function probe_sequences() {
  const follow = [];
  for (let n = 0; n <= 20; n += 1) {
    follow.push(new Array(n).fill(0));                    // all zero
    follow.push(new Array(n).fill(255));                  // all set
    follow.push(Array.from({ length: n }, (_, i) => i));   // ascending
  }
  // The discharge counter measured on the vehicle: 0xF7141E0D on b12..b15.
  const measured = new Array(16).fill(0);
  measured[12] = 0xF7; measured[13] = 0x14;
  measured[14] = 0x1E; measured[15] = 0x0D;
  follow.push(measured);
  // The confirmed state of charge: raw value 0xB4.
  follow.push([0xB4]);
  // The compressor measurement "on" from the comment in readings.js.
  follow.push([0x51, 0x24, 0xC0, 0x24, 0xC0, 0x0A, 0x3A, 0x0E, 0, 0, 0]);

  let seed = 20260912;
  const cube = () => {
    // A fixed, simple generator: the run must be repeatable,
    // otherwise a red run turns green again tomorrow.
    seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF;
    return seed % 256;
  };
  for (let i = 0; i < 400; i += 1) {
    const n = 1 + (cube() % 20);
    follow.push(Array.from({ length: n }, cube));
  }
  return follow;
}

/* `auswerten` in the core pushes every value through `sauber()`, which maps undefined
 * and NaN to null. The old functions sometimes returned `undefined` for an empty
 * response (`b[0]`), the new formula gives null -
 * behind `sauber` that is the same value. So the comparison is made afterwards. */
function clean(val) {
  return (val === null || val === undefined || Number.isNaN(val))
    ? null : val;
}

/* Floating point: `roh / 1310.77 / 1000` and `roh / 1310770` are mathematically
 * the same, but not bit-identical in double precision. The difference
 * is around 1e-16 relative - meaningless for a kilowatt hour with one
 * decimal place. A real mix-up of sign,
 * divisor or byte position, on the other hand, is off by orders of magnitude. */
function same(a, b) {
  if (a === null || b === null) return a === b;
  if (a === b) return true;
  const denominator = Math.max(Math.abs(a), Math.abs(b), 1e-12);
  return Math.abs(a - b) / denominator < 1e-9;
}

/* ---------- Run ---------- */

function main() {
  const f = newCharging();
  let failure = 0;

  if (f.joltObd.TABLE_ERROR.length) {
    console.log("Tabelle meldet Fehler:");
    for (const t of f.joltObd.TABLE_ERROR) console.log("  " + t);
    failure += f.joltObd.TABLE_ERROR.length;
  }

  // The read functions live inside the module; they are reachable via the
  // detour the recording also takes. That is why the resolved table
  // is accessed directly here.
  const table = f.window.joltReadings;
  const fields = f.joltObd.FIELDS;

  // 1. Completeness: no value may have been lost in the refactoring.
  const oldNames = Object.keys(OLD).sort();
  const newNames = fields.map((x) => x.name).sort();
  const missing = oldNames.filter((n) => !newNames.includes(n));
  const surplus = newNames.filter((n) => !oldNames.includes(n));
  console.log(`Messwerte: ${newNames.length} in der Tabelle, `
              + `${oldNames.length} vorher`);
  if (missing.length) {
    console.log("  FEHLT: " + missing.join(", ")); failure += missing.length;
  }
  if (surplus.length) {
    console.log("  NEU (nicht in der Vorgabe): " + surplus.join(", "));
  }

  // 2. Data identifier, target address and interval.
  console.log("\nDatenkennung, Adresse und Takt:");
  let headerError = 0;
  for (const row of table.vals) {
    const plan_value = OLD_ADDRESSES[row.name];
    if (!plan_value) continue;
    const address = table.addresses[row.address];
    const [did, sh, required, rarely] = plan_value;
    const deviation = [];
    if (row.did !== did) deviation.push(`did ${row.did} statt ${did}`);
    if (!address) deviation.push(`Adresse "${row.address}" unbekannt`);
    else if (address.sh !== sh) deviation.push(`sh ${address.sh} statt ${sh}`);
    if (!!row.required !== required) deviation.push("pflicht weicht ab");
    if ((row.rarely || 0) !== rarely) {
      deviation.push(`selten ${row.rarely || 0} statt ${rarely}`);
    }
    if (deviation.length) {
      console.log(`  FEHLER ${row.name}: ${deviation.join(", ")}`);
      headerError += 1;
    }
  }
  console.log(headerError ? `  ${headerError} Abweichung(en)` : "  alle gleich");
  failure += headerError;

  // 3. The conversions themselves.
  console.log("\nUmrechnungen gegen die Vorgabe:");
  const follow = probe_sequences();
  const readNew = {};
  for (const row of table.vals) {
    readNew[row.name] = f.joltObd.readFor(row.name);
    for (const w of row.also || []) {
      readNew[w.name] = f.joltObd.readFor(w.name);
    }
  }

  for (const name of oldNames) {
    const old = OLD[name];
    const fresh = readNew[name];
    if (!fresh) {
      console.log(`  FEHLER ${name}: keine Lesefunktion in der Tabelle`);
      failure += 1;
      continue;
    }
    let deviations = 0;
    let example = null;
    for (const bytes of follow) {
      const a = clean(old(bytes));
      const b = clean(fresh(bytes));
      if (!same(a, b)) {
        deviations += 1;
        if (!example) example = { bytes, a, b };
      }
    }
    if (deviations) {
      console.log(`  FEHLER ${name}: ${deviations} von ${follow.length} `
                  + `Folgen weichen ab`);
      console.log(`         Bytes [${example.bytes.join(", ")}]`);
      console.log(`         vorher ${example.a}, jetzt ${example.b}`);
      failure += 1;
    } else {
      console.log(`  ok     ${name} (${follow.length} Folgen gleich)`);
    }
  }

  console.log(failure === 0
    ? "\nDie Tabelle rechnet wie die Funktionen davor."
    : `\n${failure} Abweichung(en).`);
  process.exit(failure === 0 ? 0 : 1);
}

main();
