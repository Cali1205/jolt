#!/usr/bin/env node
// Prueft den Standort-Weg aus frontend/live.js: im Browser ueber
// navigator.geolocation, in der iOS-App ueber das Hintergrund-Plugin.
//
// Was sich ohne Geraet pruefen laesst, wird hier geprueft: dass das Plugin mit
// der Option angelegt wird, die den Hintergrund erst moeglich macht, dass die
// Messpunkte mit der Zeit des Fixes (nicht des Eintreffens) hinausgehen, dass
// Fehler und Abbruch sauber aufgeraeumt werden. Ob iOS die App bei gesperrtem
// Bildschirm wirklich weiterlaufen laesst, zeigt nur eine Fahrt.
//
//     node tools/check_standort.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const quelle = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "live.js"), "utf8");

let fehler = 0;
function pruefe(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) fehler++;
}

const speicher = {};
const meldungen = [];
const posts = [];
const K0 = {
  zustand: { sitzungId: 7 },
  melden: (t, art) => meldungen.push({ t, art }),
  an() {}, reglerKoppeln() {}, sitzungMerken() {}, gemerkteSitzung: () => null,
  api: async (pfad, opt) => {
    posts.push({ pfad, body: opt && opt.body });
    return { typ: "zustand" };
  },
};
const leer = () => new Proxy(function () { return ""; }, {
  get: (z, n) => (n === Symbol.toPrimitive ? () => "" : n === "style" ? {} : leer()),
  apply: () => leer(), set: () => true });
const K = new Proxy(K0, { get: (z, n) => (n in z ? z[n] : leer()) });

let webWacheAngelegt = 0;
let webRueckruf = null;
const fenster = { jolt: K, addEventListener() {}, joltBlePlugin: undefined };
const kontext = {
  window: fenster, console, setTimeout, clearTimeout, Date, JSON, Math, Promise,
  document: { addEventListener() {}, getElementById: () => leer(),
              querySelector: () => leer(), querySelectorAll: () => [],
              createElement: () => leer(), body: { appendChild() {} },
              visibilityState: "visible" },
  navigator: { geolocation: {
    watchPosition: (f) => { webWacheAngelegt++; webRueckruf = f; return 1; },
    clearWatch() {} } },
  localStorage: { getItem: (k) => (k in speicher ? speicher[k] : null),
                  setItem: (k, v) => { speicher[k] = v; },
                  removeItem: (k) => { delete speicher[k]; } },
  WebSocket: function () {},
};
vm.createContext(kontext);
vm.runInContext(quelle, kontext);
const live = fenster.joltLive;
const warte = (ms) => new Promise((r) => setTimeout(r, ms));

// Die Meldesperre (12 s Wanduhr) laeuft sonst im Test mit.
let jetzt = Date.now();
const echtesNow = Date.now;
Date.now = () => (jetzt += 13000);

/* Ein nachgebautes Plugin: haelt fest, womit es angelegt und entfernt wurde. */
function plugin({ verzoegerungMs = 0, ablehnen = false, verfuegbar = true } = {}) {
  const p = { angelegt: [], entfernt: [], rueckruf: null, wachhalten: 0 };
  p.BackgroundGeolocation = {
    addWatcher: async (optionen, rueckruf) => {
      p.angelegt.push(optionen);
      p.rueckruf = rueckruf;
      if (verzoegerungMs) await warte(verzoegerungMs);
      if (ablehnen) throw new Error("kaputt");
      return "wacher-" + p.angelegt.length;
    },
    removeWatcher: async (o) => { p.entfernt.push(o.id); },
  };
  p.Capacitor = { isNativePlatform: () => true,
                  isPluginAvailable: (n) => verfuegbar && n === "BackgroundGeolocation" };
  p.KeepAwake = { keepAwake: async () => { p.wachhalten++; },
                  allowSleep: async () => {} };
  return p;
}

