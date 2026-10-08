#!/usr/bin/env node
// Checks the measurement point queue from frontend/live.js against a server
// that goes down: dead zone, resending in batches, 409/422. No browser, no
// network - live.js runs in Node against stand-ins.
//
//     node tools/check_buffer.js
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(
  require("path").join(__dirname, "..", "frontend", "live.js"), "utf8");

let failure = 0;
function verify(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) failure++;
}

const storage = {};
const reports = [];
let grid = true;           // false = "server unreachable"
let nextStatus = null;     // forced HTTP status
let badTime = null;  // a batch containing this point is rejected with 422
let rejectedRequests = 0;
const posts = [];          // what arrived at the server

const empty = () => new Proxy(function () { return ""; }, {
  get: (z, n) => (n === Symbol.toPrimitive ? () => "" : n === "style" ? {} : empty()),
  apply: () => empty(), set: () => true });
const K0 = {
  state: { sessionId: 7 },
  report: (t) => reports.push(t),
  at() {}, sliderCouple() {}, sessionRemember() {}, rememberedSession: () => null,
  api: async (fs_path, opt) => {
    if (!grid) throw new Error("Server nicht erreichbar.");
    if (nextStatus) {
      const e = new Error("HTTP " + nextStatus); e.status = nextStatus; throw e;
    }
    if (badTime && opt.body && opt.body.points
        && opt.body.points.some((p) => p.timestamp === badTime)) {
      rejectedRequests++;
      const e = new Error("HTTP 422"); e.status = 422; throw e;
    }
    posts.push({ fs_path, body: opt.body });
    return { kind: "zustand", n: posts.length };
  },
};
const K = new Proxy(K0, { get: (z, n) => (n in z ? z[n] : empty()) });
let geoCallback = null;
const timeframeEvents = {};
const element = () => ({ style: {}, addEventListener() {}, play: async () => {},
  pause() {}, setAttribute() {}, appendChild() {}, hidden: false });
const timeframe = {
  jolt: K,
  addEventListener: (n, f) => { timeframeEvents[n] = f; },
  joltBlePlugin: undefined,
};
const context = {
  window: timeframe, console, setTimeout, clearTimeout, Date, JSON, Math,
  document: { addEventListener() {}, getElementById: () => empty(), querySelector: () => empty(), querySelectorAll: () => [],
              createElement: element, body: { appendChild() {} },
              visibilityState: "visible" },
  navigator: { geolocation: {
    watchPosition: (f) => { geoCallback = f; return 1; },
    clearWatch() {} } },
  localStorage: {
    getItem: (k) => (k in storage ? storage[k] : null),
    setItem: (k, v) => { storage[k] = v; },
    removeItem: (k) => { delete storage[k]; } },
  WebSocket: function () {},
};
context.window.localStorage = context.localStorage;
vm.createContext(context);
vm.runInContext(source, context);
const live = timeframe.joltLive;
// showState needs the DOM - without effect for the test.

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
let t = Date.now() - 600000;
async function point(pace = null) {
  t += 12000;
  // Get around the throttle on too frequent reports: each report is 12 s
  // later by measurement time, but the clock in the function uses wall time.
  context.Date = Date;
  geoCallback({ coords: { latitude: 52, longitude: 10, speed: pace,
                          altitude: null }, timestamp: t });
  await wait(20);
}

