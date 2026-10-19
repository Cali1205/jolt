#!/usr/bin/env node
/* Checks the platform profiles (frontend/platforms/*.js).
 *
 * Three things have to hold:
 *
 *  1. The MEB profile reproduces the table as it was before the platforms
 *     existed (tools/fixtures/meb_platform_baseline.json: every row with
 *     address, order, interval, required flag and the decoded value for
 *     single- and multi-frame responses). The move into a profile must not
 *     have changed a single byte position.
 *  2. Every registered platform resolves without errors, has exactly one
 *     required row and that is `soc_raw`, and no name appears twice.
 *  3. The OBD2 profile decodes standard answers, and an unknown platform id
 *     falls back to MEB.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FRONTEND = path.join(__dirname, "..", "frontend");
const BASELINE = path.join(__dirname, "fixtures", "meb_platform_baseline.json");

function load(bluetooth) {
  const storage = {};
  const timeframe = {
    console, TextEncoder, TextDecoder, DataView, Uint8Array,
    setTimeout, clearTimeout, navigator: { bluetooth },
    localStorage: {
      getItem: (k) => (k in storage ? storage[k] : null),
      setItem: (k, v) => { storage[k] = String(v); },
    },
  };
  timeframe.window = timeframe;
  const context = vm.createContext(timeframe);
  const platforms = fs.readdirSync(path.join(FRONTEND, "platforms"))
    .filter((f) => f.endsWith(".js")).sort();
  const files = platforms.map((f) => path.join("platforms", f))
    .concat(["obd-ble-native.js", "obd-decode.js", "obd-core.js"]);
  for (const file of files) {
    vm.runInContext(fs.readFileSync(path.join(FRONTEND, file), "utf8"),
                    context, { filename: file });
  }
  timeframe.storage = storage;
  return timeframe;
}

let failure = 0;
function check(ok, text) {
  if (!ok) { console.log("  FEHLER " + text); failure += 1; }
  return ok;
}
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const f = load();
const Decode = f.joltObdDecode;
const O = f.joltObd;
const registry = f.joltPlatforms;

console.log("MEB gegen die Vorgabe:");
const base = JSON.parse(fs.readFileSync(BASELINE, "utf8"));
const meb = registry.meb;
check(meb && meb.id === "meb", "Profil meb fehlt");
check(same(meb.addresses, base.addresses), "Adressen weichen ab");
check(meb.vals.length === base.rows.length,
      `${meb.vals.length} Zeilen statt ${base.rows.length}`);
meb.vals.forEach((row, order) => {
  const ref = base.rows[order];
  if (!ref) return;
  check(row.name === ref.name && ref.order === order,
        `Reihenfolge: Platz ${order} ist ${row.name}, vorher ${ref.name}`);
  check(row.did === ref.did, `${row.name}: did ${row.did} statt ${ref.did}`);
  check(row.address === ref.address, `${row.name}: Adresse ${row.address}`);
  check((row.rarely || 0) === ref.rarely, `${row.name}: selten abweichend`);
  check(!!row.required === ref.required, `${row.name}: pflicht abweichend`);
  check(same(JSON.parse(JSON.stringify(row)), ref.spec),
        `${row.name}: Zeile weicht ab`);
  const extra = row.also || [];
  const further = extra.length
    ? Object.fromEntries(extra.map((e) => [e.name, Decode.formula(e)])) : null;
  const entry = { did: row.did, load: Decode.formula(row), further };
  ref.decoded.forEach((d, i) => {
    check(same(Decode.evaluate(d.response, entry), d.value),
          `${row.name}: Antwort ${i} dekodiert anders`);
  });
});
console.log(failure ? "  Abweichungen" : `  ${base.rows.length} Zeilen identisch`);

console.log("\nJedes Profil:");
for (const [id, profile] of Object.entries(registry)) {
  check(profile.id === id, `${id}: id im Profil ist ${profile.id}`);
  check(typeof profile.title === "string" && profile.title, `${id}: title fehlt`);
  check(Array.isArray(profile.handshake) && profile.handshake.length > 0,
        `${id}: handshake fehlt`);
  check(typeof profile.protocol === "string", `${id}: protocol fehlt`);
  check(profile.soc && typeof profile.soc.bms === "function", `${id}: soc.bms fehlt`);
  O.usePlatform(id);
  check(O.platform().id === id, `${id}: usePlatform wählt nicht`);
  check(O.TABLE_ERROR.length === 0, `${id}: ${O.TABLE_ERROR.join(" | ")}`);
  const names = O.FIELDS.map((x) => x.name);
  check(new Set(names).size === names.length, `${id}: doppelte Namen`);
  const required = O.FIELDS.filter((x) => x.required).map((x) => x.name);
  check(same(required, ["soc_raw"]), `${id}: Pflicht ist [${required}]`);
  const soc = O.socFromRaw(100);
  check(soc && typeof soc.bms === "number" && typeof soc.hmi === "number",
        `${id}: socFromRaw liefert kein {raw,bms,hmi}`);
  console.log(`  ${id}: ${names.length} Werte`);
}
check(O.socFromRaw("x") === null && O.socFromRaw(null) === null,
      "socFromRaw ist nicht null-sicher");

console.log("\nOBD2:");
const obd2 = registry.obd2;
function decodeObd2(name, response) {
  const row = obd2.vals.find((v) => v.name === name);
  return Decode.evaluate(response, { did: row.did, load: Decode.formula(row) });
}
const raw = decodeObd2("soc_raw", "7E8 03 41 5B 80");
check(raw === 128, `soc_raw ${raw}`);
check(Math.abs(obd2.soc.bms(raw) - 50.196) < 0.01, "bms nicht ca. 50,2");
check(decodeObd2("speed_kmh", "7E8 03 41 0D 32") === 50, "speed_kmh");
check(decodeObd2("odometer_km", "7E8 06 41 A6 00 00 30 39") === 1234.5, "odometer_km");
check(decodeObd2("outside_temp_c", "7E8 03 41 46 5A") === 50, "outside_temp_c");
check(decodeObd2("aux12_v", "7E8 04 41 42 38 A4") === 14.5, "aux12_v");
check(Decode.payload_bytes("7E8 03 41 5B 80", "015B").length === 1,
      "payload_bytes: Quittung 415B");
check(Decode.payload_bytes("17FE007B 04 62 02 8C B4", "22028C")[0] === 0xB4,
      "payload_bytes: Quittung 62028C");
console.log(failure ? "  Abweichungen" : "  Standardantworten dekodiert");

console.log("\nStellantis e-CMP:");
const ecmp = registry.ecmp;
function decodeEcmp(name, response) {
  const row = ecmp.vals.find((v) => v.name === name);
  return Decode.evaluate(response, { did: row.did, load: Decode.formula(row) });
}
const ecmpRaw = decodeEcmp("soc_raw", "694 05 62 D4 10 A0 00");
check(ecmpRaw === 40960 && ecmp.soc.bms(ecmpRaw) === 80, `e-CMP soc_raw ${ecmpRaw}`);
check(decodeEcmp("voltage_v", "694 05 62 D8 15 15 E0") === 350, "e-CMP voltage_v");
check(decodeEcmp("current_a", "694 07 62 D8 16 00 01 38 80") === 50, "e-CMP current_a entladen");
check(decodeEcmp("current_a", "694 07 62 D8 16 00 01 2C 00") === 0, "e-CMP current_a null");
check(decodeEcmp("odometer_km", "694 07 62 D4 9C 00 BC 61 4E") === 12345.678, "e-CMP odometer_km");
check(decodeEcmp("speed_kmh", "682 05 62 D4 02 01 F4") === 50, "e-CMP speed_kmh");
check(decodeEcmp("outside_temp_c", "682 04 62 D4 34 F6") === -10, "e-CMP outside_temp_c vorzeichenbehaftet");
check(decodeEcmp("batterie_c", "682 04 62 D8 EF 14") === 20, "e-CMP batterie_c");
check(decodeEcmp("cell_min_v", "694 05 62 D8 6F 0E 74") === 3.7, "e-CMP cell_min_v");
check(decodeEcmp("energy_content_kwh", "694 05 62 D8 65 0A 00") === 40, "e-CMP energy_content_kwh");
check(decodeEcmp("soh_pct", "694 06 62 D8 60 01 05 DC") === 93.75, "e-CMP soh_pct");
check(decodeEcmp("speed_kmh", "682 05 62 D4 02 0B B8") === null, "e-CMP 300 km/h verworfen");
console.log(failure ? "  Abweichungen" : "  Antworten dekodiert");

console.log("\nPlattformwahl:");
O.usePlatform("meb");
check(O.usePlatform("obd2") === true && O.platform().id === "obd2", "wechsel auf obd2");
check(f.storage["jolt-platform"] === "obd2", "Wahl wird nicht gemerkt");
check(O.usePlatform("obd2") === false, "gleiche Plattform meldet Wechsel");
check(O.usePlatform("gibt-es-nicht") === true && O.platform().id === "meb",
      "unbekannte Plattform fällt nicht auf meb zurück");
check(O.platforms().map((p) => p.id).sort().join() === Object.keys(registry).sort().join(),
      "platforms() listet nicht alle Profile");

/* An ELM327 that answers what a car of the platform would answer and says
 * OK to every AT command. `sent` collects what the app put on the wire. */