(async () => {
  console.log("\nIm Browser");
  fenster.joltBlePlugin = undefined;
  live.positionVerfolgen();
  pruefe(webWacheAngelegt === 1, "ohne Plugin gilt navigator.geolocation");
  await live.beenden();

  console.log("\nIn der App");
  const p = plugin();
  fenster.joltBlePlugin = p;
  K0.zustand.sitzungId = 7;
  webWacheAngelegt = 0;
  live.positionVerfolgen();
  await warte(10);
  pruefe(webWacheAngelegt === 0,
         "mit dem Plugin wird der Browser-Standort nicht angefasst - er läuft " +
         "bei gesperrtem Telefon ohnehin nicht");
  pruefe(p.angelegt.length === 1, "der Watcher wird genau einmal angelegt");
  const o = p.angelegt[0] || {};
  pruefe(typeof o.backgroundMessage === "string" && o.backgroundMessage.length > 0,
         "mit backgroundMessage - ohne sie liefert das Plugin nur im " +
         "Vordergrund, und alles andere wäre umsonst",
         JSON.stringify(o));
  pruefe(o.requestPermissions === true, "und fragt selbst nach der Erlaubnis");
  pruefe(p.wachhalten >= 1, "der Bildschirm-Wachhalter läuft mit");
  live.positionVerfolgen();
  await warte(10);
  pruefe(p.angelegt.length === 1,
         "ein zweiter Aufruf legt keinen zweiten Watcher an");

  console.log("\nEin Fix kommt an");
  const fixZeit = Date.now() - 5 * 60 * 1000;
  p.rueckruf({ latitude: 52.5, longitude: 13.4, altitude: 40, speed: 27.5,
               time: fixZeit, accuracy: 5 });
  await warte(30);
  const punkt = posts.filter((x) => /\/punkte$/.test(x.pfad)).pop();
  const mp = punkt && punkt.body.punkte[punkt.body.punkte.length - 1];
  pruefe(!!mp && mp.lat === 52.5 && mp.lon === 13.4,
         "Position geht an den Server", JSON.stringify(punkt));
  pruefe(!!mp && Math.abs(mp.tempo_kmh - 99) < 0.01,
         "Geschwindigkeit in km/h statt m/s", String(mp && mp.tempo_kmh));
  pruefe(!!mp && new Date(mp.zeit).getTime() === fixZeit,
         "mit der Zeit des Fixes, nicht des Eintreffens - sonst wäre ein " +
         "nachgereichter Punkt auf die falsche Sekunde datiert",
         String(mp && mp.zeit));
  p.rueckruf({ latitude: 52.51, longitude: 13.41, altitude: null, speed: null,
               time: Date.now() });
  await warte(30);
  const ohneTempo = posts.filter((x) => /\/punkte$/.test(x.pfad)).pop().body.punkte.pop();
  pruefe(ohneTempo.tempo_kmh === null,
         "fehlt die Geschwindigkeit, bleibt sie leer statt 0 oder NaN");

  console.log("\nFehler");
  p.rueckruf(undefined, Object.assign(new Error("x"), { code: "NOT_AUTHORIZED" }));
  p.rueckruf(undefined, Object.assign(new Error("x"), { code: "NOT_AUTHORIZED" }));
  const warnungen = meldungen.filter((m) => /Standort nicht erlaubt/.test(m.t));
  pruefe(warnungen.length === 1,
         "fehlende Erlaubnis wird einmal gemeldet, nicht bei jedem Rückruf",
         String(warnungen.length));
  pruefe(warnungen[0] && /Immer|Verwenden/.test(warnungen[0].t),
         "und sagt, was einzustellen ist");
  const vorher = meldungen.length;
  p.rueckruf(undefined, Object.assign(new Error("Tunnel"), { code: "UNAVAILABLE" }));
  pruefe(meldungen.length === vorher,
         "ein anderer Fehler bleibt still - Tunnel gibt es",
         meldungen.slice(vorher).map((m) => m.t).join(" | "));

  console.log("\nBeenden");
  await live.beenden();
  await warte(10);
  pruefe(p.entfernt.length === 1 && p.entfernt[0] === "wacher-1",
         "der Watcher wird entfernt - sonst läuft die Ortung ohne Fahrt " +
         "weiter und kostet Akku", JSON.stringify(p.entfernt));

  console.log("\nBeenden, während iOS noch nach der Erlaubnis fragt");
  const spaet = plugin({ verzoegerungMs: 80 });
  fenster.joltBlePlugin = spaet;
  K0.zustand.sitzungId = 8;
  live.positionVerfolgen();
  await warte(10);
  await live.beenden();
  await warte(150);
  pruefe(spaet.entfernt.length === 1,
         "der erst danach eintreffende Watcher wird sofort wieder entfernt",
         JSON.stringify(spaet.entfernt));

  console.log("\nPlugin schlägt beim Anlegen fehl");
  const kaputt = plugin({ ablehnen: true });
  fenster.joltBlePlugin = kaputt;
  K0.zustand.sitzungId = 9;
  webWacheAngelegt = 0;
  live.positionVerfolgen();
  await warte(30);
  pruefe(webWacheAngelegt === 1,
         "dann gilt der Standort im Vordergrund - besser als gar keiner für " +
         "den Rest der Fahrt", String(webWacheAngelegt));
  live.positionVerfolgen();
  await warte(10);
  pruefe(webWacheAngelegt === 1 && kaputt.angelegt.length === 1,
         "und es gibt keinen zweiten Watcher daneben");
  await live.beenden();

  console.log("\nÄltere App ohne das Plugin");
  const alt = plugin({ verfuegbar: false });
  fenster.joltBlePlugin = alt;
  K0.zustand.sitzungId = 10;
  webWacheAngelegt = 0;
  live.positionVerfolgen();
  await warte(30);
  pruefe(alt.angelegt.length === 0 && webWacheAngelegt === 1,
         "ist die native Klasse nicht eingebaut, wird sie gar nicht erst " +
         "gerufen - der Server liefert die neue Oberfläche an eine App, die " +
         "älter sein kann als sie", `${alt.angelegt.length} / ${webWacheAngelegt}`);
  await live.beenden();

  Date.now = echtesNow;
  console.log(fehler ? `\n${fehler} Prüfung(en) fehlgeschlagen.`
                     : "\nAlle Prüfungen bestanden.");
  process.exit(fehler ? 1 : 0);
})();
