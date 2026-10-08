#!/usr/bin/env node
/* Prueft die Beobachtung im Dongle-Baustein - Protokoll, Zaehler, Konsole.
 *
 * Die Einstellungen-Ansicht zeigt, was `joltObd.diagnose()` und
 * `joltObd.protokoll()` liefern. Stimmen diese Zahlen nicht, fuehrt die
 * Diagnose in die Irre - und sie wird gerade dann gebraucht, wenn im Auto
 * etwas nicht stimmt, also genau dort, wo man sich auf sie verlassen muss.
 *
 * Gegen einen nachgebildeten Dongle (Web Bluetooth) laufen lassen:
 *   verbinden -> Handshake -> Satz lesen -> Zaehler und Protokoll ansehen.
 * Dazu der Gegenbeweis, dass die Beobachtung nichts veraendert: Was
 * gesendet wird, ist dieselbe Befehlsfolge wie ohne.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FRONTEND = path.join(__dirname, "..", "frontend");
let failure = 0;

function verify(planApply, text, extra) {
  if (planApply) {
    console.log("  ok    " + text);
  } else {
    console.log("  FEHLT " + text + (extra ? "   " + extra : ""));
    failure += 1;
  }
}

/* Ein ELM327, der auf bekannte Abfragen antwortet, auf eine gar nicht
 * (Zeitablauf) und auf den Rest mit NO DATA. */
