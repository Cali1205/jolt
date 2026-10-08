#!/usr/bin/env node
/* Prueft die gezeichneten CarPlay-Kacheln (frontend/tiles.js) ohne Browser.
 *
 * Gezeichnet wird in einen Aufzeichner statt auf eine Leinwand: Er merkt sich
 * jeden Textaufruf mit Schriftgroesse und Ausrichtung. Damit lassen sich die
 * Fehler finden, die man sonst erst im Auto sieht - ein Text, der ueber den
 * Rand ragt ("kWh/100" war in der ersten Fassung rechts abgeschnitten), eine
 * Kachel, die bei fehlendem Wert abstuerzt, eine Farbe, die den falschen
 * Zustand meldet.
 *
 * Wie es *aussieht*, sagt das nicht - dafuer gibt es die Vorschau in den
 * Einstellungen.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

let failure = 0;
function verify(planApply, text, extra) {
  if (planApply) console.log("  ok    " + text);
  else { console.log("  FEHLT " + text + (extra ? "   " + extra : "")); failure += 1; }
}

/* Ein Zeichenkontext, der alles annimmt und Texte mitschreibt. Die Breite
 * eines Textes ist grob: 0,62 Schriftgroesse je Zeichen (Festbreite). Das
 * ist eher zu breit als zu schmal - die Pruefung bleibt auf der sicheren Seite. */
function recorder() {
  const texte = [];
  let typeface = 10, orientation = "left";
  const c = {
    texte,
    get font() { return `${typeface}px`; },
    set font(f) { const m = /(\d+(?:\.\d+)?)px/.exec(f); if (m) typeface = parseFloat(m[1]); },
    set textAlign(a) { orientation = a; },
    measureText: (s) => ({ width: String(s).length * typeface * 0.62 }),
    fillText(s, x, y) {
      const b = String(s).length * typeface * 0.62;
      const left_side = orientation === "center" ? x - b / 2 : (orientation === "right" ? x - b : x);
      texte.push({ s: String(s), left_side, right: left_side + b, y, typeface });
    },
    createLinearGradient: () => ({ addColorStop() {} }),
    setLineDash() {}, save() {}, restore() {}, scale() {},
  };
  // Alles andere (Pfade, Fuellen, Linien) nimmt der Aufzeichner kommentarlos an.
  return new Proxy(c, {
    get(destination, name) { return name in destination ? destination[name] : () => {}; },
    set(destination, name, val) { destination[name] = val; return true; },
  });
}

function load() {
  const timeframe = {};
  timeframe.window = timeframe;
  timeframe.document = { createElement: () => ({ getContext: () => recorder(),
                                               toDataURL: () => "data:image/png;base64,QUJD" }) };
  const context = vm.createContext(timeframe);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "frontend", "tiles.js"), "utf8"),
                  context, { filename: "tiles.js" });
  return timeframe.joltTiles;
}

const K = load();
const PAGE = K.PAGE;

console.log("\nFarben");
verify(K.colorSoc(80) === K.colorSoc(35) && K.colorSoc(34) === K.colorSoc(20)
       && K.colorSoc(19) === K.colorSoc(0) && K.colorSoc(35) !== K.colorSoc(34)
       && K.colorSoc(20) !== K.colorSoc(19),
       "Ladestand: ab 35 % ein Zustand, 20 bis 34 ein zweiter, darunter ein dritter");
verify(K.colorArrival(-10) === K.colorArrival(2) && K.colorArrival(3) === K.colorArrival(15)
       && K.colorArrival(16) !== K.colorArrival(15) && K.colorArrival(2) !== K.colorArrival(3),
       "Ankunft: früher oder bis +2 min gut, bis +15 min Warnung, darüber schlecht");
verify(K.colorReserve(100) !== K.colorReserve(99) && K.colorReserve(40) !== K.colorReserve(39),
       "Reserve: Grenzen bei 100 und 40 km");

console.log("\nWerte aus dem Modell");
const p = K.probe();
const d = K.records(p.m, p.series_list);
verify(K.SLOTS.every((s) => d[s]), "mit dem Probemodell hat jeder der sieben Plätze Werte");
verify(d.soc.val === 68 && d.soc.series.length > 5, "der Ladestand trägt Wert und Reihe");
verify(d.stops.count === 2 && d.stops.upcoming === "41 km", "die Stopps tragen Zahl und nächste Entfernung");
const emptyD = K.records({ soc: null, arrival: null, reserve: null, history: null,
                        aux: null, stopList: null }, null);