(async () => {
  live.positionTrace();
  timeframe.joltBlePlugin = undefined;

  // The report throttle (12 s wall clock) kicks in during the test - resetting
  // it via visibilitychange would be needed; simpler: patch Date.now.
  let now_ts = Date.now();
  const realNow = Date.now;
  Date.now = () => (now_ts += 13000);

  console.log("\nNetz da");
  await point();
  verify(posts.length === 1 && posts[0].fs_path === "/api/live/7/points"
         && posts[0].body.points.length === 1, "ein Punkt geht als Stapel von eins hinaus",
         JSON.stringify(posts));
  verify(typeof posts[0].body.points[0].timestamp === "string"
         && posts[0].body.points[0].timestamp.endsWith("Z"),
         "mit Messzeit im ISO-Format", posts[0].body.points[0].timestamp);
  verify(!storage["jolt-puffer-7"], "und nichts bleibt im Speicher liegen");

  console.log("\nFunkloch");
  grid = false;
  await point(); await point(); await point();
  verify(posts.length === 1, "ohne Netz kommt nichts an");
  const saved = JSON.parse(storage["jolt-puffer-7"] || "[]");
  verify(saved.length === 3, "drei Punkte liegen gesichert im Speicher",
         String(saved.length));
  verify(reports.filter((m) => /gesammelt/.test(m)).length === 1,
         "gemeldet wird das Funkloch einmal, nicht bei jedem Punkt",
         JSON.stringify(reports));

  console.log("\nNetz kommt zurueck");
  grid = true;
  await point();
  const last = posts[posts.length - 1].body.points;
  verify(last.length === 4, "alle vier gehen in einem Stapel hinaus",
         String(last.length));
  const times = last.map((p) => p.timestamp);
  verify(JSON.stringify(times) === JSON.stringify([...times].sort())
         && new Set(times).size === 4,
         "in Messreihenfolge und mit verschiedenen Zeiten");
  verify(!storage["jolt-puffer-7"], "danach ist der Speicher leer");
  verify(reports.some((m) => /nachgereicht/.test(m)),
         "und es wird gesagt, dass nachgereicht wurde");

  console.log("\nSitzung beendet");
  nextStatus = 409;
  const earlier = posts.length;
  await point();
  verify(!storage["jolt-puffer-7"] && posts.length === earlier,
         "409 leert die Warteschlange, statt sie ewig zu wiederholen");
  nextStatus = null;

  console.log("\nUngueltiger Stapel");
  grid = true; nextStatus = 422;
  await point();
  verify(!storage["jolt-puffer-7"], "422 verwirft den Stapel, er verstopft nichts");
  nextStatus = null;

  console.log("\nGrosser Rueckstau");
  grid = false;
  for (let i = 0; i < 250; i++) await point();
  grid = true;
  const prior = posts.length;
  await point();
  const fresh = posts.slice(prior).map((p) => p.body.points.length);
  verify(fresh.length === 3 && fresh[0] === 100 && fresh[1] === 100 && fresh[2] === 51,
         "251 Punkte gehen in Stapeln zu 100", JSON.stringify(fresh));

  console.log("\nEin schlechter Punkt im Stapel");
  // Since the hardening, the server rejects impossible values with 422. It used
  // to throw out the whole batch - up to 99 good points because of one.
  grid = false;
  const measurement_times = [];
  for (let i = 0; i < 12; i++) { await point(); measurement_times.push(new Date(t).toISOString()); }
  badTime = measurement_times[5];
  grid = true;
  rejectedRequests = 0;
  reports.length = 0;
  const priorBatch = posts.length;
  await point();                       // 13 points waiting: batch with the bad one
  const arrived = posts.slice(priorBatch).flatMap((x) => x.body.points.map((p) => p.timestamp));
  verify(arrived.length === 12 && !arrived.includes(badTime),
         "genau der schlechte Punkt fehlt, die zwoelf anderen sind angekommen",
         `${arrived.length} angekommen, schlechter dabei: ${arrived.includes(badTime)}`);
  verify(JSON.stringify(arrived) === JSON.stringify([...arrived].sort()),
         "in Messreihenfolge");
  verify(rejectedRequests >= 1 && rejectedRequests <= 5,
         "durch Halbieren gefunden - wenige Anfragen, nicht eine je Punkt",
         String(rejectedRequests));
  verify(!storage["jolt-puffer-7"], "und nichts bleibt liegen");
  verify(reports.some((m) => /1 Messpunkt wurde vom Server abgelehnt/.test(m)),
         "der Nutzer erfaehrt, dass einer verworfen wurde", JSON.stringify(reports));
  badTime = null;
  // After that it continues with the full batch size.
  grid = false;
  for (let i = 0; i < 150; i++) await point();
  grid = true;
  const vor2 = posts.length;
  await point();
  const gr = posts.slice(vor2).map((x) => x.body.points.length);
  verify(gr[0] === 100, "danach wieder volle Stapel zu 100", JSON.stringify(gr));

  console.log("\nDer einzige Punkt ist schlecht");
  grid = false;
  await point();
  badTime = new Date(t).toISOString();
  grid = true;
  rejectedRequests = 0;
  const vor3 = posts.length;
  badTime = new Date(t).toISOString();
  // The point is already in the buffer: it is sent, rejected, discarded.
  await point();                       // this second one goes through
  verify(rejectedRequests >= 1 && !storage["jolt-puffer-7"],
         "ein einzelner abgelehnter Punkt wird verworfen und verstopft nichts",
         String(rejectedRequests));
  badTime = null;

  console.log("\nWerte, die der Server ablehnen wuerde");
  const youngest = () => { const b = posts[posts.length - 1].body.points;
                          return b[b.length - 1]; };
  grid = true;
  await point(25);
  verify(Math.abs(youngest().speed_kmh - 90) < 1e-9, "25 m/s sind 90 km/h", String(youngest().speed_kmh));
  await point(null);
  verify(youngest().speed_kmh === null, "kein Tempo bleibt leer");
  await point(-1);
  verify(youngest().speed_kmh === null,
         "-1 (das Geraet weiss es nicht) wird nicht als Rueckwaertsfahrt gesendet",
         String(youngest().speed_kmh));
  await point(300);
  verify(youngest().speed_kmh === null,
         "1080 km/h sind ein Messfehler und gehen nicht hinaus", String(youngest().speed_kmh));
  await point(NaN);
  verify(youngest().speed_kmh === null, "NaN auch nicht");
  await point(250 / 3.6);
  verify(youngest().speed_kmh !== null, "250 km/h sind noch gueltig");

  Date.now = realNow;
  console.log(failure ? `\n${failure} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
  process.exit(failure ? 1 : 0);
})();
