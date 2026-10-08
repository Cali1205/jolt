#!/usr/bin/env node
// Checks starting and stopping the recording from CarPlay
// (frontend/trips.js: carplayAction).
//
// The CarPlay list sends an event ("carplayAction": start or
// stop) and gets the result back. In the car nobody sees the
// messages of the UI - a start that fails silently would be a
// button that does nothing there. So the main thing checked is: does an
// answer always come, and does it name the reason on an error?
//
//     node tools/check_carplay_action.js
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

function build(o) {
  o = o || {};
  const elemente = {
    "rec-vehicle": { value: o.vehicle === undefined ? "1" : o.vehicle,
                       selectedOptions: [{ textContent: " ID.Buzz " }] },
    "rec-name": { value: "" },
    "rec-start": { disabled: !!o.startRunning, textContent: "" },
    "rec-status": { textContent: "" },
    "live-empty": { hidden: false }, "live-content": { hidden: true },
  };
  const empty = () => new Proxy(function () { return ""; }, {
    get: (z, n) => (n === Symbol.toPrimitive ? () => "" : n === "style" ? {} : empty()),
    apply: () => empty(), set: () => true });

  const calls = { api: [], listener: {}, ready: [], result: [], finish: 0, saved: {} };
  const K = {
    state: { sessionId: o.running ? 7 : null, vehicles: [{ id: 1 }], tripsStale: false },
    report() {},
    at(id, event, f) { return (elemente[id] || empty()); },
    api: async (fs_path, opt) => { calls.api.push({ fs_path, body: opt && opt.body });
                                return { session_id: 9, trip_id: 3 }; },
    sessionRemember() {},
  };
  const plugin = {
    addListener: async (name, f) => { calls.listener[name] = f; return { remove() {} }; },
    ready: async (a) => { calls.ready.push(a); },
    actionResult: async (a) => { calls.result.push(a); },
  };
  const timeframe = {
    jolt: K,
    joltBlePlugin: o.browser ? undefined
      : { JoltDisplay: plugin, Capacitor: { isNativePlatform: () => true } },
    joltApp: { showView() {} },
    joltObd: { obtainable: () => false },
    joltLive: {
      link: () => { K.state.sessionId = 9; },
      positionTrace() {}, drivingStateStart() {}, dongleUse() {},
      reconnectDongle() {}, handshakeSafe: async () => false,
      finish: async () => { calls.finish++; if (!o.endStuck) K.state.sessionId = null; },
    },
  };
  const context = {
    window: timeframe, console: { log() {} }, setTimeout, Promise, Date, JSON, Math, Number, String,
    document: { getElementById: (id) => elemente[id] || empty(),
                querySelector: () => empty(), createElement: () => empty(),
                addEventListener() {} },
    navigator: { geolocation: {
      getCurrentPosition: (ok, no) => {
        if (o.noLocation) no({ message: "Standort nicht erlaubt" });
        else ok({ coords: { latitude: 48.47, longitude: 9.14 } });
      } } },
    localStorage: { getItem: () => null, setItem: (k, v) => { calls.saved[k] = v; },
                    removeItem() {} },
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  timeframe.joltTrips.set_up();
  return { f: timeframe, calls, K, elemente };
}

(async () => {
  console.log("Anbindung");
  let t = build();
  verify(typeof t.calls.listener.carplayAction === "function",
         "in der iOS-App haengt sich trips.js an das Ereignis 'carplayAction'");
  verify(t.calls.ready.length === 1 && t.calls.ready[0].vehicle === "ID.Buzz",
         "und meldet CarPlay das gewaehlte Fahrzeug (ohne Leerzeichen am Rand)",
         JSON.stringify(t.calls.ready));
  t = build({ vehicle: "" });
  verify(t.calls.ready[0] && t.calls.ready[0].vehicle === "",
         "ohne gewaehltes Fahrzeug meldet es einen leeren Namen - CarPlay zeigt dann keinen erfundenen");
  t = build({ browser: true });
  verify(Object.keys(t.calls.listener).length === 0,
         "im Browser (ohne Plugin) passiert nichts, und nichts bricht");

  console.log("\nStarten");
  t = build();
  await t.calls.listener.carplayAction({ action: "starten" });
  verify(t.calls.api.length === 1 && t.calls.api[0].fs_path === "/api/live/recording"
         && t.calls.api[0].body.vehicle_id === 1,
         "legt die Aufzeichnung mit dem gewaehlten (zuletzt benutzten) Fahrzeug an",
         JSON.stringify(t.calls.api));
  verify(t.calls.result.length === 1 && t.calls.result[0].ok === true
         && t.calls.result[0].action === "starten",
         "und meldet CarPlay den Erfolg", JSON.stringify(t.calls.result));
  verify(t.calls.saved["jolt-aufz-fahrzeug"] === "1",
         "das Fahrzeug wird als zuletzt benutzt gemerkt");

  t = build({ running: true });
  await t.calls.listener.carplayAction({ action: "starten" });
  verify(t.calls.api.length === 0 && t.calls.result[0].ok === true
         && /läuft schon/.test(t.calls.result[0].text),
         "laeuft schon eine Aufzeichnung, wird keine zweite angelegt - und das wird gesagt",
         JSON.stringify(t.calls));

  t = build({ startRunning: true });
  await t.calls.listener.carplayAction({ action: "starten" });
  verify(t.calls.api.length === 0 && t.calls.result[0].ok === false
         && /Start läuft/.test(t.calls.result[0].text),
         "ein Start, der gerade laeuft, wird nicht doppelt ausgeloest");

  console.log("\nStarten scheitert - CarPlay erfaehrt den Grund");
  t = build({ noLocation: true });
  await t.calls.listener.carplayAction({ action: "starten" });
  verify(t.calls.result[0].ok === false && /Standort/.test(t.calls.result[0].text)
         && t.calls.api.length === 0,
         "kein Standort: keine Aufzeichnung, und der Grund steht im Ergebnis",
         JSON.stringify(t.calls.result));
  t = build({ vehicle: "" });
  await t.calls.listener.carplayAction({ action: "starten" });
  verify(t.calls.result[0].ok === false && /Fahrzeug/.test(t.calls.result[0].text)
         && t.calls.api.length === 0,
         "kein Fahrzeug gewaehlt: wird nicht geraten, und CarPlay sagt es",
         JSON.stringify(t.calls.result));

  console.log("\nBeenden");
  t = build({ running: true });
  await t.calls.listener.carplayAction({ action: "beenden" });
  verify(t.calls.finish === 1 && t.calls.result[0].ok === true
         && t.calls.result[0].action === "beenden",
         "beendet die laufende Aufzeichnung und meldet es", JSON.stringify(t.calls.result));
  t = build({ running: true, endStuck: true });
  await t.calls.listener.carplayAction({ action: "beenden" });
  verify(t.calls.result[0].ok === false && /Beenden/.test(t.calls.result[0].text),
         "geht das Beenden schief, sagt CarPlay es - statt eine laufende Fahrt fuer beendet zu halten");
  t = build();
  await t.calls.listener.carplayAction({ action: "beenden" });
  verify(t.calls.finish === 0 && t.calls.result[0].ok === true,
         "ohne laufende Aufzeichnung gibt es nichts zu beenden");

  console.log("\nUnsinn");
  t = build();
  await t.calls.listener.carplayAction({ action: "loeschen" });
  await t.calls.listener.carplayAction(undefined);
  verify(t.calls.api.length === 0 && t.calls.result.length === 0 && t.calls.finish === 0,
         "eine unbekannte Aktion tut nichts");

  console.log(failure ? `\n${failure} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
  process.exit(failure ? 1 : 0);
})();
