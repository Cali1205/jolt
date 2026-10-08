#!/usr/bin/env node
// Checks how the browser reads times from the server (frontend/core.js: K.zeit).
//
// The server stores UTC without a zone. A browser reads a text like
// "2026-10-05T15:56:21" as **local time**: in summer time it then said 15:56 where
// it was 17:56 - the trip list showed the wrong time, and after
// reloading live.js considered the last vehicle values two hours old and
// rebuilt the dongle connection.
//
// The server now delivers UTC with Z (backend/app/timestamp.py). K.zeit is the
// second safeguard for everything that arrives without a zone.
//
// The result must not depend on the computer's time zone; therefore
// the same check is repeated under several time zones:
//
//     node tools/check_timestamp.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { spawnSync } = require("child_process");

const frontend = path.join(__dirname, "..", "frontend");

if (!process.env.JOLT_TIMEZONE_CHECKED) {
  // Run the same file under several time zones.
  let failures = 0;
  for (const zone of ["Europe/Berlin", "UTC", "Pacific/Auckland", "America/Los_Angeles"]) {
    const cycle = spawnSync(process.execPath, [__filename], {
      env: { ...process.env, TZ: zone, JOLT_TIMEZONE_CHECKED: "1" }, encoding: "utf8" });
    const tail = (cycle.stdout || "").trim().split("\n").slice(-1)[0];
    console.log(`${zone.padEnd(22)} ${cycle.status === 0 ? "ok" : "FEHLT"}  ${tail}`);
    if (cycle.status !== 0) {
      failures++;
      console.log((cycle.stdout || "") + (cycle.stderr || ""));
    }
  }
  console.log(failures ? `\n${failures} Zeitzone(n) fehlgeschlagen.`
                           : "\nAlle Prüfungen bestanden.");
  process.exit(failures ? 1 : 0);
}

let failure = 0;
function verify(ok, text, detail) {
  if (!ok) { console.log("  FEHLT " + text + "  -> " + detail); failure++; }
}

const empty = () => new Proxy(function () { return ""; }, {
  get: (z, n) => (n === Symbol.toPrimitive ? () => "" : n === "style" ? {} : empty()),
  apply: () => empty(), set: () => true });
const timeframe = { addEventListener() {} };
const context = {
  window: timeframe, console, setTimeout, clearTimeout, Date, JSON, Math, Number,
  document: { getElementById: () => empty(), addEventListener() {}, querySelector: () => empty() },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  fetch: async () => ({ ok: true, json: async () => ({}) }),
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(frontend, "core.js"), "utf8"), context);
const K = timeframe.jolt;

// 15:56:21 UTC, no matter where the device is.
const correct = Date.UTC(2026, 9, 5, 15, 56, 21);

verify(K.timeMs("2026-10-05T15:56:21") === correct,
       "ohne Zone gilt UTC, nicht die Ortszeit des Geraets (der Fehler)",
       String(K.timeMs("2026-10-05T15:56:21")) + " statt " + correct);
verify(K.timeMs("2026-10-05T15:56:21.646968") === correct + 646,
       "auch mit Mikrosekunden, wie sie die Datenbank liefert",
       String(K.timeMs("2026-10-05T15:56:21.646968") - correct));
verify(K.timeMs("2026-10-05T15:56:21Z") === correct, "mit Z ohnehin", "");
verify(K.timeMs("2026-10-05T15:56:21.646968Z") === correct + 646,
       "mit Z und Mikrosekunden", "");
verify(K.timeMs("2026-10-05T17:56:21+02:00") === correct,
       "mit Offset wird der Offset beachtet, nicht ueberschrieben", "");
verify(K.timeMs("2026-10-05T17:56:21+0200") === correct, "auch ohne Doppelpunkt", "");
verify(K.timeMs("2026-10-05T10:56:21-05:00") === correct, "und mit Minus", "");
verify(K.timeMs("2026-10-05") === Date.UTC(2026, 9, 5),
       "ein Datum ohne Uhrzeit bleibt, was es war", String(K.timeMs("2026-10-05")));

for (const nonsense of [null, undefined, "", "gestern", 12345, {}, "2026-13-45T99:99:99"]) {
  verify(K.timestamp(nonsense) === null && Number.isNaN(K.timeMs(nonsense)),
         "Unlesbares ist null bzw. NaN, kein Absturz und kein Datum aus dem Nichts",
         String(nonsense));
}

// The comparison where it showed up: how old is a measured value?
const measurement_time = K.timeMs("2026-10-05T15:56:21");
const age = (correct + 90 * 1000) - measurement_time;
verify(age === 90000,
       "90 Sekunden nach der Messung sind 90 Sekunden vergangen - nicht 2 Stunden und 90 Sekunden",
       String(age));

// Safety check: nobody should read a server date with `new Date(...)` again.
const forbidden = [
  ["trips.js", /new Date\(iso\)/],
  ["live.js", /new Date\(p\.timestamp\)/],
  ["live.js", /new Date\(letzter\.timestamp\)/],
];
for (const [file, pattern] of forbidden) {
  const text = fs.readFileSync(path.join(frontend, file), "utf8");
  verify(!pattern.test(text),
         `${file} liest ein Server-Datum direkt mit new Date - ueber K.zeit`, String(pattern));
}
const trips = fs.readFileSync(path.join(frontend, "trips.js"), "utf8");
verify(/K\.timestamp\(/.test(trips), "trips.js liest das Datum ueber K.zeit", "");
const live = fs.readFileSync(path.join(frontend, "live.js"), "utf8");
verify((live.match(/K\.timeMs\(/g) || []).length >= 2,
       "live.js liest beide Messzeiten ueber K.timeMs", "");

console.log(failure ? `${failure} Pruefung(en) fehlgeschlagen.` : "Alle Pruefungen bestanden.");
process.exit(failure ? 1 : 0);
