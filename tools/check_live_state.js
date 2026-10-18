#!/usr/bin/env node
// Checks what the live view shows for each state message it receives
// (frontend/live.js: showState and what it calls).
//
// A state message comes in through the WebSocket. The test feeds a planned trip
// and a recording (no route, no plan) a few messages each, including a repeated
// one, a changed plan and an unmeasured state of charge, and records everything
// that is written to the page (values, bars, hint, response, plan list,
// consumption foot), every canvas call of the two charts, every call to the map
// and what goes to the display model (CarPlay).
//
// The expected record in tools/fixtures/live_state_baseline.json was taken from
// showState as it stood in one 158-line function; it pins what the function
// does so that it can be taken apart. Regenerate it on purpose, not by habit:
//
//     node tools/check_live_state.js            checks
//     node tools/check_live_state.js --record   writes the baseline
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { canvasStub } = require("./fixtures/consumption_cases.js");

const FRONTEND = path.join(__dirname, "..", "frontend");
const BASELINE = path.join(__dirname, "fixtures", "live_state_baseline.json");
const record = process.argv.includes("--record");

let failure = 0;
function verify(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) failure++;
}

function round(v) { return typeof v === "number" ? Math.round(v * 1000) / 1000 : v; }

function scenario(name, trip, messages) {
  const log = [];
  const stores = {};
  const canvasLogs = {};
  const empty = () => new Proxy(function () { return ""; }, {
    get: (z, n) => (n === Symbol.toPrimitive ? () => "" : n === "style" ? {} : empty()),
    apply: () => empty(), set: () => true });
  const methods = new Set(["appendChild", "addEventListener", "setAttribute", "removeAttribute",
                           "querySelector", "querySelectorAll", "focus", "blur", "remove",
                           "insertAdjacentHTML", "removeEventListener"]);
  function element(id) {
    if (stores[id]) return stores[id].proxy;
    const store = { children: [{ textContent: "" }, { textContent: "" }], classList: { add() {}, remove() {}, toggle() {} } };
    const styleStore = {};
    store.style = new Proxy(styleStore, { set: (z, n, v) => { z[n] = v; log.push(`${id}.style.${String(n)}=${v}`); return true; } });
    if (id === "live-history" || id === "live-consumption") {
      const clog = [];
      canvasLogs[id] = clog;
      Object.assign(store, canvasStub(clog));
    }
    const proxy = new Proxy(store, {
      get: (z, n) => {
        if (n in z) return z[n];
        if (methods.has(n)) return () => empty();
        if (n === "clientWidth") return 300;
        if (n === "clientHeight") return 100;
        return empty();
      },
      set: (z, n, v) => { z[n] = v; log.push(`${id}.${String(n)}=${typeof v === "object" ? JSON.stringify(v) : v}`); return true; },
    });
    stores[id] = { proxy, store };
    return proxy;
  }

  const sockets = [];
  function WebSocket() { sockets.push(this); this.send = () => {}; this.close = () => {}; }
  const mapCalls = [];
  const displayCalls = [];
  const timeframe = {
    addEventListener() {}, devicePixelRatio: 2, joltBlePlugin: undefined,
    joltMap: {
      setRoute: (r) => mapCalls.push(["setRoute", r.length]),
      setMarker: (m) => mapCalls.push(["setMarker", m]),
    },
    joltDisplay: {
      report: (z, extra) => displayCalls.push([round(z.actual_soc), extra.track.length, !!extra.plan]),
      finish() {},
    },
    joltApp: { showView() {} },
  };
  const store = {};
  let clock = 1700000000000;
  class FixedDate extends Date { static now() { return (clock += 13000); } }
  const context = {
    window: timeframe, console: { log() {} }, setTimeout, clearTimeout, setInterval() {}, clearInterval() {},
    Date: FixedDate, JSON, Math, Number, String, Promise, Map, Set, Array, Object, Request, Response, WebSocket,
    location: { protocol: "http:", host: "test" },
    document: { addEventListener() {}, getElementById: (id) => element(id), querySelector: () => empty(),
                querySelectorAll: () => [], createElement: () => empty(), body: { appendChild() {} },
                visibilityState: "visible" },
    navigator: { geolocation: { watchPosition: () => 1, clearWatch() {} } },
    localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; },
                    removeItem: (k) => { delete store[k]; } },
  };
  vm.createContext(context);
  const run = (file) => vm.runInContext(fs.readFileSync(path.join(FRONTEND, file), "utf8"), context);
  for (const f of ["core.js", "awake.js", "notify.js", "consumption.js", "drive-state.js", "buffer.js",
                   "history-chart.js", "live.js"]) run(f);
  const K = timeframe.jolt;
  K.state.sessionId = 7;
  K.state.trip = trip;
  K.state.vehicles = [];
  timeframe.joltLive.link(7);
  const ws = sockets[sockets.length - 1];
  for (const m of messages) ws.onmessage({ data: JSON.stringify(m) });
  return { name, writes: log.filter((l) => !/^live-(history|consumption)\./.test(l)),
           charts: canvasLogs, map: mapCalls, display: displayCalls };
}

