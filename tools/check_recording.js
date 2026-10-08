#!/usr/bin/env node
// Checks the start of a recording (frontend/trips.js) against a
// dongle that does not answer.
//
// The case at hand: the car is still asleep, the dongle is connected,
// the first query (start state of charge) runs into the timeout. It used to be that
// the dongle then counted as absent for the whole recording - on
// 5.10. a trip delivered only GPS for three minutes until someone restarted
// by hand.
//
//     node tools/check_recording.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "trips.js"), "utf8");

let failure = 0;
function verify(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) failure++;
}

/* A run with a dongle that behaves as `szenario` says. */
async function cycle(scenario) {
  const reports = [];
  const calls = { dongleUse: 0, link: 0, positionTrace: 0,
                    state: null, order: [] };
  const sent = [];
  const elemente = {
    "aufz-start": { disabled: false },
    "aufz-stand": { textContent: "" },
    "aufz-fahrzeug": { value: "1" },
    "aufz-name": { value: "Testfahrt" },
    "live-leer": { hidden: false },
    "live-inhalt": { hidden: true },
  };
  const empty = () => new Proxy(function () { return ""; }, {
    get: (z, n) => (n === Symbol.toPrimitive ? () => "" : n === "style" ? {} : empty()),
    apply: () => empty(), set: () => true });

  let linked = true;
  const obd = {
    obtainable: () => true,
    set_up() {},
    attach: async () => { linked = scenario.connectedPastConnect !== false; },
    linked: () => linked,
    handshake: async () => true,
    command: async () => {
      if (scenario.response) return scenario.response;
      if (scenario.connectionPath) linked = false;
      throw new Error("Zeitüberschreitung bei 22028C");
    },
    socFromResponse: () => ({ hmi: 71.7 }),
  };
  const K = {
    state: { sessionId: null, vehicles: [{ id: 1 }], tripsStale: false },
    report: (t) => reports.push(t),
    api: async (fs_path, opt) => { sent.push({ fs_path, body: opt && opt.body });
                                return { session_id: 9 }; },
    sessionRemember() {},
  };
  const timeframe = {
    jolt: K, joltObd: obd,
    joltApp: { showView() {} },
    joltLive: {
      link: () => { calls.link++; },
      positionTrace: () => { calls.positionTrace++; calls.order.push("position"); },
      drivingStateStart: (z) => { calls.state = z; calls.order.push("zustand"); },
      dongleUse: () => { calls.dongleUse++; },
      reconnectDongle: () => { calls.reconnect = (calls.reconnect || 0) + 1; },
      handshakeSafe: async () => obd.linked() && (await obd.handshake()),
    },
  };
  const context = {
    window: timeframe, console: { log() {} }, setTimeout, Promise, Date, JSON, Math, Number,
    document: { getElementById: (id) => elemente[id] || empty(),
                querySelector: () => empty(), createElement: () => empty(),
                addEventListener() {} },
    navigator: { geolocation: {
      getCurrentPosition: (ok) => ok({ coords: { latitude: 48.47, longitude: 9.14 } }) } },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  await timeframe.joltTrips.startRecording();
  return { calls, sent, reports, as_of: elemente["aufz-stand"].textContent,
           btn: elemente["aufz-start"] };
}

(async () => {
  console.log("\nDas Auto antwortet auf die erste Abfrage");
  let r = await cycle({ response: "62028C B5" });
  verify(r.calls.dongleUse === 1, "der Dongle wird benutzt");
  verify(r.sent.length === 1 && r.sent[0].body.soc === 71.7,
         "und der Startladestand geht mit", JSON.stringify(r.sent));
  verify(/Ladestand kommt aus dem Auto/.test(r.reports.join(" ")),
         "die Meldung sagt es", r.reports.join(" | "));

  console.log("\nDas Auto schläft noch - keine Antwort, Dongle verbunden");
  r = await cycle({});
  verify(r.calls.dongleUse === 1,
         "der Dongle bleibt in Benutzung - sonst läuft die ganze Aufzeichnung " +
         "ohne Fahrzeugwerte, wie am 5.10. in Sitzung 6", String(r.calls.dongleUse));
  verify(r.sent.length === 1 && r.sent[0].body.soc === null,
         "ohne Startladestand - der wird nicht erfunden",
         JSON.stringify(r.sent && r.sent[0] && r.sent[0].body));
  verify(/antwortet noch nicht/.test(r.reports.join(" ")),
         "und die Meldung sagt, dass es später kommt - statt \"Ladestand von Hand\"",
         r.reports.join(" | "));
  verify(r.calls.link === 1 && r.calls.positionTrace === 1,
         "die Fahrt läuft an");
  verify(r.calls.state === "faehrt"
         && r.calls.order.join() === "zustand,position",
         "und beginnt im Zustand 'fährt', noch bevor die Position verfolgt wird - "
         + "wer die Aufzeichnung startet, sitzt im Auto. Sonst begann sie im "
         + "Zustand 'steht' und las im Stand nie: Die Live-Anzeige blieb leer",
         `${r.calls.state} / ${r.calls.order.join()}`);

  console.log("\nDie Verbindung ist weg");
  r = await cycle({ connectionPath: true });
  verify(r.calls.dongleUse === 1 && r.calls.reconnect === 1,
         "ist die Verbindung weg, gilt der Dongle als nicht da - jolt versucht es von selbst weiter",
         String(r.calls.dongleUse));
  verify(/Ladestand unterwegs/.test(r.reports.join(" ")),
         "und der Ladestand kommt von Hand", r.reports.join(" | "));

  console.log("\nKein Dongle");
  r = await cycle({ connectedPastConnect: false });
  verify(r.sent.length === 1,
         "ohne Verbindung läuft die Aufzeichnung ohne Dongle weiter");
  verify(r.calls.dongleUse === 1 && r.calls.reconnect === 1,
         "und jolt klopft von selbst wieder an, statt auf den Knopf zu warten",
         JSON.stringify(r.calls));
  verify(r.calls.state === "faehrt",
         "der Zustand gilt auch dann - er sagt nur, ob gelesen werden darf, "
         + "falls später doch ein Dongle verbunden wird");

  console.log(failure ? `\n${failure} Prüfung(en) fehlgeschlagen.`
                     : "\nAlle Prüfungen bestanden.");
  process.exit(failure ? 1 : 0);
})();
