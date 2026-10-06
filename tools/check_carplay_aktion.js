#!/usr/bin/env node
// Prueft den Start und das Beenden der Aufzeichnung aus CarPlay
// (frontend/fahrten.js: carplayAktion).
//
// Die CarPlay-Liste sendet ein Ereignis ("carplayAktion": starten oder
// beenden) und bekommt das Ergebnis zurueck. Im Auto sieht niemand die
// Meldungen der Oberflaeche - ein Start, der still scheitert, waere dort ein
// Knopf, der nichts tut. Geprueft wird deshalb vor allem: Kommt immer eine
// Antwort, und nennt sie bei einem Fehler den Grund?
//
//     node tools/check_carplay_aktion.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const quelle = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "fahrten.js"), "utf8");

let fehler = 0;
function pruefe(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) fehler++;
}

function bauen(o) {
  o = o || {};
  const elemente = {
    "aufz-fahrzeug": { value: o.fahrzeug === undefined ? "1" : o.fahrzeug,
                       selectedOptions: [{ textContent: " ID.Buzz " }] },
    "aufz-name": { value: "" },
    "aufz-start": { disabled: !!o.startLaeuft, textContent: "" },
    "aufz-stand": { textContent: "" },
    "live-leer": { hidden: false }, "live-inhalt": { hidden: true },
  };
  const leer = () => new Proxy(function () { return ""; }, {
    get: (z, n) => (n === Symbol.toPrimitive ? () => "" : n === "style" ? {} : leer()),
    apply: () => leer(), set: () => true });

  const aufrufe = { api: [], listener: {}, bereit: [], ergebnis: [], beenden: 0, gespeichert: {} };
  const K = {
    zustand: { sitzungId: o.laeuft ? 7 : null, fahrzeuge: [{ id: 1 }], fahrtenVeraltet: false },
    melden() {},
    an(id, ereignis, f) { return (elemente[id] || leer()); },
    api: async (pfad, opt) => { aufrufe.api.push({ pfad, body: opt && opt.body });
                                return { sitzung_id: 9, fahrt_id: 3 }; },
    sitzungMerken() {},
  };
  const plugin = {
    addListener: async (name, f) => { aufrufe.listener[name] = f; return { remove() {} }; },
    bereit: async (a) => { aufrufe.bereit.push(a); },
    aktionErgebnis: async (a) => { aufrufe.ergebnis.push(a); },
  };
  const fenster = {
    jolt: K,
    joltBlePlugin: o.browser ? undefined
      : { JoltAnzeige: plugin, Capacitor: { isNativePlatform: () => true } },
    joltApp: { ansichtZeigen() {} },
    joltObd: { verfuegbar: () => false },
    joltLive: {
      verbinden: () => { K.zustand.sitzungId = 9; },
      positionVerfolgen() {}, fahrzustandStart() {}, dongleNutzen() {},
      dongleWiederverbinden() {}, handshakeSicher: async () => false,
      beenden: async () => { aufrufe.beenden++; if (!o.beendenKlemmt) K.zustand.sitzungId = null; },
    },
  };
  const kontext = {
    window: fenster, console: { log() {} }, setTimeout, Promise, Date, JSON, Math, Number, String,
    document: { getElementById: (id) => elemente[id] || leer(),
                querySelector: () => leer(), createElement: () => leer(),
                addEventListener() {} },
    navigator: { geolocation: {
      getCurrentPosition: (ok, nein) => {
        if (o.keinStandort) nein({ message: "Standort nicht erlaubt" });
        else ok({ coords: { latitude: 48.47, longitude: 9.14 } });
      } } },
    localStorage: { getItem: () => null, setItem: (k, v) => { aufrufe.gespeichert[k] = v; },
                    removeItem() {} },
  };
  vm.createContext(kontext);
  vm.runInContext(quelle, kontext);
  fenster.joltFahrten.einrichten();
  return { f: fenster, aufrufe, K, elemente };
}

