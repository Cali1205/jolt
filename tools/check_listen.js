#!/usr/bin/env node
/* Prueft das passive Mithoeren (obd-core.js: lauschen).
 *
 * Es soll an einem ladenden, verriegelten Auto beobachten, ohne die
 * Alarmanlage zu wecken. Was sich ohne Auto pruefen laesst, ist das, woran
 * der Alarm haengt - **was gesendet wird**:
 *   - bis ATMA nur AT-Befehle (bleiben im Dongle), danach nur noch ein
 *     Stopp-Zeichen und der Handshake (wieder nur AT-Befehle),
 *   - keine Abfrage wie 22028C oder 0100, auch nicht von einer Leserunde,
 *     die zufaellig dazwischenkommt,
 *   - ATCSM1 (stilles Mitlesen) und ATCRA (Filter weg) vor ATMA.
 * Dazu die Auswertung: Kennungen, Haeufigkeit, Veraenderlichkeit.
 *
 *     node tools/check_listen.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FRONTEND = path.join(__dirname, "..", "frontend");
let failure = 0;

function verify(ok, text, extra) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "   " + (extra || "")));
  if (!ok) failure += 1;
}

/* Ein ELM327, der nach ATMA Frames schickt, bis irgendein Zeichen kommt, dann
 * "STOPPED" und die Eingabeaufforderung. */
