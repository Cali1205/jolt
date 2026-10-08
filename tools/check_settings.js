#!/usr/bin/env node
/* Checks the dongle buttons in the settings: connect and listen.
 *
 * Trigger: the listen button seemingly did nothing in the car. After parking
 * the dongle is disconnected, the settings had no way to
 * connect it, and the button reported "Kein Dongle verbunden" as a hint that
 * vanished after six seconds. So the whole path is checked,
 * from the tap to the text in the result field - against a simulated
 * dongle module, without a browser.
 *
 *     node tools/check_settings.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "settings.js"), "utf8");

let failure = 0;
function verify(ok, text, extra) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "   " + (extra || "")));
  if (!ok) failure += 1;
}

function build(options) {
  const o = options || {};
  const elemente = {};
  const element = (id) => {
    if (!elemente[id]) {
      elemente[id] = { id, hidden: false, disabled: false, textContent: "", value: "",
                       checked: false, innerHTML: "", style: {}, tap: null,
                       addEventListener(n, f) { if (n === "click") this.tap = f; },
                       dispatchEvent() {}, click() {} };
    }
    return elemente[id];
  };
  element("listen-log").value = "6";
  element("listen-duration").value = "10000";

  const reports = [];
  const calls = { attach: 0, listen: [], detach: 0 };
  let linked = !!o.linked;
  const O = {
    obtainable: () => o.bluetooth !== false,
    linked: () => linked,
    attach: async () => {
      calls.attach += 1;
      if (o.connectionFails) throw new Error("kein Gerät gewählt");
      linked = !o.remainsSeparate;
    },
    listen: async (opt) => {
      calls.listen.push(opt);
      if (!linked) throw new Error("nicht verbunden");
      return { trace_log: opt.trace_log, duration_ms: opt.duration_ms, total: o.frames || 0,
               ids: (o.frames ? [{ id: "7B0", n: o.frames, perSec: 5, variants: 2,
                                   tail: "06 62 02 8C B4 00 00" }] : []),
               probes: [], hints: [] };
    },
    diagnose: () => ({ linked, connection: { device: "IOS-Vlink" }, commands: {}, readings: {},
                       rounds: {}, changeFailed: [], tablesError: [] }),
    trace_log: () => [], FIELDS: [],
    detach() { calls.detach += 1; linked = false; },
    cli: async () => "OK", forget() {},
    logClear() {}, resetCounter() {},
  };
  const K = {
    state: { sessionId: o.tripRunning ? 7 : null },
    report: (t, variety) => reports.push({ t, variety }),
    at: (id, event, f) => { element(id).addEventListener(event, f); return element(id); },
    api: async () => ({}), num: (x) => String(x),
  };
  const timeframe = {
    jolt: K, joltObd: O,
    confirm: () => true, navigator: { clipboard: { writeText: async () => {} } },
    document: {
      getElementById: element,
      createElement: () => ({ set textContent(t) { this._t = t; }, get innerHTML() { return this._t || ""; } }),
      querySelector: () => null, querySelectorAll: () => [],
    },
    setInterval: () => 1, clearInterval() {}, setTimeout, clearTimeout, console, Date, JSON, Math,
    Number, String, Array, Object, Promise, Set, Map, Event: function () {},
    localStorage: { getItem: () => null, setItem() {} },
  };
  timeframe.window = timeframe;
  vm.createContext(timeframe);
  vm.runInContext(source, timeframe);
  timeframe.joltSettings.set_up();
  return { element, reports, calls, f: timeframe, setConnected: (v) => { linked = v; } };
}

async function main() {
  console.log("Mithoeren ohne verbundenen Dongle");
  let t = build({ linked: false, frames: 120 });
  await t.element("listen-start").tap();
  verify(t.calls.attach === 1 && t.calls.listen.length === 1,
         "der Knopf verbindet zuerst und hoert dann zu (nach dem Parken ist der Dongle getrennt)",
         JSON.stringify(t.calls));
  verify(t.calls.listen[0].trace_log === "6" && t.calls.listen[0].duration_ms === 10000,
         "mit dem gewaehlten Protokoll und der Dauer");
  const result = t.element("listen-result");
  verify(result.hidden === false && /120 Frames/.test(result.textContent)
         && /7B0/.test(result.textContent),
         "das Ergebnis steht im Feld, mit Kennung und Anzahl", result.textContent);
  verify(t.element("listen-copy").hidden === false, "und der Kopieren-Knopf erscheint");
  verify(t.element("listen-start").disabled === false, "der Knopf ist danach wieder frei");

  console.log("\nMithoeren: Rueckmeldungen stehen im Feld, nicht in einer Meldung");
  t = build({ linked: false, connectionFails: true });
  await t.element("listen-start").tap();
  verify(/kein Gerät gewählt/.test(t.element("listen-result").textContent)
         && t.calls.listen.length === 0,
         "scheitert das Verbinden, steht der Grund im Ergebnisfeld - und es wird nicht gelauscht",
         t.element("listen-result").textContent);
  verify(t.reports.length === 0,
         "ohne fluechtige Meldung, die nach sechs Sekunden weg ist");
  verify(t.element("listen-start").disabled === false, "der Knopf bleibt benutzbar");

  t = build({ linked: false, remainsSeparate: true });
  await t.element("listen-start").tap();
  verify(/keine Verbindung/.test(t.element("listen-result").textContent)
         && t.calls.listen.length === 0,
         "kommt trotz Dialog keine Verbindung zustande, sagt das Feld es");

  t = build({ bluetooth: false });
  await t.element("listen-start").tap();
  verify(/kein Bluetooth/.test(t.element("listen-result").textContent),
         "ein Browser ohne Bluetooth bekommt einen klaren Satz",
         t.element("listen-result").textContent);

  t = build({ linked: true, tripRunning: true });
  await t.element("listen-start").tap();
  verify(/Fahrt läuft/.test(t.element("listen-result").textContent)
         && t.calls.listen.length === 0 && t.calls.attach === 0,
         "waehrend einer Aufzeichnung wird nicht gelauscht, und das steht im Feld",
         t.element("listen-result").textContent);

  t = build({ linked: true, frames: 0 });
  await t.element("listen-start").tap();
  verify(t.calls.attach === 0 && /Nichts angekommen/.test(t.element("listen-result").textContent),
         "ist der Dongle schon verbunden, wird nicht erneut verbunden; ein stiller Bus wird benannt");

  console.log("\nDongle verbinden in den Einstellungen");
  t = build({ linked: false });
  await t.element("settings-connect").tap();
  verify(t.calls.attach === 1
         && /Verbunden: IOS-Vlink/.test(t.element("settings-connect-status").textContent),
         "der Knopf verbindet und nennt das Geraet", t.element("settings-connect-status").textContent);
  verify(t.calls.listen.length === 0, "es wird dabei nichts gelauscht oder gelesen");
  t = build({ linked: false, connectionFails: true });
  await t.element("settings-connect").tap();
  verify(/Nicht verbunden: kein Gerät gewählt/.test(t.element("settings-connect-status").textContent),
         "scheitert es, steht der Grund neben dem Knopf");

  console.log(failure ? `\n${failure} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
  process.exit(failure ? 1 : 0);
}

main().catch((x) => { console.log("Abbruch:", x); process.exit(1); });
