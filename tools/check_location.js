#!/usr/bin/env node
// Checks the location path from frontend/live.js: in the browser via
// navigator.geolocation, in the iOS app via the background plugin.
//
// What can be checked without a device is checked here: that the plugin is created with
// the option that makes the background possible in the first place, that the
// measurement points go out with the fix's time (not the arrival time), that
// errors and cancellation are cleaned up properly. Whether iOS keeps the app
// running with a locked screen, only a trip shows.
//
//     node tools/check_location.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "live.js"), "utf8");

let failure = 0;
function verify(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) failure++;
}

const storage = {};
const reports = [];
const posts = [];
const K0 = {
  state: { sessionId: 7 },
  report: (t, variety) => reports.push({ t, variety }),
  at() {}, sliderCouple() {}, sessionRemember() {}, rememberedSession: () => null,
  api: async (fs_path, opt) => {
    posts.push({ fs_path, body: opt && opt.body });
    return { kind: "zustand" };
  },
};
const empty = () => new Proxy(function () { return ""; }, {
  get: (z, n) => (n === Symbol.toPrimitive ? () => "" : n === "style" ? {} : empty()),
  apply: () => empty(), set: () => true });
const K = new Proxy(K0, { get: (z, n) => (n in z ? z[n] : empty()) });

let webAwakeCreatedAt = 0;
let webCallback = null;
const timeframe = { jolt: K, addEventListener() {}, joltBlePlugin: undefined };
const context = {
  window: timeframe, console, setTimeout, clearTimeout, Date, JSON, Math, Promise,
  document: { addEventListener() {}, getElementById: () => empty(),
              querySelector: () => empty(), querySelectorAll: () => [],
              createElement: () => empty(), body: { appendChild() {} },
              visibilityState: "visible" },
  navigator: { geolocation: {
    watchPosition: (f) => { webAwakeCreatedAt++; webCallback = f; return 1; },
    clearWatch() {} } },
  localStorage: { getItem: (k) => (k in storage ? storage[k] : null),
                  setItem: (k, v) => { storage[k] = v; },
                  removeItem: (k) => { delete storage[k]; } },
  WebSocket: function () {},
};
vm.createContext(context);
vm.runInContext(source, context);
const live = timeframe.joltLive;

/* The display model (frontend/display.js): live.js reports every state and
 * the end of the trip to it. Only a recorder here - the model itself is checked by
 * tools/check_display.js. */
const display = { report: [], finish: 0, raises: false };
timeframe.joltDisplay = {
  report: (z) => { if (display.raises) throw new Error("Anzeige kaputt"); display.report.push(z); },
  finish: () => { display.finish++; },
};
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// The report throttle (12 s wall clock) would otherwise run along in the test.
let now_ts = Date.now();
const realNow = Date.now;
Date.now = () => (now_ts += 13000);

/* A simulated plugin: records what it was created and removed with. */
function plugin({ delayMs = 0, reject = false, obtainable = true } = {}) {
  const p = { created_at: [], removed: [], callback: null, hold_awake: 0 };
  p.BackgroundGeolocation = {
    addWatcher: async (options, callback) => {
      p.created_at.push(options);
      p.callback = callback;
      if (delayMs) await wait(delayMs);
      if (reject) throw new Error("kaputt");
      return "wacher-" + p.created_at.length;
    },
    removeWatcher: async (o) => { p.removed.push(o.id); },
  };
  p.Capacitor = { isNativePlatform: () => true,
                  isPluginAvailable: (n) => obtainable && n === "BackgroundGeolocation" };
  p.KeepAwake = { keepAwake: async () => { p.hold_awake++; },
                  allowSleep: async () => {} };
  return p;
}