function dongle(sent, responses) {
  const listener = [];
  const ch = {
    uuid: "fff1",
    properties: { write: true, writeWithoutResponse: true, notify: true },
    addEventListener(t, fn) { listener.push(fn); },
    removeEventListener() {},
    async startNotifications() {},
    async writeValueWithoutResponse(d) {
      const t = new TextDecoder().decode(d).trim();
      sent.push(t);
      const a = responses[t] !== undefined ? responses[t]
        : (t.startsWith("AT") ? "OK" : "NO DATA");
      setTimeout(() => {
        const val = new DataView(new TextEncoder().encode(a + "\r\r>").buffer);
        for (const fn of listener) fn({ target: { value: val } });
      }, 1);
    },
  };
  const service = { uuid: "fff0", async getCharacteristics() { return [ch]; } };
  const device = {
    name: "IOS-Vlink", addEventListener() {},
    gatt: { connected: true,
      async connect() { return { async getPrimaryServices() { return [service]; } }; },
      disconnect() { this.connected = false; } },
  };
  return { requestDevice: async () => device, getDevices: async () => [device] };
}

async function main() {
  console.log("\nDongle gegen simulierten Adapter:");
  const sent = [];
  const car = {
    "015B": "7E8 03 41 5B 80", "010D": "7E8 03 41 0D 32",
    "0146": "7E8 03 41 46 5A", "01A6": "7E8 06 41 A6 00 00 30 39",
    "0142": "7E8 04 41 42 38 A4",
    "22028C": "17FE007B 04 62028C B4",
    "22D410": "694 05 62 D4 10 A0 00", "22D815": "694 05 62 D8 15 15 E0",
    "22D816": "694 07 62 D8 16 00 01 38 80", "22D49C": "694 07 62 D4 9C 00 BC 61 4E",
    "22D402": "682 05 62 D4 02 01 F4", "22D434": "682 04 62 D4 34 F6",
  };
  const g = load(dongle(sent, car));
  const D = g.joltObd;
  await D.link();

  D.usePlatform("obd2");
  sent.length = 0;
  await D.handshake();
  check(same(sent, registry.obd2.handshake), `obd2: Handshake ${sent.join(",")}`);
  sent.length = 0;
  const soc = await D.readSoc();
  check(same(sent, ["ATSH7DF", "ATCRA", "015B"]),
        `obd2: readSoc sendet ${sent.join(",")} (kein ATCP, ATCRA leer)`);
  check(soc && soc.raw === 128 && Math.abs(soc.bms - 50.196) < 0.01 && soc.hmi === soc.bms,
        `obd2: readSoc liefert ${JSON.stringify(soc)}`);
  const record = await D.readRecord(0);
  check(record.soc_raw === 128 && record.speed_kmh === 50 && record.odometer_km === 1234.5
        && record.outside_temp_c === 50 && record.aux12_v === 14.5,
        `obd2: Satz ${JSON.stringify(record)}`);

  D.usePlatform("ecmp");
  sent.length = 0;
  await D.handshake();
  check(same(sent, registry.ecmp.handshake), `ecmp: Handshake ${sent.join(",")}`);
  sent.length = 0;
  const ecmpSoc = await D.readSoc();
  check(same(sent, ["ATSH6B4", "ATFCSH6B4", "ATFCSD300000", "ATFCSM1",
                    "ATCRA694", "22D410"]),
        `ecmp: readSoc sendet ${sent.join(",")} (11 Bit, kein ATCP)`);
  check(ecmpSoc && ecmpSoc.bms === 80 && ecmpSoc.hmi === 80,
        `ecmp: readSoc liefert ${JSON.stringify(ecmpSoc)}`);
  const ecmpRecord = await D.readRecord(0);
  check(ecmpRecord.soc_raw === 40960 && ecmpRecord.current_a === 50
        && ecmpRecord.speed_kmh === 50 && ecmpRecord.outside_temp_c === -10,
        `ecmp: Satz ${JSON.stringify(ecmpRecord)}`);

  D.usePlatform("meb");
  sent.length = 0;
  await D.handshake();
  check(same(sent, registry.meb.handshake), `meb: Handshake ${sent.join(",")}`);
  sent.length = 0;
  const mebSoc = await D.readSoc();
  check(same(sent, ["ATCP17", "ATSHFC007B", "ATFCSH17FC007B", "ATFCSD300000",
                    "ATFCSM1", "ATCRA17FE007B", "22028C"]),
        `meb: readSoc sendet ${sent.join(",")}`);
  check(mebSoc && mebSoc.raw === 180 && mebSoc.bms === 72
        && Math.abs(mebSoc.hmi - 73.4) < 0.05,
        `meb: 0xB4 gibt 72,0 % brutto und 73,4 % Anzeige: ${JSON.stringify(mebSoc)}`);
}

main().then(() => {
  if (failure) {
    console.log(`\n${failure} Fehler.`);
    process.exit(1);
  }
  console.log("\nAlles in Ordnung.");
});
