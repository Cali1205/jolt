#!/usr/bin/env node
/* Prueft die native Bluetooth-Bruecke ohne iPhone.
 *
 * frontend/obd-ble-nativ.js bildet fuer obd-kern.js die Gestalt von Web
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
let fehler = 0;

function pruefe(sollGelten, text) {
  if (sollGelten) {
    console.log("  ok    " + text);
  } else {
    console.log("  FEHLT " + text);
    fehler += 1;
  }
}

/* Eine frische Browserumgebung je Fall.
 *
 * Die beiden Dateien sind Sofortfunktionen, die sich an `window` haengen;
 * zweimal in denselben Kontext geladen, teilten sich die Faelle ihren
 * Zustand - besonders die gemerkte Geraetekennung und die Verbindung. */
function umgebung(zusatz) {
  const speicher = {};
  const fenster = {
    localStorage: {
      getItem: (k) => (k in speicher ? speicher[k] : null),
      setItem: (k, v) => { speicher[k] = String(v); },
    },
    navigator: {},
    TextEncoder, TextDecoder, DataView, Uint8Array,
    setTimeout, clearTimeout, console,
  };
  Object.assign(fenster, zusatz || {});
  fenster.window = fenster;
  const kontext = vm.createContext(fenster);
  for (const datei of ["obd-ble-nativ.js", "obd-kern.js"]) {
    vm.runInContext(fs.readFileSync(path.join(FRONTEND, datei), "utf8"),
                    kontext, { filename: datei });
  }
  return fenster;
}

/* ---------- Ein Dongle, der sich wie einer benimmt ---------- */

/* Antwortet auf jeden Befehl mit "OK>" - ausser auf ATZ, da meldet sich ein
 * ELM327 mit seiner Fassung. Wichtiger als der Inhalt ist die Gestalt: Die
 * Antwort kommt in zwei Haeppchen, so wie BLE sie liefert, und erst das
 * '>' schliesst sie ab. Genau daran haengt die Rahmenlogik im Kern. */
function falschesPlugin(protokoll) {
  const DIENST = "0000fff0-0000-1000-8000-00805f9b34fb";
  const CHAR = "0000fff1-0000-1000-8000-00805f9b34fb";
  let melden = null;

  return {
    Capacitor: { isNativePlatform: () => true, getPlatform: () => "ios" },
    KeepAwake: { keepAwake: async () => {}, allowSleep: async () => {} },
    BleClient: {
      async initialize() { protokoll.push("initialize"); },
      async requestDevice() {
        protokoll.push("requestDevice");
        return { deviceId: "AA-BB-CC", name: "IOS-Vlink" };
      },
      async getDevices(kennungen) {
        protokoll.push("getDevices:" + kennungen.join(","));
        return kennungen.map((k) => ({ deviceId: k, name: "IOS-Vlink" }));
      },
      async connect(kennung) { protokoll.push("connect:" + kennung); },
      async disconnect() { protokoll.push("disconnect"); },
      async getServices() {
        return [{
          uuid: DIENST,
          characteristics: [{
            uuid: CHAR,
            properties: { write: true, writeWithoutResponse: true,
                          notify: true },
          }],
        }];
      },
      async startNotifications(kennung, dienst, charakteristik, rueckruf) {
        protokoll.push("startNotifications");
        melden = rueckruf;
      },
      async write(kennung, dienst, charakteristik, wert) {
        const text = new TextDecoder().decode(wert);
        protokoll.push("write:" + JSON.stringify(text));
        const antwort = text.startsWith("ATZ") ? "ELM327 v2.3" : "OK";
        // In zwei Haeppchen, wie BLE sie liefert.
        setTimeout(() => {
          melden(alsDataView(antwort.slice(0, 3)));
          melden(alsDataView(antwort.slice(3) + "\r>"));
        }, 0);
      },
      async writeWithoutResponse(k, d, c, wert) {
        return this.write(k, d, c, wert);
      },
    },
  };
}

function alsDataView(text) {
  const feld = new TextEncoder().encode(text);
  return new DataView(feld.buffer, feld.byteOffset, feld.byteLength);
}

/* ---------- Die Faelle ---------- */

async function nativerWeg() {
  console.log("Nativ: der Weg einer Runde am Auto");
  const protokoll = [];
  const f = umgebung({ joltBlePlugin: falschesPlugin(protokoll) });

  pruefe(f.joltBleNativ.verfuegbar(), "die Bruecke meldet sich als verfuegbar");
  pruefe(f.joltObd.verfuegbar(), "der Kern sieht Bluetooth");

  f.joltObd.einrichten(() => {}, null);
  const verbunden = await f.joltObd.anschliessen();
  pruefe(verbunden === true, "anschliessen() meldet Erfolg");
  pruefe(f.joltObd.verbunden(), "verbunden() sagt ja");
  pruefe(protokoll.includes("requestDevice"), "der Auswahldialog kam");
  pruefe(protokoll.includes("connect:AA-BB-CC"), "verbunden mit dem Geraet");
  pruefe(protokoll.includes("startNotifications"),
         "Benachrichtigungen abonniert");

  const antwort = await f.joltObd.befehl("ATZ");
  pruefe(protokoll.includes('write:"ATZ\\r"'),
         "ATZ ging mit Wagenruecklauf hinaus");
  pruefe(antwort === "ELM327 v2.3",
         `die zweigeteilte Antwort wurde zusammengesetzt (kam: ${JSON.stringify(antwort)})`);

  // Die Kennung muss den Neustart ueberdauern, sonst kommt bei jeder Fahrt
  // der Auswahldialog.
  pruefe(f.joltBleNativ.kennung() === "AA-BB-CC",
         "die Geraetekennung ist gemerkt");

  const zweite = umgebung({ joltBlePlugin: falschesPlugin(protokoll) });
  // Frische Umgebung, aber derselbe Speicher existiert dort nicht - deshalb
  // wird hier nur geprueft, dass getDevices ohne Kennung leer bleibt statt
  // zu scheitern.
  const ohne = await zweite.joltBleNativ.bluetooth.getDevices();
  pruefe(Array.isArray(ohne) && ohne.length === 0,
         "ohne gemerkte Kennung liefert getDevices eine leere Liste");
}

function rueckfallImBrowser() {
  console.log("Browser: der Rueckfall auf echtes Web Bluetooth");

  const echtes = { requestDevice: () => {}, getDevices: () => {} };
  const mit = umgebung({ navigator: { bluetooth: echtes } });
  pruefe(mit.joltBleNativ.verfuegbar() === false,
         "ohne Capacitor meldet sich die Bruecke als nicht verfuegbar");
  pruefe(mit.joltObd.verfuegbar() === true,
         "der Kern nimmt trotzdem Bluetooth an - naemlich das echte");

  const ohne = umgebung({ navigator: {} });
  pruefe(ohne.joltObd.verfuegbar() === false,
         "ohne beides meldet der Kern ehrlich 'kein Bluetooth'");
}

async function main() {
  await nativerWeg();
  rueckfallImBrowser();
  console.log(fehler === 0
    ? "\nAlles in Ordnung."
    : `\n${fehler} Pruefung(en) fehlgeschlagen.`);
  process.exit(fehler === 0 ? 0 : 1);
}

main().catch((f) => { console.error(f); process.exit(1); });
