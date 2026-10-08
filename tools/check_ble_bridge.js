#!/usr/bin/env node
/* Checks the native Bluetooth bridge without an iPhone.
 *
 * frontend/obd-ble-native.js mimics the shape of Web Bluetooth for obd-core.js,
 * but answers it through the Capacitor plugin. This file can only be fully
 * verified in the car - but that is exactly the reason for this check:
 * everything that can be established *beforehand* should also show up
 * beforehand, not only at the charging station.
 *
 * What is checked is the path one round in the car takes:
 *   connect -> find services -> pick characteristic ->
 *   subscribe to notifications -> write command -> collect response
 *
 * On top of that comes the fallback: without Capacitor the core must use the
 * real navigator.bluetooth unchanged, otherwise the web UI would be broken in
 * Bluefy - and that is the everyday route until further notice.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FRONTEND = path.join(__dirname, "..", "frontend");
let failure = 0;

function verify(planApply, text) {
  if (planApply) {
    console.log("  ok    " + text);
  } else {
    console.log("  FEHLT " + text);
    failure += 1;
  }
}

/* A fresh browser environment per case.
 *
 * The two files are immediately invoked functions that attach themselves to
 * `window`; loaded twice into the same context, the cases would share their
 * state - especially the remembered device ID and the connection. */
function environment(extra) {
  const storage = {};
  const timeframe = {
    localStorage: {
      getItem: (k) => (k in storage ? storage[k] : null),
      setItem: (k, v) => { storage[k] = String(v); },
    },
    navigator: {},
    TextEncoder, TextDecoder, DataView, Uint8Array,
    setTimeout, clearTimeout, console,
  };
  Object.assign(timeframe, extra || {});
  timeframe.window = timeframe;
  const context = vm.createContext(timeframe);
  for (const file of ["obd-ble-native.js", "obd-core.js"]) {
    vm.runInContext(fs.readFileSync(path.join(FRONTEND, file), "utf8"),
                    context, { filename: file });
  }
  return timeframe;
}

/* ---------- A dongle that behaves like one ---------- */

/* Answers every command with "OK>" - except ATZ, where an ELM327 reports its
 * version. More important than the content is the shape: the response
 * arrives in two chunks, just as BLE delivers it, and only the '>'
 * terminates it. The framing logic in the core depends on exactly that. */
function wrongPlugin(trace_log) {
  const SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb";
  const CHAR = "0000fff1-0000-1000-8000-00805f9b34fb";
  let report = null;

  return {
    Capacitor: { isNativePlatform: () => true, getPlatform: () => "ios" },
    KeepAwake: { keepAwake: async () => {}, allowSleep: async () => {} },
    BleClient: {
      async initialize() { trace_log.push("initialize"); },
      async requestDevice() {
        trace_log.push("requestDevice");
        return { deviceId: "AA-BB-CC", name: "IOS-Vlink" };
      },
      async getDevices(ids) {
        trace_log.push("getDevices:" + ids.join(","));
        return ids.map((k) => ({ deviceId: k, name: "IOS-Vlink" }));
      },
      async connect(ident) { trace_log.push("connect:" + ident); },
      async disconnect() { trace_log.push("disconnect"); },
      async getServices() {
        return [{
          uuid: SERVICE,
          characteristics: [{
            uuid: CHAR,
            properties: { write: true, writeWithoutResponse: true,
                          notify: true },
          }],
        }];
      },
      async startNotifications(ident, service, charakteristik, callback) {
        trace_log.push("startNotifications");
        report = callback;
      },
      async write(ident, service, charakteristik, val) {
        const text = new TextDecoder().decode(val);
        trace_log.push("write:" + JSON.stringify(text));
        const response = text.startsWith("ATZ") ? "ELM327 v2.3"
          : text.startsWith("ATRV") ? "12.6V" : "OK";
        // In two chunks, as BLE delivers it.
        setTimeout(() => {
          report(asDataView(response.slice(0, 3)));
          report(asDataView(response.slice(3) + "\r>"));
        }, 0);
      },
      async writeWithoutResponse(k, d, c, val) {
        return this.write(k, d, c, val);
      },
    },
  };
}

