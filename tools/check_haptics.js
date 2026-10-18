#!/usr/bin/env node
// Checks the haptic feedback (frontend/haptics.js): silent without the plugin,
// the right call for each outcome with it, throttled, and never an error.
//
//     node tools/check_haptics.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

let failure = 0;
function verify(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) failure++;
}

function build(shell, clock) {
  const timeframe = { joltBlePlugin: shell };
  let now = clock || 1000000;
  class FakeDate extends Date { static now() { return now; } }
  const context = vm.createContext({ window: timeframe, Date: FakeDate });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "frontend", "haptics.js"), "utf8"), context);
  return { h: timeframe.joltHaptic, advance: (ms) => { now += ms; } };
}

function plugin(options) {
  const calls = [];
  const o = options || {};
  return {
    calls,
    Capacitor: { isNativePlatform: () => o.native !== false },
    Haptics: {
      impact: async (a) => { calls.push(["impact", a.style]); if (o.fails) throw new Error("kaputt"); },
      notification: async (a) => { calls.push(["notification", a.type]); },
    },
  };
}

(async () => {
  let t = build(undefined);
  t.h.tap(); t.h.good(); t.h.report("fehler");
  verify(true, "im Browser (ohne Plugin) passiert nichts und nichts bricht");

  const web = plugin({ native: false });
  t = build(web);
  t.h.tap(); t.h.good();
  verify(web.calls.length === 0, "auch mit Plugin-Objekt, aber nicht in der nativen App: nichts");

  const app = plugin();
  t = build(app);
  t.h.tap();
  verify(app.calls.join("|") === "impact,LIGHT", "tap: leichter Anstoss", app.calls.join("|"));
  t.advance(1000); t.h.good();
  verify(app.calls[1].join() === "notification,SUCCESS", "good: Erfolg");
  t.advance(1000); t.h.warn();
  verify(app.calls[2].join() === "notification,WARNING", "warn: Warnung");
  t.advance(1000); t.h.bad();
  verify(app.calls[3].join() === "notification,ERROR", "bad: Fehler");

  const burst = plugin();
  t = build(burst);
  t.h.warn(); t.h.warn(); t.h.bad(); t.h.tap();
  verify(burst.calls.length === 1, "ein Schwall von Meldungen wird gedrosselt: nur das erste kommt an", String(burst.calls.length));
  t.h.good();
  verify(burst.calls.length === 2, "ein Erfolg (Aufzeichnung gestartet) geht immer durch, auch direkt danach");

  const reports = plugin();
  t = build(reports);
  t.h.report("hinweis");
  verify(reports.calls.length === 0, "ein Hinweis macht kein Gefuehl");
  t.h.report("fehler");
  verify(reports.calls[0].join() === "notification,ERROR", "ein Fehler schon");
  t.advance(1000); t.h.report("warnung");
  verify(reports.calls[1].join() === "notification,WARNING", "eine Warnung auch");

  const broken = plugin({ fails: true });
  t = build(broken);
  let threw = false;
  try { t.h.tap(); await new Promise((r) => setTimeout(r, 10)); } catch (e) { threw = true; }
  verify(!threw, "schlaegt das Plugin fehl, bleibt das unbemerkt");

  if (failure) { console.log("\n" + failure + " Pruefung(en) fehlgeschlagen."); process.exit(1); }
  console.log("\nAlle Pruefungen bestanden.");
})();
