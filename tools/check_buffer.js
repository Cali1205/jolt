#!/usr/bin/env node
// Prueft die Messpunkt-Warteschlange aus frontend/live.js gegen einen Server,
// der ausfaellt: Funkloch, Nachreichen in Stapeln, 409/422. Ohne Browser, ohne
// Netz - live.js laeuft in Node gegen Attrappen.
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
let grid = true;           // false = "Server nicht erreichbar"
let nextStatus = null;     // erzwungener HTTP-Status
let badTime = null;  // ein Stapel mit diesem Punkt wird mit 422 abgelehnt
let rejectedRequests = 0;
const posts = [];          // was beim Server ankam

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
// zustandAnzeigen braucht DOM - fuer den Test ohne Wirkung.

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
let t = Date.now() - 600000;
async function point(pace = null) {
  t += 12000;
  // Die Sperre gegen zu haeufige Meldungen umgehen: jede Meldung ist 12 s
  // spaeter nach Messzeit, aber der Takt in der Funktion nutzt die Wanduhr.
  context.Date = Date;
  geoCallback({ coords: { latitude: 52, longitude: 10, speed: pace,
                          altitude: null }, timestamp: t });
  await wait(20);
}

(async () => {
  live.positionTrace();
  timeframe.joltBlePlugin = undefined;

  // Die Meldesperre (12 s Wanduhr) schlaegt im Test zu - deshalb ueber
  // visibilitychange zuruecksetzen waere noetig; einfacher: Date.now patchen.
  let now_ts = Date.now();
  const realNow = Date.now;
  Date.now = () => (now_ts += 13000);

  console.log("\nNetz da");
  await point();
  verify(posts.length === 1 && posts[0].fs_path === "/api/live/7/punkte"
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
  // Der Server lehnt seit der Absicherung Unmoegliches mit 422 ab. Frueher
  // flog dann der ganze Stapel raus - wegen eines Punktes bis zu 99 gute.
  grid = false;
  const measurement_times = [];
  for (let i = 0; i < 12; i++) { await point(); measurement_times.push(new Date(t).toISOString()); }
  badTime = measurement_times[5];
  grid = true;
  rejectedRequests = 0;
  reports.length = 0;
  const priorBatch = posts.length;
  await point();                       // 13 Punkte warten: Stapel mit dem schlechten
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
  // Danach geht es mit voller Stapelgroesse weiter.
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
  // Der Punkt ist schon im Puffer: er wird gesendet, abgelehnt, verworfen.
  await point();                       // dieser zweite geht durch
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