function dongle(sent, options) {
  const o = options || {};
  const listener = [];
  let monitor = null;
  const output = (text) => {
    // In Haeppchen von je sieben Zeichen: BLE liefert so, mitten durch Zeilen.
    for (let i = 0; i < text.length; i += 7) {
      const val = new DataView(new TextEncoder().encode(text.slice(i, i + 7)).buffer);
      for (const f of listener) f({ target: { value: val } });
    }
  };
  const ch = {
    uuid: "fff1",
    properties: { write: true, writeWithoutResponse: true, notify: true },
    addEventListener(t, f) { listener.push(f); },
    removeEventListener() {},
    async startNotifications() {},
    async writeValueWithoutResponse(d) {
      const raw = new TextDecoder().decode(d);
      const t = raw.trim();
      sent.push(raw === "\r" ? "<STOPP>" : t);
      if (monitor) {                     // jedes Zeichen beendet das Mithoeren
        clearInterval(monitor); monitor = null;
        setTimeout(() => output("STOPPED\r\r>"), 1);
        return;
      }
      if (t === "ATMA") {
        let i = 0;
        monitor = setInterval(() => {
          i += 1;
          const frames = o.still ? [] : [
            `7B0 06 62 02 8C B4 00 00\r`,
            `17FE007B 04 62 02 8C ${(i % 4 + 16).toString(16).toUpperCase()}\r`,
            ...(i % 2 ? ["7B0 06 62 02 8C B4 00 00\r"] : []),
          ];
          if (o.noise && i === 2) frames.push("BUFFER FULL\r");
          output(frames.join(""));
        }, 20);
        return;
      }
      const response = (o.unknown || []).includes(t) ? "?"
        : (t === "ATZ" ? "ELM327 v2.3" : (t.startsWith("AT") ? "OK" : "NO DATA"));
      setTimeout(() => output(response + "\r\r>"), 1);
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
    setTimeout, clearTimeout, setInterval, clearInterval, console,
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
  console.log("Was gesendet wird");
  let sent = [];
  let f = environment(dongle(sent));
  let O = f.joltObd;
  await O.link();
  await O.handshake();
  sent.length = 0;

  // Waehrend des Mithoerens versucht jemand zu lesen: Es darf nichts hinausgehen.
  let during = null;
  const cycle = O.listen({ trace_log: "6", duration_ms: 600 });
  await new Promise((w) => setTimeout(w, 250));
  during = {
    listens: O.listens(),
    cli: await O.cli("ATRV").then(() => "gesendet", (e) => e.message),
    record: await O.readRecord(0).then(() => "gelesen", (e) => e.message),
    voltage: await O.voltage(),
  };
  const e = await cycle;

  const pastAtma = sent.slice(sent.indexOf("ATMA") + 1);
  const priorAtma = sent.slice(0, sent.indexOf("ATMA"));
  verify(sent.includes("ATMA"), "ATMA wird gesendet");
  verify(priorAtma.every((b) => /^AT/.test(b)),
         "alles vor ATMA sind AT-Befehle (bleiben im Dongle)", priorAtma.join(" "));
  verify(pastAtma.every((b) => b === "<STOPP>" || /^AT/.test(b)),
         "danach nur noch das Stopp-Zeichen und AT-Befehle (der Handshake)",
         pastAtma.join(" "));
  verify(!sent.some((b) => /^[0-9A-F]{4,}$/.test(b)),
         "an keiner Stelle eine Abfrage (22028C, 0100 ...) - nichts, was ein "
         + "Steuergeraet weckt", sent.join(" "));
  const csm = sent.indexOf("ATCSM1"), cra = sent.indexOf("ATCRA"), ma = sent.indexOf("ATMA");
  verify(csm > -1 && cra > -1 && csm < ma && cra < ma,
         "ATCSM1 (stilles Mitlesen) und ATCRA (Filter weg) kommen vor ATMA");
  verify(sent.indexOf("ATSP6") > -1 && sent.indexOf("ATSP6") < ma,
         "das gewaehlte Protokoll steht vor ATMA");
  verify(sent.indexOf("ATS1") > -1 && sent.indexOf("ATCAF0") > -1,
         "Leerzeichen an, Formatierung aus: rohe Frames, lesbar zerlegt");

  console.log("\nWaehrenddessen");
  verify(during.listens === true, "lauscht() meldet es - die Oberflaeche liest dann nicht");
  verify(/lauscht/.test(during.cli), "ein Befehl aus der Konsole wird abgewiesen",
         during.cli);
  verify(/lauscht/.test(during.record), "eine Leserunde wird abgewiesen", during.record);
  verify(during.voltage === null, "die Spannungspruefung auch - nichts geht hinaus");
  verify(!sent.includes("ATRV") && !sent.some((b) => b === "22028C"),
         "und es ist auch nichts davon beim Dongle angekommen");

  console.log("\nAuswertung");
  verify(e.total > 6 && e.ids.length === 2, "Frames gezaehlt, zwei Kennungen",
         JSON.stringify({ g: e.total, ids: e.ids.map((i) => i.id) }));
  const a = e.ids.find((i) => i.id === "7B0"), b = e.ids.find((i) => i.id === "17FE007B");
  verify(a && a.variants === 1 && a.tail === "06 62 02 8C B4 00 00",
         "eine Kennung mit immer gleichem Inhalt: eine Variante", JSON.stringify(a));
  verify(b && b.variants >= 3 && b.variants <= 4,
         "eine mit wechselndem Inhalt: mehrere Varianten (so sieht ein Messwert aus)",
         JSON.stringify(b));
  verify(a.n > b.n && e.ids[0].id === "7B0",
         "sortiert nach Haeufigkeit", e.ids.map((i) => i.id + ":" + i.n).join(" "));
  verify(a.perSec > 5 && a.perSec < 100, "mit Rate je Sekunde", String(a.perSec));
  verify(e.probes.length > 0 && e.probes.length <= 30, "und ein paar Rohzeilen als Probe");

  console.log("\nDanach");
  verify(O.listens() === false, "lauscht() ist wieder aus");
  verify(sent.indexOf("ATZ", ma) > -1 && sent.includes("ATSP7") &&
         sent.lastIndexOf("ATSP7") > ma && sent.lastIndexOf("ATSH" + "FC007B") > ma,
         "der Handshake stellt Protokoll, Filter und Adresse wieder her");
  sent.length = 0;
  const response = await O.cli("ATRV").then(() => "ok", (x) => x.message);
  verify(response === "ok", "danach geht die Konsole wieder", response);

  console.log("\nStille und Stoerungen");
  sent = [];
  f = environment(dongle(sent, { still: true }));
  O = f.joltObd;
  await O.link();
  const still = await O.listen({ duration_ms: 500 });
  verify(still.total === 0 && still.ids.length === 0,
         "ein stiller Bus ergibt: nichts angekommen - ohne Fehler");

  sent = [];
  f = environment(dongle(sent, { noise: true, unknown: ["ATCSM1"] }));
  O = f.joltObd;
  await O.link();
  const laut = await O.listen({ trace_log: "7", duration_ms: 500 });
  verify(laut.hints.some((h) => /BUFFER FULL/.test(h)),
         "BUFFER FULL erscheint als Hinweis und nicht als Frame", JSON.stringify(laut.hints));
  verify(laut.hints.some((h) => /ATCSM1/.test(h)),
         "ein Dongle, der ATCSM1 nicht kennt, wird gemeldet - das Ergebnis ist dann "
         + "mit Vorsicht zu lesen", JSON.stringify(laut.hints));
  verify(sent.includes("ATSP7"), "Protokoll 7 laesst sich waehlen");

  console.log("\nOhne Verbindung");
  f = environment(dongle([]));
  const without = await f.joltObd.listen({}).then(() => "lief", (x) => x.message);
  verify(/nicht verbunden/.test(without), "ohne Dongle: Fehler statt Absturz", without);

  console.log(failure ? `\n${failure} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
  process.exit(failure ? 1 : 0);
}

main().catch((x) => { console.log("Abbruch:", x); process.exit(1); });
