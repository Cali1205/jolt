#!/usr/bin/env node
/* Prueft die gezeichneten CarPlay-Kacheln (frontend/kacheln.js) ohne Browser.
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

let fehler = 0;
function pruefe(sollGelten, text, zusatz) {
  if (sollGelten) console.log("  ok    " + text);
  else { console.log("  FEHLT " + text + (zusatz ? "   " + zusatz : "")); fehler += 1; }
}

/* Ein Zeichenkontext, der alles annimmt und Texte mitschreibt. Die Breite
 * eines Textes ist grob: 0,62 Schriftgroesse je Zeichen (Festbreite). Das
 * ist eher zu breit als zu schmal - die Pruefung bleibt auf der sicheren Seite. */
function aufzeichner() {
  const texte = [];
  let schrift = 10, ausrichtung = "left";
  const c = {
    texte,
    get font() { return `${schrift}px`; },
    set font(f) { const m = /(\d+(?:\.\d+)?)px/.exec(f); if (m) schrift = parseFloat(m[1]); },
    set textAlign(a) { ausrichtung = a; },
    measureText: (s) => ({ width: String(s).length * schrift * 0.62 }),
    fillText(s, x, y) {
      const b = String(s).length * schrift * 0.62;
      const links = ausrichtung === "center" ? x - b / 2 : (ausrichtung === "right" ? x - b : x);
      texte.push({ s: String(s), links, rechts: links + b, y, schrift });
    },
    createLinearGradient: () => ({ addColorStop() {} }),
    setLineDash() {}, save() {}, restore() {}, scale() {},
  };
  // Alles andere (Pfade, Fuellen, Linien) nimmt der Aufzeichner kommentarlos an.
  return new Proxy(c, {
    get(ziel, name) { return name in ziel ? ziel[name] : () => {}; },
    set(ziel, name, wert) { ziel[name] = wert; return true; },
  });
}

function laden() {
  const fenster = {};
  fenster.window = fenster;
  fenster.document = { createElement: () => ({ getContext: () => aufzeichner(),
                                               toDataURL: () => "data:image/png;base64,QUJD" }) };
  const kontext = vm.createContext(fenster);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "frontend", "kacheln.js"), "utf8"),
                  kontext, { filename: "kacheln.js" });
  return fenster.joltKacheln;
}

const K = laden();
const SEITE = K.SEITE;

console.log("\nFarben");
pruefe(K.farbeSoc(80) === K.farbeSoc(35) && K.farbeSoc(34) === K.farbeSoc(20)
       && K.farbeSoc(19) === K.farbeSoc(0) && K.farbeSoc(35) !== K.farbeSoc(34)
       && K.farbeSoc(20) !== K.farbeSoc(19),
       "Ladestand: ab 35 % ein Zustand, 20 bis 34 ein zweiter, darunter ein dritter");
pruefe(K.farbeAnkunft(-10) === K.farbeAnkunft(2) && K.farbeAnkunft(3) === K.farbeAnkunft(15)
       && K.farbeAnkunft(16) !== K.farbeAnkunft(15) && K.farbeAnkunft(2) !== K.farbeAnkunft(3),
       "Ankunft: früher oder bis +2 min gut, bis +15 min Warnung, darüber schlecht");
pruefe(K.farbeReserve(100) !== K.farbeReserve(99) && K.farbeReserve(40) !== K.farbeReserve(39),
       "Reserve: Grenzen bei 100 und 40 km");

console.log("\nWerte aus dem Modell");
const p = K.probe();
const d = K.daten(p.m, p.reihen);
pruefe(K.SLOTS.every((s) => d[s]), "mit dem Probemodell hat jeder der sieben Plätze Werte");
pruefe(d.soc.wert === 68 && d.soc.reihe.length > 5, "der Ladestand trägt Wert und Reihe");
pruefe(d.stopps.anzahl === 2 && d.stopps.naechster === "41 km", "die Stopps tragen Zahl und nächste Entfernung");
const leerD = K.daten({ soc: null, ankunft: null, reserve: null, verlauf: null,
                        neben: null, stoppListe: null }, null);
pruefe(K.SLOTS.every((s) => !leerD[s]), "ohne Werte im Modell bleibt jeder Platz leer - nichts wird erfunden");
pruefe(Object.keys(K.daten(null, null)).length === 0, "ohne Modell gibt es keine Daten");
const teil = K.daten({ soc: { prozent: 50, text: "50 %" }, neben: { kw: null } }, null);
pruefe(teil.soc && !teil.neben && !teil.verbrauch, "Teilmodelle: nur was da ist, hat Daten");