(async () => {
  console.log("\nIm Browser");
  timeframe.joltBlePlugin = undefined;
  live.positionTrace();
  verify(webAwakeCreatedAt === 1, "ohne Plugin gilt navigator.geolocation");
  await live.finish();

  console.log("\nIn der App");
  const p = plugin();
  timeframe.joltBlePlugin = p;
  K0.state.sessionId = 7;
  webAwakeCreatedAt = 0;
  live.positionTrace();
  await wait(10);
  verify(webAwakeCreatedAt === 0,
         "mit dem Plugin wird der Browser-Standort nicht angefasst - er läuft " +
         "bei gesperrtem Telefon ohnehin nicht");
  verify(p.created_at.length === 1, "der Watcher wird genau einmal angelegt");
  const o = p.created_at[0] || {};
  verify(typeof o.backgroundMessage === "string" && o.backgroundMessage.length > 0,
         "mit backgroundMessage - ohne sie liefert das Plugin nur im " +
         "Vordergrund, und alles andere wäre umsonst",
         JSON.stringify(o));
  verify(o.requestPermissions === true, "und fragt selbst nach der Erlaubnis");
  verify(p.hold_awake >= 1, "der Bildschirm-Wachhalter läuft mit");
  live.positionTrace();
  await wait(10);
  verify(p.created_at.length === 1,
         "ein zweiter Aufruf legt keinen zweiten Watcher an");

  console.log("\nEin Fix kommt an");
  const fixTime = Date.now() - 5 * 60 * 1000;
  p.callback({ latitude: 52.5, longitude: 13.4, altitude: 40, speed: 27.5,
               time: fixTime, accuracy: 5 });
  await wait(30);
  const point = posts.filter((x) => /\/punkte$/.test(x.fs_path)).pop();
  const mp = point && point.body.points[point.body.points.length - 1];
  verify(!!mp && mp.lat === 52.5 && mp.lon === 13.4,
         "Position geht an den Server", JSON.stringify(point));
  verify(!!mp && Math.abs(mp.speed_kmh - 99) < 0.01,
         "Geschwindigkeit in km/h statt m/s", String(mp && mp.speed_kmh));
  verify(!!mp && new Date(mp.timestamp).getTime() === fixTime,
         "mit der Zeit des Fixes, nicht des Eintreffens - sonst wäre ein " +
         "nachgereichter Punkt auf die falsche Sekunde datiert",
         String(mp && mp.timestamp));
  p.callback({ latitude: 52.51, longitude: 13.41, altitude: null, speed: null,
               time: Date.now() });
  await wait(30);
  const withoutSpeed = posts.filter((x) => /\/punkte$/.test(x.fs_path)).pop().body.points.pop();
  verify(withoutSpeed.speed_kmh === null,
         "fehlt die Geschwindigkeit, bleibt sie leer statt 0 oder NaN");

  console.log("\nFehler");
  p.callback(undefined, Object.assign(new Error("x"), { code: "NOT_AUTHORIZED" }));
  p.callback(undefined, Object.assign(new Error("x"), { code: "NOT_AUTHORIZED" }));
  const warnings = reports.filter((m) => /Standort nicht erlaubt/.test(m.t));
  verify(warnings.length === 1,
         "fehlende Erlaubnis wird einmal gemeldet, nicht bei jedem Rückruf",
         String(warnings.length));
  verify(warnings[0] && /Immer|Verwenden/.test(warnings[0].t),
         "und sagt, was einzustellen ist");
  const earlier = reports.length;
  p.callback(undefined, Object.assign(new Error("Tunnel"), { code: "UNAVAILABLE" }));
  verify(reports.length === earlier,
         "ein anderer Fehler bleibt still - Tunnel gibt es",
         reports.slice(earlier).map((m) => m.t).join(" | "));

  console.log("\nAnzeigemodell");
  verify(display.report.length >= 1 && display.report[0].kind === "zustand",
         "jeder Zustand, den der Server liefert, geht auch ans Anzeigemodell - "
         + "fuer CarPlay und Widget", String(display.report.length));
  const postsPrior = display.report.length;
  display.raises = true;
  let ok = true;
  try {
    p.callback({ latitude: 52.6, longitude: 13.5, altitude: null, speed: 20, time: Date.now() });
    await wait(30);
  } catch (e) { ok = false; }
  verify(ok, "ein Fehler im Anzeigemodell reisst die Oberflaeche nicht mit");
  display.raises = false;
  verify(display.report.length === postsPrior, "(der Fehler kam vor dem Mitschreiben an)");

  console.log("\nBeenden");
  const endPrior = display.finish;
  await live.finish();
  await wait(10);
  verify(display.finish === endPrior + 1,
         "am Ende der Fahrt wird das Anzeigemodell abgemeldet - die Anzeige im "
         + "Auto soll verschwinden", String(display.finish - endPrior));
  verify(p.removed.length === 1 && p.removed[0] === "wacher-1",
         "der Watcher wird entfernt - sonst läuft die Ortung ohne Fahrt " +
         "weiter und kostet Akku", JSON.stringify(p.removed));

  console.log("\nBeenden, während iOS noch nach der Erlaubnis fragt");
  const late = plugin({ delayMs: 80 });
  timeframe.joltBlePlugin = late;
  K0.state.sessionId = 8;
  live.positionTrace();
  await wait(10);
  await live.finish();
  await wait(150);
  verify(late.removed.length === 1,
         "der erst danach eintreffende Watcher wird sofort wieder entfernt",
         JSON.stringify(late.removed));

  console.log("\nPlugin schlägt beim Anlegen fehl");
  const broken = plugin({ reject: true });
  timeframe.joltBlePlugin = broken;
  K0.state.sessionId = 9;
  webAwakeCreatedAt = 0;
  live.positionTrace();
  await wait(30);
  verify(webAwakeCreatedAt === 1,
         "dann gilt der Standort im Vordergrund - besser als gar keiner für " +
         "den Rest der Fahrt", String(webAwakeCreatedAt));
  live.positionTrace();
  await wait(10);
  verify(webAwakeCreatedAt === 1 && broken.created_at.length === 1,
         "und es gibt keinen zweiten Watcher daneben");
  await live.finish();

  console.log("\nÄltere App ohne das Plugin");
  const old = plugin({ obtainable: false });
  timeframe.joltBlePlugin = old;
  K0.state.sessionId = 10;
  webAwakeCreatedAt = 0;
  live.positionTrace();
  await wait(30);
  verify(old.created_at.length === 0 && webAwakeCreatedAt === 1,
         "ist die native Klasse nicht eingebaut, wird sie gar nicht erst " +
         "gerufen - der Server liefert die neue Oberfläche an eine App, die " +
         "älter sein kann als sie", `${old.created_at.length} / ${webAwakeCreatedAt}`);
  await live.finish();

  Date.now = realNow;
  console.log(failure ? `\n${failure} Prüfung(en) fehlgeschlagen.`
                     : "\nAlle Prüfungen bestanden.");
  process.exit(failure ? 1 : 0);
})();