function asDataView(text) {
  const field = new TextEncoder().encode(text);
  return new DataView(field.buffer, field.byteOffset, field.byteLength);
}

/* ---------- The cases ---------- */

async function nativePath() {
  console.log("Nativ: der Weg einer Runde am Auto");
  const trace_log = [];
  const f = environment({ joltBlePlugin: wrongPlugin(trace_log) });

  verify(f.joltBleNative.obtainable(), "die Bruecke meldet sich als verfuegbar");
  verify(f.joltObd.obtainable(), "der Kern sieht Bluetooth");

  f.joltObd.set_up(() => {}, null);
  const linked = await f.joltObd.attach();
  verify(linked === true, "anschliessen() meldet Erfolg");
  verify(f.joltObd.linked(), "verbunden() sagt ja");
  verify(trace_log.includes("requestDevice"), "der Auswahldialog kam");
  verify(trace_log.includes("connect:AA-BB-CC"), "verbunden mit dem Geraet");
  verify(trace_log.includes("startNotifications"),
         "Benachrichtigungen abonniert");

  const response = await f.joltObd.command("ATZ");
  verify(trace_log.includes('write:"ATZ\\r"'),
         "ATZ ging mit Wagenruecklauf hinaus");
  verify(response === "ELM327 v2.3",
         `die zweigeteilte Antwort wurde zusammengesetzt (kam: ${JSON.stringify(response)})`);

  // ATRV is measured by the ELM chip itself and does not touch the CAN bus. On
  // that depends whether jolt may read the voltage on a locked car without
  // waking the alarm system - so exactly this one command may go out.
  const earlier = trace_log.length;
  const volt = await f.joltObd.voltage();
  const sent = trace_log.slice(earlier).filter((z) => z.startsWith("write:"));
  verify(volt === 12.6, `spannung() liest 12,6 V aus "12.6V" (kam: ${volt})`);
  verify(sent.length === 1 && sent[0] === 'write:"ATRV\\r"',
         "und es geht nur ATRV hinaus - nichts, was den Bus weckt",
         JSON.stringify(sent));

  // The ID must survive a restart, otherwise the selection dialog appears
  // on every trip.
  verify(f.joltBleNative.ident() === "AA-BB-CC",
         "die Geraetekennung ist gemerkt");

  const second = environment({ joltBlePlugin: wrongPlugin(trace_log) });
  // Fresh environment, but the same storage does not exist there - so this
  // only checks that getDevices stays empty without an ID instead of
  // failing.
  const without = await second.joltBleNative.bluetooth.getDevices();
  verify(Array.isArray(without) && without.length === 0,
         "ohne gemerkte Kennung liefert getDevices eine leere Liste");
}

function fallbackInBrowser() {
  console.log("Browser: der Rueckfall auf echtes Web Bluetooth");

  const real = { requestDevice: () => {}, getDevices: () => {} };
  const using = environment({ navigator: { bluetooth: real } });
  verify(using.joltBleNative.obtainable() === false,
         "ohne Capacitor meldet sich die Bruecke als nicht verfuegbar");
  verify(using.joltObd.obtainable() === true,
         "der Kern nimmt trotzdem Bluetooth an - naemlich das echte");

  const without = environment({ navigator: {} });
  verify(without.joltObd.obtainable() === false,
         "ohne beides meldet der Kern ehrlich 'kein Bluetooth'");
}

async function main() {
  await nativePath();
  fallbackInBrowser();
  console.log(failure === 0
    ? "\nAlles in Ordnung."
    : `\n${failure} Pruefung(en) fehlgeschlagen.`);
  process.exit(failure === 0 ? 0 : 1);
}

main().catch((f) => { console.error(f); process.exit(1); });