function dongle(sent, options) {
  const listener = [];
  const responses = {
    "ATZ": "ELM327 v2.3", "ATRV": "12.6V",
    "22028C": "17FE007B 04 62028C B4",
  };
  const ch = {
    uuid: "fff1",
    properties: { write: true, writeWithoutResponse: true, notify: true },
    addEventListener(t, f) { listener.push(f); },
    removeEventListener() {},
    async startNotifications() {},
    async writeValueWithoutResponse(d) {
      const t = new TextDecoder().decode(d).trim();
      sent.push(t);
      if (options && options.muted && options.muted.includes(t)) return;
      const a = responses[t] !== undefined ? responses[t]
        : (t.startsWith("AT") ? "OK" : "NO DATA");
      setTimeout(() => {
        const text = a + "\r\r>";
        for (const part of [text.slice(0, 6), text.slice(6)]) {
          const val = new DataView(new TextEncoder().encode(part).buffer);
          for (const f of listener) f({ target: { value: val } });
        }
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

function environment(bluetooth) {
  const storage = {};
  const timeframe = {
    localStorage: {
      getItem: (k) => (k in storage ? storage[k] : null),
      setItem: (k, v) => { storage[k] = String(v); },
      removeItem: (k) => { delete storage[k]; },
    },
    navigator: { bluetooth },
    TextEncoder, TextDecoder, DataView, Uint8Array,
    setTimeout, clearTimeout, console,
  };
  timeframe.window = timeframe;
  const context = vm.createContext(timeframe);
  for (const file of ["obd-ble-native.js", "readings.js", "obd-core.js"]) {
    vm.runInContext(fs.readFileSync(path.join(FRONTEND, file), "utf8"),
                    context, { filename: file });
  }
  return timeframe;
}

async function main() {
  console.log("\nZaehler und Protokoll");
  const sent = [];
  const f = environment(dongle(sent));
  const O = f.joltObd;

  const earlier = O.diagnose();
  verify(earlier.linked === false && earlier.rounds.n === 0
         && earlier.transport === "web",
         "vor dem Verbinden: getrennt, keine Runden, Zugang 'web'");

  await O.link();
  await O.handshake();
  let d = O.diagnose();
  verify(d.linked && d.connection.device === "IOS-Vlink"
         && typeof d.connection.since === "number",
         "nach dem Verbinden: Geraet und Zeitpunkt stehen drin");
  verify(d.commands.sent === sent.length && d.commands.answered === sent.length
         && d.commands.timeout === 0,
         "jeder gesendete Befehl ist gezaehlt und beantwortet",
         `${d.commands.sent}/${d.commands.answered}/${sent.length}`);
  verify(d.commands.lastReception !== null && d.commands.latestMs !== null,
         "letzte Antwort und Antwortzeit sind vermerkt");

  const record = await O.readRecord(0);
  d = O.diagnose();
  verify(record.soc_raw === 180, "der Ladestand wird wie bisher gelesen (0xB4 = 180)");
  verify(d.rounds.n === 1 && d.rounds.failure === 0 && d.rounds.latestMs !== null,
         "eine Runde ist gezaehlt");
  verify(d.readings.soc_raw.ok === 1 && d.readings.soc_raw.val === 180,
         "der Ladestand zaehlt als ok, mit letztem Wert");
  verify(d.readings.current_a.empty === 1 && d.readings.current_a.ok === 0,
         "NO DATA zaehlt als 'leer' - das Steuergeraet antwortet, die Kennung passt nicht");
  verify(d.lastRecord && d.lastRecord.vals.soc_raw === 180,
         "der letzte Satz steht zur Anzeige bereit");

  // Selten gelesene Werte: in Runde 0 dran, in Runde 1 nicht.
  const earlierN = d.readings.battery_kwh ? (d.readings.battery_kwh.ok + d.readings.battery_kwh.empty + d.readings.battery_kwh.failure) : 0;
  await O.readRecord(1);
  d = O.diagnose();
  const afterN = d.readings.battery_kwh.ok + d.readings.battery_kwh.empty + d.readings.battery_kwh.failure;
  verify(afterN === earlierN, "was in der Runde nicht dran ist, wird nicht mitgezaehlt");

  console.log("\nProtokoll");
  const log = O.trace_log(false);
  verify(log.length > 10 && log.some((z) => z.variety === "raus" && z.text === "22028C")
         && log.some((z) => z.variety === "rein" && z.text.includes("62028C")),
         "gesendete und empfangene Zeilen stehen im Protokoll, mit Richtung");
  verify(log.every((z) => typeof z.timestamp === "number"), "jede Zeile traegt eine Zeit");
  verify(O.trace_log(true).every((z) => /NO DATA|FEHLER|Zeit|keine Antwort|verspätet|getrennt|fehlgeschlagen|ERROR|UNABLE|BUS/i.test(z.text))
         && O.trace_log(true).length > 0,
         "der Filter 'nur Auffaelliges' zeigt nur Auffaelliges");
  log[0].text = "veraendert";
  verify(O.trace_log(false)[0].text !== "veraendert",
         "das Protokoll nach aussen ist eine Kopie");

  // Ringpuffer
  const f2 = environment(dongle([]));
  await f2.joltObd.link();
  for (let i = 0; i < 400; i++) await f2.joltObd.command("ATRV");
  verify(f2.joltObd.trace_log(false).length <= 600,
         "das Protokoll waechst nicht unbegrenzt (Ringpuffer)",
         String(f2.joltObd.trace_log(false).length));

  console.log("\nKonsole");
  const priorCommand = sent.length;
  await O.cli("ATSH123");
  verify(sent[priorCommand] === "ATSH123",
         "ein von Hand gesendeter Befehl geht unveraendert hinaus (ohne Umformen ausser dem Trimmen)");
  verify(O.diagnose().latestAddress === null,
         "danach ist die gemerkte Adresse verworfen - die naechste Runde setzt sie neu");
  const pastConsole = sent.length;
  await O.readRecord(2);
  verify(sent.slice(pastConsole).some((t) => t.startsWith("ATSH")),
         "und setzt sie dann wirklich neu");

  console.log("\nAusfaelle");
  const gesendet3 = [];
  const f3 = environment(dongle(gesendet3, { muted: ["ATRV"] }));
  await f3.joltObd.link();
  let toss = null;
  try { await f3.joltObd.command("ATRV", 40); } catch (e) { toss = e; }
  const d3 = f3.joltObd.diagnose();
  verify(toss && /Zeit/.test(toss.message) && d3.commands.timeout === 1
         && d3.commands.answered === 0,
         "ein Befehl ohne Antwort ist als Zeitablauf gezaehlt, nicht als beantwortet");
  verify(f3.joltObd.trace_log(true).some((z) => /keine Antwort/.test(z.text)),
         "und steht als Auffaelliges im Protokoll");

  console.log("\nZuruecksetzen und Vergessen");
  O.resetCounter();
  d = O.diagnose();
  verify(d.rounds.n === 0 && d.commands.sent === 0
         && Object.keys(d.readings).length === 0,
         "Zaehler zuruecksetzen leert Runden, Befehle und Messwerte");
  verify(d.linked === true, "und trennt nichts");
  O.forget();
  verify(O.diagnose().rememberedDevice === null && O.diagnose().connection.device === "",
         "Vergessen loescht das gemerkte Geraet");
  verify(f.localStorage.getItem("jolt-ble-geraet") === null,
         "und die Merkkennung der nativen Bruecke");

  O.detach();
  d = O.diagnose();
  verify(d.linked === false && d.connection.since === null,
         "Trennen setzt 'verbunden seit' zurueck");

  console.log(failure ? `\n${failure} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
  process.exit(failure ? 1 : 0);
}

main().catch((e) => { console.log("ABBRUCH", e); process.exit(2); });