(async () => {
  console.log("Anbindung");
  let t = bauen();
  pruefe(typeof t.aufrufe.listener.carplayAktion === "function",
         "in der iOS-App haengt sich fahrten.js an das Ereignis 'carplayAktion'");
  pruefe(t.aufrufe.bereit.length === 1 && t.aufrufe.bereit[0].fahrzeug === "ID.Buzz",
         "und meldet CarPlay das gewaehlte Fahrzeug (ohne Leerzeichen am Rand)",
         JSON.stringify(t.aufrufe.bereit));
  t = bauen({ fahrzeug: "" });
  pruefe(t.aufrufe.bereit[0] && t.aufrufe.bereit[0].fahrzeug === "",
         "ohne gewaehltes Fahrzeug meldet es einen leeren Namen - CarPlay zeigt dann keinen erfundenen");
  t = bauen({ browser: true });
  pruefe(Object.keys(t.aufrufe.listener).length === 0,
         "im Browser (ohne Plugin) passiert nichts, und nichts bricht");

  console.log("\nStarten");
  t = bauen();
  await t.aufrufe.listener.carplayAktion({ aktion: "starten" });
  pruefe(t.aufrufe.api.length === 1 && t.aufrufe.api[0].pfad === "/api/live/aufzeichnung"
         && t.aufrufe.api[0].body.fahrzeug_id === 1,
         "legt die Aufzeichnung mit dem gewaehlten (zuletzt benutzten) Fahrzeug an",
         JSON.stringify(t.aufrufe.api));
  pruefe(t.aufrufe.ergebnis.length === 1 && t.aufrufe.ergebnis[0].ok === true
         && t.aufrufe.ergebnis[0].aktion === "starten",
         "und meldet CarPlay den Erfolg", JSON.stringify(t.aufrufe.ergebnis));
  pruefe(t.aufrufe.gespeichert["jolt-aufz-fahrzeug"] === "1",
         "das Fahrzeug wird als zuletzt benutzt gemerkt");

  t = bauen({ laeuft: true });
  await t.aufrufe.listener.carplayAktion({ aktion: "starten" });
  pruefe(t.aufrufe.api.length === 0 && t.aufrufe.ergebnis[0].ok === true
         && /läuft schon/.test(t.aufrufe.ergebnis[0].text),
         "laeuft schon eine Aufzeichnung, wird keine zweite angelegt - und das wird gesagt",
         JSON.stringify(t.aufrufe));

  t = bauen({ startLaeuft: true });
  await t.aufrufe.listener.carplayAktion({ aktion: "starten" });
  pruefe(t.aufrufe.api.length === 0 && t.aufrufe.ergebnis[0].ok === false
         && /Start läuft/.test(t.aufrufe.ergebnis[0].text),
         "ein Start, der gerade laeuft, wird nicht doppelt ausgeloest");

  console.log("\nStarten scheitert - CarPlay erfaehrt den Grund");
  t = bauen({ keinStandort: true });
  await t.aufrufe.listener.carplayAktion({ aktion: "starten" });
  pruefe(t.aufrufe.ergebnis[0].ok === false && /Standort/.test(t.aufrufe.ergebnis[0].text)
         && t.aufrufe.api.length === 0,
         "kein Standort: keine Aufzeichnung, und der Grund steht im Ergebnis",
         JSON.stringify(t.aufrufe.ergebnis));
  t = bauen({ fahrzeug: "" });
  await t.aufrufe.listener.carplayAktion({ aktion: "starten" });
  pruefe(t.aufrufe.ergebnis[0].ok === false && /Fahrzeug/.test(t.aufrufe.ergebnis[0].text)
         && t.aufrufe.api.length === 0,
         "kein Fahrzeug gewaehlt: wird nicht geraten, und CarPlay sagt es",
         JSON.stringify(t.aufrufe.ergebnis));

  console.log("\nBeenden");
  t = bauen({ laeuft: true });
  await t.aufrufe.listener.carplayAktion({ aktion: "beenden" });
  pruefe(t.aufrufe.beenden === 1 && t.aufrufe.ergebnis[0].ok === true
         && t.aufrufe.ergebnis[0].aktion === "beenden",
         "beendet die laufende Aufzeichnung und meldet es", JSON.stringify(t.aufrufe.ergebnis));
  t = bauen({ laeuft: true, beendenKlemmt: true });
  await t.aufrufe.listener.carplayAktion({ aktion: "beenden" });
  pruefe(t.aufrufe.ergebnis[0].ok === false && /Beenden/.test(t.aufrufe.ergebnis[0].text),
         "geht das Beenden schief, sagt CarPlay es - statt eine laufende Fahrt fuer beendet zu halten");
  t = bauen();
  await t.aufrufe.listener.carplayAktion({ aktion: "beenden" });
  pruefe(t.aufrufe.beenden === 0 && t.aufrufe.ergebnis[0].ok === true,
         "ohne laufende Aufzeichnung gibt es nichts zu beenden");

  console.log("\nUnsinn");
  t = bauen();
  await t.aufrufe.listener.carplayAktion({ aktion: "loeschen" });
  await t.aufrufe.listener.carplayAktion(undefined);
  pruefe(t.aufrufe.api.length === 0 && t.aufrufe.ergebnis.length === 0 && t.aufrufe.beenden === 0,
         "eine unbekannte Aktion tut nichts");

  console.log(fehler ? `\n${fehler} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
  process.exit(fehler ? 1 : 0);
})();
