#!/usr/bin/env node
/* Die Tabelle muss dieselben Zahlen liefern wie die Funktionen davor.
 *
 * Bis zu diesem Umbau stand jede Umrechnung als eigene Funktion in
 * obd-core.js. Jetzt steht sie als Zeile in readings.js, und ein
 * Interpreter setzt sie um. Genau bei so einer Umstellung gehen Vorzeichen,
 * Skalierung und Bytereihenfolge still daneben - `soc_roh` ist ein Byte
 * geteilt durch 2,5, der Batteriestrom `(Rohwert - 150000)/100` ueber vier
 * Bytes, und beide saehen auch falsch noch plausibel aus.
 *
 * Deshalb stehen die **alten Funktionen unveraendert hier drin** und
 * dienen als Vorgabe. Verglichen wird ueber zufaellige und gezielt
 * gewaehlte Bytefolgen, darunter die am Fahrzeug gemessenen. Weicht eine
 * Zeile ab, nennt die Ausgabe den Namen, die Bytes und beide Zahlen.
 *
 * Diese Datei ist bewusst redundant. Sie darf erst verschwinden, wenn die
 * Tabelle einmal am Auto bestaetigt ist.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FRONTEND = path.join(__dirname, "..", "frontend");

/* ---------- Die Vorgabe: der Stand vor dem Umbau ---------- */

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

/* Die Zieladresse und die Datenkennung gehoeren zur Umrechnung: Eine Zeile
 * mit richtiger Formel und falscher Adresse liefert gar nichts, und eine
 * mit falscher Datenkennung die Zahl eines anderen Parameters. */
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

/* ---------- Das Neue laden ---------- */

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

/* ---------- Bytefolgen zum Vergleichen ---------- */

/* Erst die Grenzfaelle, dann Zufall. Die Grenzfaelle sind die wichtigeren:
 * Dort entscheidet sich, ob eine zu kurze Antwort null ergibt statt einer
 * aus `undefined` gerechneten Zahl. */
function probe_sequences() {
  const follow = [];
  for (let n = 0; n <= 20; n += 1) {
    follow.push(new Array(n).fill(0));                    // alles null
    follow.push(new Array(n).fill(255));                  // alles gesetzt
    follow.push(Array.from({ length: n }, (_, i) => i));   // aufsteigend
  }
  // Der am Fahrzeug gemessene Entladezaehler: 0xF7141E0D auf b12..b15.
  const measured = new Array(16).fill(0);
  measured[12] = 0xF7; measured[13] = 0x14;
  measured[14] = 0x1E; measured[15] = 0x0D;
  follow.push(measured);
  // Der bestaetigte Ladestand: Rohwert 0xB4.
  follow.push([0xB4]);
  // Die Kompressor-Messung "an" aus dem Kommentar in readings.js.
  follow.push([0x51, 0x24, 0xC0, 0x24, 0xC0, 0x0A, 0x3A, 0x0E, 0, 0, 0]);

  let seed = 20260912;
  const cube = () => {
    // Ein fester, einfacher Generator: Der Lauf muss wiederholbar sein,
    // sonst ist ein roter Lauf morgen wieder gruen.
    seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF;
    return seed % 256;
  };
  for (let i = 0; i < 400; i += 1) {
    const n = 1 + (cube() % 20);
    follow.push(Array.from({ length: n }, cube));
  }
  return follow;
}

/* `auswerten` im Kern schiebt jeden Wert durch `sauber()`, das undefined
 * und NaN auf null abbildet. Die alten Funktionen gaben fuer eine leere
 * Antwort teils `undefined` zurueck (`b[0]`), die neue Formel gibt null -
 * hinter `sauber` ist das derselbe Wert. Verglichen wird deshalb danach. */
function clean(val) {
  return (val === null || val === undefined || Number.isNaN(val))
    ? null : val;
}

/* Fliesskomma: `roh / 1310.77 / 1000` und `roh / 1310770` sind mathematisch
 * dasselbe, in doppelter Genauigkeit aber nicht bitgleich. Der Unterschied
 * liegt bei rund 1e-16 relativ - fuer eine Kilowattstunde mit einer
 * Nachkommastelle bedeutungslos. Eine echte Verwechslung von Vorzeichen,
 * Teiler oder Bytelage liegt dagegen um Groessenordnungen daneben. */
function same(a, b) {
  if (a === null || b === null) return a === b;
  if (a === b) return true;
  const denominator = Math.max(Math.abs(a), Math.abs(b), 1e-12);
  return Math.abs(a - b) / denominator < 1e-9;
}

/* ---------- Lauf ---------- */

function main() {
  const f = newCharging();
  let failure = 0;

  if (f.joltObd.TABLE_ERROR.length) {
    console.log("Tabelle meldet Fehler:");
    for (const t of f.joltObd.TABLE_ERROR) console.log("  " + t);
    failure += f.joltObd.TABLE_ERROR.length;
  }

  // Die Lesefunktionen stecken im Modul; erreichbar sind sie ueber den
  // Umweg, den auch die Aufzeichnung geht. Deshalb wird hier direkt auf die
  // aufgeloeste Tabelle zugegriffen.
  const table = f.window.joltReadings;
  const fields = f.joltObd.FIELDS;

  // 1. Vollstaendigkeit: Kein Wert darf beim Umbau verlorengegangen sein.
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

  // 2. Datenkennung, Zieladresse und Takt.
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

  // 3. Die Umrechnungen selbst.
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
