#!/usr/bin/env node
// Checks the bar chart "consumption per time section" (frontend/consumption.js).
//
// The expected values in tools/fixtures/consumption_baseline.json were recorded
// from the code as it stood inside live.js before it became a module: the
// sections and every canvas call for seven synthetic trips (with energy
// counter, from the state of charge only, no battery size, too few points,
// standing, recuperation, a long trip that widens the bars). A change that
// alters what the chart shows fails here.
//
//     node tools/check_consumption.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { cases, canvasStub } = require("./fixtures/consumption_cases.js");

const expected = JSON.parse(fs.readFileSync(
  path.join(__dirname, "fixtures", "consumption_baseline.json"), "utf8"));

let failure = 0;
function verify(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) failure++;
}

function load(K, document) {
  const timeframe = { jolt: K, devicePixelRatio: 2 };
  const context = { window: timeframe, document, console, Math, Number, String, Map, Set, Array };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(
    path.join(__dirname, "..", "frontend", "consumption.js"), "utf8"), context);
  return timeframe.joltConsumption;
}

for (const [name, c] of Object.entries(cases)) {
  const log = [];
  const canvas = canvasStub(log);
  const foot = { hidden: false, children: [{ textContent: "" }, { textContent: "" }] };
  const K = { num: (v, d) => Number(v).toFixed(d || 0) };
  const chart = load(K, {});
  const sections = chart.sections(c.pts, c.vehicle);
  chart.draw(canvas, foot, c.pts, c.vehicle);
  const got = {
    sections,
    canvas: { hidden: canvas.hidden, width: canvas.width, height: canvas.height },
    foot: { hidden: foot.hidden, a: foot.children[0].textContent, b: foot.children[1].textContent },
    log,
  };
  const want = expected[name];
  verify(JSON.stringify(got.sections) === JSON.stringify(want.sections),
         name + ": dieselben Abschnitte wie vor dem Auslagern",
         JSON.stringify(got.sections).slice(0, 120));
  verify(JSON.stringify([got.canvas, got.foot, got.log]) === JSON.stringify([want.canvas, want.foot, want.log]),
         name + ": dieselben Zeichenbefehle, Fusszeile und Sichtbarkeit", "");
}

const chart = load({ num: String }, {});
verify(chart.draw(null, null, [], {}) === undefined,
       "ohne Zeichenflaeche passiert nichts und nichts bricht");

if (failure) { console.log("\n" + failure + " Pruefung(en) fehlgeschlagen."); process.exit(1); }
console.log("\nAlle Pruefungen bestanden.");