const profile = Array.from({ length: 12 }, (_, i) => ({
  km: i * 25, soc: 90 - i * 6, elevation: 200 + 40 * Math.sin(i), lat: 48 + i * 0.1, lon: 9 + i * 0.1 }));
const trip = { profile, vehicle: { reserve_soc: 12, capacity_kwh: 77 } };
const stops = [{ km_on_route: 110, lat: 48.5, lon: 9.5, charge_time_minutes: 32, name: "Ladepark <A>", point_count: 6,
                 arrival_soc: 14, departure_soc: 80, max_kw: 150 }];
const plan = { feasible: true, stops, reading_km: 40 };

function z(o) {
  return Object.assign({
    kind: "zustand", actual_soc: 80, soc_reported: true, soc_source: "gemessen", deviation_pp: -1.5,
    forecast_soc_at_target: 21, arrival_shift_min: 4, remaining_km: 210, km_on_route: 30, reason: "",
    replanning_required: false, reserve_at_km: 190, lat: 48.3, lon: 9.3, next_stop: null,
    plan: null, plan_changed: false, change: null,
  }, o);
}

const scenarios = [
  scenario("planned", trip, [
    z({ plan, next_stop: { name: "Ladepark", km_on_route: 110, expected_soc: 14 } }),
    z({ actual_soc: 77.6, km_on_route: 38, lat: 48.38, lon: 9.38, deviation_pp: -3, soc_source: "zuletzt" }),
    z({ actual_soc: 77.6, km_on_route: 38, lat: 48.38, lon: 9.38, deviation_pp: -3 }),
    z({ actual_soc: 70, km_on_route: 60, lat: 48.6, lon: 9.6, deviation_pp: -6, arrival_shift_min: 14,
        plan: Object.assign({}, plan, { reading_km: 60 }), plan_changed: true, change: "Neuer Stopp",
        reason: "Verbrauch höher als geplant", replanning_required: true, forecast_soc_at_target: 4 }),
    z({ actual_soc: 66, km_on_route: 70, lat: 48.7, lon: 9.7, forecast_soc_at_target: -3, arrival_shift_min: 0 }),
  ]),
  scenario("recording", null, [
    z({ lat: 48.1, lon: 9.1, km_on_route: null, forecast_soc_at_target: null, deviation_pp: null,
        arrival_shift_min: null, remaining_km: null, reserve_at_km: null }),
    z({ lat: 48.12, lon: 9.12, km_on_route: null, actual_soc: 79.6, soc_reported: false,
        forecast_soc_at_target: null, deviation_pp: null, arrival_shift_min: null, remaining_km: null }),
    z({ lat: 48.12, lon: 9.12, km_on_route: null, actual_soc: 79.6, soc_reported: false,
        forecast_soc_at_target: null, deviation_pp: null, arrival_shift_min: null, remaining_km: null }),
    z({ lat: 48.2, lon: 9.2, km_on_route: null, actual_soc: 9, forecast_soc_at_target: null,
        deviation_pp: null, arrival_shift_min: null, remaining_km: null }),
  ]),
];

if (record) {
  fs.writeFileSync(BASELINE, JSON.stringify(scenarios));
  console.log("baseline written: " + scenarios.map((s) => s.name + " " + s.writes.length + " writes").join(", "));
  process.exit(0);
}

const expected = JSON.parse(fs.readFileSync(BASELINE, "utf8"));
for (const got of scenarios) {
  const want = expected.find((e) => e.name === got.name);
  const same = (part) => JSON.stringify(got[part]) === JSON.stringify(want[part]);
  verify(same("writes"), got.name + ": dieselben Werte, Balken, Hinweise und Listen auf der Seite wie vorher",
         `${got.writes.length} Schreibvorgaenge, erwartet ${want.writes.length}`);
  verify(same("charts"), got.name + ": dieselben Zeichenbefehle fuer Verlauf und Verbrauch", "");
  verify(same("map"), got.name + ": dieselben Aufrufe an die Karte", JSON.stringify(got.map).slice(0, 160));
  verify(same("display"), got.name + ": dieselben Meldungen an die Anzeige (CarPlay)", JSON.stringify(got.display));
}
verify(scenarios[0].writes.some((w) => w.startsWith("live-values.innerHTML=")),
       "Plausibilitaet: die Wertekacheln werden wirklich geschrieben");

if (failure) { console.log("\n" + failure + " Pruefung(en) fehlgeschlagen."); process.exit(1); }
console.log("\nAlle Pruefungen bestanden.");
