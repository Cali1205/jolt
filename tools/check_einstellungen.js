#!/usr/bin/env node
/* Prueft die Dongle-Knoepfe der Einstellungen: Verbinden und Mithoeren.
 *
 * Auslöser: Der Mithoer-Knopf tat im Auto scheinbar nichts. Nach dem Parken
 * ist der Dongle getrennt, die Einstellungen hatten keinen Weg, ihn zu
 * verbinden, und der Knopf meldete "Kein Dongle verbunden" als Hinweis, der
 * nach sechs Sekunden verschwand. Geprueft wird deshalb der ganze Weg
 * vom Tippen bis zum Text im Ergebnisfeld - gegen einen nachgebildeten
 * Dongle-Baustein, ohne Browser.
 *
 *     node tools/check_einstellungen.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const quelle = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "einstellungen.js"), "utf8");

let fehler = 0;
function pruefe(ok, text, zusatz) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "   " + (zusatz || "")));
  if (!ok) fehler += 1;
}

function bauen(optionen) {
  const o = optionen || {};
  const elemente = {};
  const element = (id) => {
    if (!elemente[id]) {
      elemente[id] = { id, hidden: false, disabled: false, textContent: "", value: "",
                       checked: false, innerHTML: "", style: {}, klick: null,
                       addEventListener(n, f) { if (n === "click") this.klick = f; },
                       dispatchEvent() {}, click() {} };
    }
    return elemente[id];
  };
  element("lausch-protokoll").value = "6";
  element("lausch-dauer").value = "10000";

  const meldungen = [];
  const aufrufe = { anschliessen: 0, lauschen: [], trennen: 0 };
  let verbunden = !!o.verbunden;
  const O = {
    verfuegbar: () => o.bluetooth !== false,
    verbunden: () => verbunden,
    anschliessen: async () => {
      aufrufe.anschliessen += 1;
      if (o.verbindungScheitert) throw new Error("kein Gerät gewählt");
      verbunden = !o.bleibtGetrennt;
    },
    lauschen: async (opt) => {
      aufrufe.lauschen.push(opt);
      if (!verbunden) throw new Error("nicht verbunden");
      return { protokoll: opt.protokoll, dauer_ms: opt.dauer_ms, gesamt: o.frames || 0,
               ids: (o.frames ? [{ id: "7B0", n: o.frames, proSek: 5, varianten: 2,
                                   letzte: "06 62 02 8C B4 00 00" }] : []),
               proben: [], hinweise: [] };
    },
    diagnose: () => ({ verbunden, verbindung: { geraet: "IOS-Vlink" }, befehle: {}, messwerte: {},
                       runden: {}, wechselGescheitert: [], tabellenFehler: [] }),
    protokoll: () => [], FELDER: [],
    trennen() { aufrufe.trennen += 1; verbunden = false; },
    konsole: async () => "OK", vergessen() {},
    protokollLeeren() {}, zaehlerZuruecksetzen() {},
  };
  const K = {
    zustand: { sitzungId: o.fahrtLaeuft ? 7 : null },
    melden: (t, art) => meldungen.push({ t, art }),
    an: (id, ereignis, f) => { element(id).addEventListener(ereignis, f); return element(id); },
    api: async () => ({}), zahl: (x) => String(x),
  };
  const fenster = {
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
  fenster.window = fenster;
  vm.createContext(fenster);
  vm.runInContext(quelle, fenster);
  fenster.joltEinstellungen.einrichten();
  return { element, meldungen, aufrufe, f: fenster, setzeVerbunden: (v) => { verbunden = v; } };
}

async function main() {
  console.log("Mithoeren ohne verbundenen Dongle");
  let t = bauen({ verbunden: false, frames: 120 });
  await t.element("lausch-start").klick();
  pruefe(t.aufrufe.anschliessen === 1 && t.aufrufe.lauschen.length === 1,
         "der Knopf verbindet zuerst und hoert dann zu (nach dem Parken ist der Dongle getrennt)",
         JSON.stringify(t.aufrufe));
  pruefe(t.aufrufe.lauschen[0].protokoll === "6" && t.aufrufe.lauschen[0].dauer_ms === 10000,
         "mit dem gewaehlten Protokoll und der Dauer");
  const ergebnis = t.element("lausch-ergebnis");
  pruefe(ergebnis.hidden === false && /120 Frames/.test(ergebnis.textContent)
         && /7B0/.test(ergebnis.textContent),
         "das Ergebnis steht im Feld, mit Kennung und Anzahl", ergebnis.textContent);
  pruefe(t.element("lausch-kopieren").hidden === false, "und der Kopieren-Knopf erscheint");
  pruefe(t.element("lausch-start").disabled === false, "der Knopf ist danach wieder frei");

  console.log("\nMithoeren: Rueckmeldungen stehen im Feld, nicht in einer Meldung");
  t = bauen({ verbunden: false, verbindungScheitert: true });
  await t.element("lausch-start").klick();
  pruefe(/kein Gerät gewählt/.test(t.element("lausch-ergebnis").textContent)
         && t.aufrufe.lauschen.length === 0,
         "scheitert das Verbinden, steht der Grund im Ergebnisfeld - und es wird nicht gelauscht",
         t.element("lausch-ergebnis").textContent);
  pruefe(t.meldungen.length === 0,
         "ohne fluechtige Meldung, die nach sechs Sekunden weg ist");
  pruefe(t.element("lausch-start").disabled === false, "der Knopf bleibt benutzbar");

  t = bauen({ verbunden: false, bleibtGetrennt: true });
  await t.element("lausch-start").klick();
  pruefe(/keine Verbindung/.test(t.element("lausch-ergebnis").textContent)
         && t.aufrufe.lauschen.length === 0,
         "kommt trotz Dialog keine Verbindung zustande, sagt das Feld es");

  t = bauen({ bluetooth: false });
  await t.element("lausch-start").klick();
  pruefe(/kein Bluetooth/.test(t.element("lausch-ergebnis").textContent),
         "ein Browser ohne Bluetooth bekommt einen klaren Satz",
         t.element("lausch-ergebnis").textContent);

  t = bauen({ verbunden: true, fahrtLaeuft: true });
  await t.element("lausch-start").klick();
  pruefe(/Fahrt läuft/.test(t.element("lausch-ergebnis").textContent)
         && t.aufrufe.lauschen.length === 0 && t.aufrufe.anschliessen === 0,
         "waehrend einer Aufzeichnung wird nicht gelauscht, und das steht im Feld",
         t.element("lausch-ergebnis").textContent);

  t = bauen({ verbunden: true, frames: 0 });
  await t.element("lausch-start").klick();
  pruefe(t.aufrufe.anschliessen === 0 && /Nichts angekommen/.test(t.element("lausch-ergebnis").textContent),
         "ist der Dongle schon verbunden, wird nicht erneut verbunden; ein stiller Bus wird benannt");

  console.log("\nDongle verbinden in den Einstellungen");
  t = bauen({ verbunden: false });
  await t.element("einst-verbinden").klick();
  pruefe(t.aufrufe.anschliessen === 1
         && /Verbunden: IOS-Vlink/.test(t.element("einst-verbinden-stand").textContent),
         "der Knopf verbindet und nennt das Geraet", t.element("einst-verbinden-stand").textContent);
  pruefe(t.aufrufe.lauschen.length === 0, "es wird dabei nichts gelauscht oder gelesen");
  t = bauen({ verbunden: false, verbindungScheitert: true });
  await t.element("einst-verbinden").klick();
  pruefe(/Nicht verbunden: kein Gerät gewählt/.test(t.element("einst-verbinden-stand").textContent),
         "scheitert es, steht der Grund neben dem Knopf");

  console.log(fehler ? `\n${fehler} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
  process.exit(fehler ? 1 : 0);
}

main().catch((x) => { console.log("Abbruch:", x); process.exit(1); });