verify(K.SLOTS.every((s) => !emptyD[s]), "ohne Werte im Modell bleibt jeder Platz leer - nichts wird erfunden");
verify(Object.keys(K.records(null, null)).length === 0, "ohne Modell gibt es keine Daten");
const part = K.records({ soc: { percent: 50, text: "50 %" }, aux: { kw: null } }, null);
verify(part.soc && !part.aux && !part.consumption, "Teilmodelle: nur was da ist, hat Daten");

console.log("\nZeichnen");
for (const look of ["a", "b"]) {
  for (const slot of K.SLOTS) {
    let ok = true;
    try {
      K.draw(look, slot, d[slot], recorder());
      K.draw(look, slot, null, recorder());       // leere Kachel
    } catch (e) { ok = false; console.log("   ", look, slot, e.message); }
    verify(ok, `Stil ${look}, ${slot}: zeichnet mit und ohne Wert ohne Fehler`);
  }
}

console.log("\nKein Text über den Rand");
// Die ungünstigsten Werte je Platz: lange Zahlen, Stunden, grosse Entfernungen.
const extrem = {
  soc: [{ val: 100 }, { val: 7.4, source: "gerechnet" }, { val: 0 }],
  arrival: [{ min: 5, text: "+5 min" }, { min: 125, text: "+2 h 05" }, { min: -125, text: "−2 h 05" },
            { min: 0, text: "nach Plan" }, { min: -45, text: "−45 min" }],
  reserve: [{ km: 999, text: "999 km" }, { km: 0.4, text: "gleich" }, { km: 1234, text: "1.234 km" }],
  consumption: [{ text: "123,4", bar: [10, null, 30, 200, 15, 0.5] }, { text: "–", bar: [null, null, 5, null, null, null] }],
  aux: [{ val: 12.5, text: "12,5", series: [1, 2, 3] }, { val: 0, text: "0,0", series: [0, 0, 0] }],
  regen: [{ val: 100, series: [1, 2] }, { val: 0, series: null }],
  stops: [{ count: 8, upcoming: "1.234 km", kms: [10, 100, 400, 800, 900, 1000, 1100, 1200] },
           { count: 1, upcoming: "gleich", kms: [0] }, { count: 12, upcoming: null, kms: [] }],
};
let rows = 0;
for (const look of ["a", "b"]) {
  for (const slot of K.SLOTS) {
    for (const vals of extrem[slot]) {
      const c = recorder();
      K.draw(look, slot, vals, c);
      for (const t of c.texte) {
        rows += 1;
        // Ein Rand von 4 Einheiten: Die Kachel hat selbst eine Rundung und einen Rahmen.
        if (t.left_side < 4 || t.right > PAGE - 4 || t.y > PAGE - 2 || t.y - t.typeface < 0) {
          verify(false, `Stil ${look}, ${slot}: "${t.s}" liegt ausserhalb`,
                 `x ${t.left_side.toFixed(0)}..${t.right.toFixed(0)}, y ${t.y}, ${t.typeface}px`);
        }
      }
    }
  }
}
verify(rows > 100, `${rows} Textstellen in allen Stilen und Grenzfällen geprüft, keine ragt über den Rand`);

console.log("\nLesbarkeit");
// Im Auto ist eine Kachel klein: Alles unter 11 Einheiten wäre dort kaum zu lesen.
let minor = [];
for (const look of ["a", "b"]) {
  for (const slot of K.SLOTS) {
    const c = recorder();
    K.draw(look, slot, d[slot], c);
    for (const t of c.texte) if (t.typeface < 11) minor.push(`${look}/${slot} "${t.s}" ${t.typeface}px`);
  }
}
verify(minor.length === 0, "keine Beschriftung kleiner als 11 Einheiten", minor.join("; "));

console.log("\nBilder");
const b = K.pictures("a", p.m, p.series_list);
verify(K.SLOTS.every((s) => b[s] === "QUJD"), "ein Bild je Platz, Base64 ohne Kopf");
const bEmpty = K.pictures("b", null, null);
verify(K.SLOTS.every((s) => typeof bEmpty[s] === "string"),
       "auch ohne Modell kommt für jeden Platz ein (leeres) Bild - feste Plätze");
verify(K.pictures("klassisch", p.m, p.series_list) === null && K.pictures("x", p.m, p.series_list) === null,
       "für 'klassisch' und unbekannte Stile gibt es keine Bilder (dann zeichnet Swift)");
verify(K.STYLES.map((s) => s.id).join() === "klassisch,a,b", "drei Stile zur Auswahl");

console.log(failure ? `\n${failure} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
process.exit(failure ? 1 : 0);
