#!/usr/bin/env node
/* Prueft die native Bluetooth-Bruecke ohne iPhone.
 *
 * frontend/obd-ble-native.js bildet fuer obd-core.js die Gestalt von Web
 * Bluetooth nach, beantwortet sie aber ueber das Capacitor-Plugin. Diese
 * Datei laesst sich nur am Auto vollstaendig pruefen - aber genau das ist
 * der Grund fuer diese Pruefung: Alles, was sich *vorher* feststellen
 * laesst, soll auch vorher auffallen und nicht erst an der Ladesaeule.
 *
 * Geprueft wird der Weg, den eine Runde am Auto nimmt:
 *   anschliessen -> Dienste suchen -> Charakteristik waehlen ->
 *   Benachrichtigungen abonnieren -> Befehl schreiben -> Antwort einsammeln
 *
 * Dazu kommt der Rueckfall: Ohne Capacitor muss der Kern unveraendert das
 * echte navigator.bluetooth nehmen, sonst waere die Weboberflaeche in
 * Bluefy kaputt - und die ist bis auf Weiteres der Alltagsweg.
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

/* Eine frische Browserumgebung je Fall.
 *
 * Die beiden Dateien sind Sofortfunktionen, die sich an `window` haengen;
 * zweimal in denselben Kontext geladen, teilten sich die Faelle ihren
 * Zustand - besonders die gemerkte Geraetekennung und die Verbindung. */
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

/* ---------- Ein Dongle, der sich wie einer benimmt ---------- */

/* Antwortet auf jeden Befehl mit "OK>" - ausser auf ATZ, da meldet sich ein
 * ELM327 mit seiner Fassung. Wichtiger als der Inhalt ist die Gestalt: Die
 * Antwort kommt in zwei Haeppchen, so wie BLE sie liefert, und erst das
 * '>' schliesst sie ab. Genau daran haengt die Rahmenlogik im Kern. */
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
        // In zwei Haeppchen, wie BLE sie liefert.
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

/* ---------- Die Faelle ---------- */

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

  // ATRV misst der ELM-Chip selbst und fasst den CAN-Bus nicht an. Darauf
  // haengt, ob jolt am abgeschlossenen Auto die Spannung lesen darf, ohne die
  // Alarmanlage zu wecken - es darf also genau dieser eine Befehl hinausgehen.
  const earlier = trace_log.length;
  const volt = await f.joltObd.voltage();
  const sent = trace_log.slice(earlier).filter((z) => z.startsWith("write:"));
  verify(volt === 12.6, `spannung() liest 12,6 V aus "12.6V" (kam: ${volt})`);
  verify(sent.length === 1 && sent[0] === 'write:"ATRV\\r"',
         "und es geht nur ATRV hinaus - nichts, was den Bus weckt",
         JSON.stringify(sent));

  // Die Kennung muss den Neustart ueberdauern, sonst kommt bei jeder Fahrt
  // der Auswahldialog.
  verify(f.joltBleNative.ident() === "AA-BB-CC",
         "die Geraetekennung ist gemerkt");

  const second = environment({ joltBlePlugin: wrongPlugin(trace_log) });
  // Frische Umgebung, aber derselbe Speicher existiert dort nicht - deshalb
  // wird hier nur geprueft, dass getDevices ohne Kennung leer bleibt statt
  // zu scheitern.
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