console.log("\nZeichnen");
for (const stil of ["a", "b"]) {
  for (const slot of K.SLOTS) {
    let ok = true;
    try {
      K.zeichnen(stil, slot, d[slot], aufzeichner());
      K.zeichnen(stil, slot, null, aufzeichner());       // leere Kachel
    } catch (e) { ok = false; console.log("   ", stil, slot, e.message); }
    pruefe(ok, `Stil ${stil}, ${slot}: zeichnet mit und ohne Wert ohne Fehler`);
  }
}

console.log("\nKein Text über den Rand");
// Die ungünstigsten Werte je Platz: lange Zahlen, Stunden, grosse Entfernungen.
const extrem = {
  soc: [{ wert: 100 }, { wert: 7.4, quelle: "gerechnet" }, { wert: 0 }],
  ankunft: [{ min: 5, text: "+5 min" }, { min: 125, text: "+2 h 05" }, { min: -125, text: "−2 h 05" },
            { min: 0, text: "nach Plan" }, { min: -45, text: "−45 min" }],
  reserve: [{ km: 999, text: "999 km" }, { km: 0.4, text: "gleich" }, { km: 1234, text: "1.234 km" }],
  verbrauch: [{ text: "123,4", balken: [10, null, 30, 200, 15, 0.5] }, { text: "–", balken: [null, null, 5, null, null, null] }],
  neben: [{ wert: 12.5, text: "12,5", reihe: [1, 2, 3] }, { wert: 0, text: "0,0", reihe: [0, 0, 0] }],
  rekup: [{ wert: 100, reihe: [1, 2] }, { wert: 0, reihe: null }],
  stopps: [{ anzahl: 8, naechster: "1.234 km", kms: [10, 100, 400, 800, 900, 1000, 1100, 1200] },
           { anzahl: 1, naechster: "gleich", kms: [0] }, { anzahl: 12, naechster: null, kms: [] }],
};
let zeilen = 0;
for (const stil of ["a", "b"]) {
  for (const slot of K.SLOTS) {
    for (const werte of extrem[slot]) {
      const c = aufzeichner();
      K.zeichnen(stil, slot, werte, c);
      for (const t of c.texte) {
        zeilen += 1;
        // Ein Rand von 4 Einheiten: Die Kachel hat selbst eine Rundung und einen Rahmen.
        if (t.links < 4 || t.rechts > SEITE - 4 || t.y > SEITE - 2 || t.y - t.schrift < 0) {
          pruefe(false, `Stil ${stil}, ${slot}: "${t.s}" liegt ausserhalb`,
                 `x ${t.links.toFixed(0)}..${t.rechts.toFixed(0)}, y ${t.y}, ${t.schrift}px`);
        }
      }
    }
  }
}
pruefe(zeilen > 100, `${zeilen} Textstellen in allen Stilen und Grenzfällen geprüft, keine ragt über den Rand`);

console.log("\nLesbarkeit");
// Im Auto ist eine Kachel klein: Alles unter 11 Einheiten wäre dort kaum zu lesen.
let klein = [];
for (const stil of ["a", "b"]) {
  for (const slot of K.SLOTS) {
    const c = aufzeichner();
    K.zeichnen(stil, slot, d[slot], c);
    for (const t of c.texte) if (t.schrift < 11) klein.push(`${stil}/${slot} "${t.s}" ${t.schrift}px`);
  }
}
pruefe(klein.length === 0, "keine Beschriftung kleiner als 11 Einheiten", klein.join("; "));

console.log("\nBilder");
const b = K.bilder("a", p.m, p.reihen);
pruefe(K.SLOTS.every((s) => b[s] === "QUJD"), "ein Bild je Platz, Base64 ohne Kopf");
const bLeer = K.bilder("b", null, null);
pruefe(K.SLOTS.every((s) => typeof bLeer[s] === "string"),
       "auch ohne Modell kommt für jeden Platz ein (leeres) Bild - feste Plätze");
pruefe(K.bilder("klassisch", p.m, p.reihen) === null && K.bilder("x", p.m, p.reihen) === null,
       "für 'klassisch' und unbekannte Stile gibt es keine Bilder (dann zeichnet Swift)");
pruefe(K.STILE.map((s) => s.id).join() === "klassisch,a,b", "drei Stile zur Auswahl");

console.log(fehler ? `\n${fehler} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
process.exit(fehler ? 1 : 0);
