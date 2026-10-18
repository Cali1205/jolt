#!/usr/bin/env node
// Checks the curve "plan and actual over the distance" (frontend/history-chart.js).
//
// The expected canvas calls in tools/fixtures/history_baseline.json were recorded
// from drawHistory as it stood inside live.js before it became a module:
// six synthetic situations (planned trip with charging stops, planned trip
// without a plan, a recording that measures its own distance, one point, no
// history, no trip). A change that alters what the curve looks like fails here.
//
//     node tools/check_history_chart.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { cases } = require("./fixtures/history_cases.js");
const { canvasStub } = require("./fixtures/consumption_cases.js");

const expected = JSON.parse(fs.readFileSync(
  path.join(__dirname, "fixtures", "history_baseline.json"), "utf8"));

let failure = 0;
function verify(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) failure++;
}

function load() {
  const timeframe = { jolt: { num: (v, d) => Number(v).toFixed(d || 0) }, devicePixelRatio: 2 };
  const context = { window: timeframe, console, Math, Number, String };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(
    path.join(__dirname, "..", "frontend", "history-chart.js"), "utf8"), context);
  return timeframe.joltHistoryChart;
}

const chart = load();
for (const [name, c] of Object.entries(cases)) {
  const log = [];
  const canvas = canvasStub(log);
  chart.draw(canvas, c.trip, c.history, c.plan);
  const want = expected[name];
  verify(canvas.width === want.canvas.width && canvas.height === want.canvas.height
         && JSON.stringify(log) === JSON.stringify(want.log),
         name + ": dieselben Zeichenbefehle wie vor dem Auslagern",
         log.length + " Aufrufe, erwartet " + want.log.length);
}
verify(chart.draw(null, null, [], null) === undefined,
       "ohne Zeichenflaeche passiert nichts und nichts bricht");
const flat = { clientWidth: 0, clientHeight: 0, getContext() { throw new Error("darf nicht gezeichnet werden"); } };
verify(chart.draw(flat, null, [], null) === undefined,
       "eine Flaeche ohne Groesse (versteckter Tab) wird nicht beruehrt");

if (failure) { console.log("\n" + failure + " Pruefung(en) fehlgeschlagen."); process.exit(1); }
console.log("\nAlle Pruefungen bestanden.");
