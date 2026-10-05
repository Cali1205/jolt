#!/usr/bin/env node
// Prueft den Start einer Aufzeichnung (frontend/fahrten.js) gegen einen
// Dongle, der nicht antwortet.
//
// Der Fall, um den es geht: Das Auto schlaeft noch, der Dongle ist verbunden,
// die erste Abfrage (Startladestand) laeuft in den Zeitablauf. Frueher galt
// der Dongle dann fuer die ganze Aufzeichnung als nicht vorhanden - am
// 5.10. lieferte eine Fahrt drei Minuten lang nur GPS, bis jemand von Hand
// neu gestartet hat.
//
//     node tools/check_aufzeichnung.js
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

/* Ein Durchlauf mit einem Dongle, der sich so verhaelt, wie `szenario` sagt. */
async function lauf(szenario) {
  const meldungen = [];
  const aufrufe = { dongleNutzen: 0, verbinden: 0, positionVerfolgen: 0 };
  const gesendet = [];
  const elemente = {
    "aufz-start": { disabled: false },
    "aufz-stand": { textContent: "" },
    "aufz-fahrzeug": { value: "1" },
    "aufz-name": { value: "Testfahrt" },
    "live-leer": { hidden: false },
    "live-inhalt": { hidden: true },
  };
  const leer = () => new Proxy(function () { return ""; }, {
    get: (z, n) => (n === Symbol.toPrimitive ? () => "" : n === "style" ? {} : leer()),
    apply: () => leer(), set: () => true });

  let verbunden = true;
  const obd = {
    verfuegbar: () => true,
    einrichten() {},
    anschliessen: async () => { verbunden = szenario.verbundenNachAnschliessen !== false; },
    verbunden: () => verbunden,
    handshake: async () => true,
    befehl: async () => {
      if (szenario.antwort) return szenario.antwort;
      if (szenario.verbindungWeg) verbunden = false;
      throw new Error("Zeitüberschreitung bei 22028C");
    },
    socAusAntwort: () => ({ hmi: 71.7 }),
  };
  const K = {
    zustand: { sitzungId: null, fahrzeuge: [{ id: 1 }], fahrtenVeraltet: false },
    melden: (t) => meldungen.push(t),
    api: async (pfad, opt) => { gesendet.push({ pfad, body: opt && opt.body });
                                return { sitzung_id: 9 }; },
    sitzungMerken() {},
  };
  const fenster = {
    jolt: K, joltObd: obd,
    joltApp: { ansichtZeigen() {} },
    joltLive: {
      verbinden: () => { aufrufe.verbinden++; },
      positionVerfolgen: () => { aufrufe.positionVerfolgen++; },
      dongleNutzen: () => { aufrufe.dongleNutzen++; },
    },
  };
  const kontext = {
    window: fenster, console: { log() {} }, setTimeout, Promise, Date, JSON, Math, Number,
    document: { getElementById: (id) => elemente[id] || leer(),
                querySelector: () => leer(), createElement: () => leer(),
                addEventListener() {} },
    navigator: { geolocation: {
      getCurrentPosition: (ok) => ok({ coords: { latitude: 48.47, longitude: 9.14 } }) } },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  };
  vm.createContext(kontext);
  vm.runInContext(quelle, kontext);
  await fenster.joltFahrten.aufzeichnungStarten();
  return { aufrufe, gesendet, meldungen, stand: elemente["aufz-stand"].textContent,
           knopf: elemente["aufz-start"] };
}

(async () => {
  console.log("\nDas Auto antwortet auf die erste Abfrage");
  let r = await lauf({ antwort: "62028C B5" });
  pruefe(r.aufrufe.dongleNutzen === 1, "der Dongle wird benutzt");
  pruefe(r.gesendet.length === 1 && r.gesendet[0].body.soc === 71.7,
         "und der Startladestand geht mit", JSON.stringify(r.gesendet));
  pruefe(/Ladestand kommt aus dem Auto/.test(r.meldungen.join(" ")),
         "die Meldung sagt es", r.meldungen.join(" | "));

  console.log("\nDas Auto schläft noch - keine Antwort, Dongle verbunden");
  r = await lauf({});
  pruefe(r.aufrufe.dongleNutzen === 1,
         "der Dongle bleibt in Benutzung - sonst läuft die ganze Aufzeichnung " +
         "ohne Fahrzeugwerte, wie am 5.10. in Sitzung 6", String(r.aufrufe.dongleNutzen));
  pruefe(r.gesendet.length === 1 && r.gesendet[0].body.soc === null,
         "ohne Startladestand - der wird nicht erfunden",
         JSON.stringify(r.gesendet && r.gesendet[0] && r.gesendet[0].body));
  pruefe(/antwortet noch nicht/.test(r.meldungen.join(" ")),
         "und die Meldung sagt, dass es später kommt - statt \"Ladestand von Hand\"",
         r.meldungen.join(" | "));
  pruefe(r.aufrufe.verbinden === 1 && r.aufrufe.positionVerfolgen === 1,
         "die Fahrt läuft an");

  console.log("\nDie Verbindung ist weg");
  r = await lauf({ verbindungWeg: true });
  pruefe(r.aufrufe.dongleNutzen === 0,
         "ist der Dongle wirklich nicht mehr da, gilt er als nicht vorhanden",
         String(r.aufrufe.dongleNutzen));
  pruefe(/Ladestand unterwegs/.test(r.meldungen.join(" ")),
         "und der Ladestand kommt von Hand", r.meldungen.join(" | "));

  console.log("\nKein Dongle");
  r = await lauf({ verbundenNachAnschliessen: false });
  pruefe(r.aufrufe.dongleNutzen === 0 && r.gesendet.length === 1,
         "ohne Verbindung läuft die Aufzeichnung ohne Dongle weiter");

  console.log(fehler ? `\n${fehler} Prüfung(en) fehlgeschlagen.`
                     : "\nAlle Prüfungen bestanden.");
  process.exit(fehler ? 1 : 0);
})();
