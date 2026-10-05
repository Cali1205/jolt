#!/usr/bin/env node
// Prueft, wie der Browser Zeiten vom Server liest (frontend/core.js: K.zeit).
//
// Der Server speichert UTC ohne Zone. Ein Browser liest einen Text wie
// "2026-10-05T15:56:21" als **Ortszeit**: In Sommerzeit stand dann 15:56 Uhr, wo
// es 17:56 war - die Fahrtenliste zeigte die falsche Uhrzeit, und nach dem
// Neuladen hielt live.js die letzten Fahrzeugwerte fuer zwei Stunden alt und
// baute die Dongle-Verbindung neu auf.
//
// Der Server liefert jetzt UTC mit Z (backend/app/zeit.py). K.zeit ist die
// zweite Absicherung fuer alles, was ohne Zone ankommt.
//
// Das Ergebnis darf nicht von der Zeitzone des Rechners abhaengen; deshalb wird
// dieselbe Pruefung unter mehreren Zeitzonen wiederholt:
//
//     node tools/check_zeit.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { spawnSync } = require("child_process");

const frontend = path.join(__dirname, "..", "frontend");

if (!process.env.JOLT_ZEITZONE_GEPRUEFT) {
  // Dieselbe Datei unter mehreren Zeitzonen ausfuehren.
  let fehlschlaege = 0;
  for (const zone of ["Europe/Berlin", "UTC", "Pacific/Auckland", "America/Los_Angeles"]) {
    const lauf = spawnSync(process.execPath, [__filename], {
      env: { ...process.env, TZ: zone, JOLT_ZEITZONE_GEPRUEFT: "1" }, encoding: "utf8" });
    const letzte = (lauf.stdout || "").trim().split("\n").slice(-1)[0];
    console.log(`${zone.padEnd(22)} ${lauf.status === 0 ? "ok" : "FEHLT"}  ${letzte}`);
    if (lauf.status !== 0) {
      fehlschlaege++;
      console.log((lauf.stdout || "") + (lauf.stderr || ""));
    }
  }
  console.log(fehlschlaege ? `\n${fehlschlaege} Zeitzone(n) fehlgeschlagen.`
                           : "\nAlle Prüfungen bestanden.");
  process.exit(fehlschlaege ? 1 : 0);
}

let fehler = 0;
function pruefe(ok, text, detail) {
  if (!ok) { console.log("  FEHLT " + text + "  -> " + detail); fehler++; }
}

const leer = () => new Proxy(function () { return ""; }, {
  get: (z, n) => (n === Symbol.toPrimitive ? () => "" : n === "style" ? {} : leer()),
  apply: () => leer(), set: () => true });
const fenster = { addEventListener() {} };
const kontext = {
  window: fenster, console, setTimeout, clearTimeout, Date, JSON, Math, Number,
  document: { getElementById: () => leer(), addEventListener() {}, querySelector: () => leer() },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  fetch: async () => ({ ok: true, json: async () => ({}) }),
};
vm.createContext(kontext);
vm.runInContext(fs.readFileSync(path.join(frontend, "core.js"), "utf8"), kontext);
const K = fenster.jolt;

// 15:56:21 UTC, egal wo das Geraet steht.
const richtig = Date.UTC(2026, 9, 5, 15, 56, 21);

pruefe(K.zeitMs("2026-10-05T15:56:21") === richtig,
       "ohne Zone gilt UTC, nicht die Ortszeit des Geraets (der Fehler)",
       String(K.zeitMs("2026-10-05T15:56:21")) + " statt " + richtig);
pruefe(K.zeitMs("2026-10-05T15:56:21.646968") === richtig + 646,
       "auch mit Mikrosekunden, wie sie die Datenbank liefert",
       String(K.zeitMs("2026-10-05T15:56:21.646968") - richtig));
pruefe(K.zeitMs("2026-10-05T15:56:21Z") === richtig, "mit Z ohnehin", "");
pruefe(K.zeitMs("2026-10-05T15:56:21.646968Z") === richtig + 646,
       "mit Z und Mikrosekunden", "");
pruefe(K.zeitMs("2026-10-05T17:56:21+02:00") === richtig,
       "mit Offset wird der Offset beachtet, nicht ueberschrieben", "");
pruefe(K.zeitMs("2026-10-05T17:56:21+0200") === richtig, "auch ohne Doppelpunkt", "");
pruefe(K.zeitMs("2026-10-05T10:56:21-05:00") === richtig, "und mit Minus", "");
pruefe(K.zeitMs("2026-10-05") === Date.UTC(2026, 9, 5),
       "ein Datum ohne Uhrzeit bleibt, was es war", String(K.zeitMs("2026-10-05")));

for (const unsinn of [null, undefined, "", "gestern", 12345, {}, "2026-13-45T99:99:99"]) {
  pruefe(K.zeit(unsinn) === null && Number.isNaN(K.zeitMs(unsinn)),
         "Unlesbares ist null bzw. NaN, kein Absturz und kein Datum aus dem Nichts",
         String(unsinn));
}

// Der Vergleich, an dem es sich zeigte: Wie alt ist ein Messwert?
const messzeit = K.zeitMs("2026-10-05T15:56:21");
const alter = (richtig + 90 * 1000) - messzeit;
pruefe(alter === 90000,
       "90 Sekunden nach der Messung sind 90 Sekunden vergangen - nicht 2 Stunden und 90 Sekunden",
       String(alter));

// Schutzprobe: Niemand soll ein Server-Datum wieder mit `new Date(...)` lesen.
const verboten = [
  ["fahrten.js", /new Date\(iso\)/],
  ["live.js", /new Date\(p\.zeit\)/],
  ["live.js", /new Date\(letzter\.zeit\)/],
];
for (const [datei, muster] of verboten) {
  const text = fs.readFileSync(path.join(frontend, datei), "utf8");
  pruefe(!muster.test(text),
         `${datei} liest ein Server-Datum direkt mit new Date - ueber K.zeit`, String(muster));
}
const fahrten = fs.readFileSync(path.join(frontend, "fahrten.js"), "utf8");
pruefe(/K\.zeit\(/.test(fahrten), "fahrten.js liest das Datum ueber K.zeit", "");
const live = fs.readFileSync(path.join(frontend, "live.js"), "utf8");
pruefe((live.match(/K\.zeitMs\(/g) || []).length >= 2,
       "live.js liest beide Messzeiten ueber K.zeitMs", "");

console.log(fehler ? `${fehler} Pruefung(en) fehlgeschlagen.` : "Alle Pruefungen bestanden.");
process.exit(fehler ? 1 : 0);
