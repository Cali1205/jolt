#!/usr/bin/env node
// Prueft die Verkehrskachel der gewaehlten Route (frontend/route.js).
//
// Der Verkehr kommt von TomTom und wird nicht gespeichert: Er steht nur in der
// Antwort der letzten Planung. Die Kachel muss ihn deshalb dort suchen, ueber
// die Fahrt-ID - und darf bei einer Fahrt, zu der keine Planung vorliegt,
// nichts zeigen. Ein Verkehr von gestern waere schlimmer als keiner.
//
//     node tools/check_route_anzeige.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const quelle = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "route.js"), "utf8");

let fehler = 0;
function pruefe(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) fehler++;
}

const K = {
  zustand: {},
  zahl: (x, stellen) => Number(x).toFixed(stellen || 0).replace(".", ","),
  dauer: (m) => m + " min",
  wertKachel: (name, text, art) => `[${name}|${text}|${art || ""}]`,
};
const leer = () => new Proxy(function () { return ""; }, {
  get: (z, n) => (n === Symbol.toPrimitive ? () => "" : n === "style" ? {} : leer()),
  apply: () => leer(), set: () => true });
const fenster = { jolt: K, addEventListener() {} };
const kontext = {
  window: fenster, console, setTimeout, clearTimeout, Date, JSON, Math, Number,
  document: { getElementById: () => leer(), querySelector: () => leer(),
              querySelectorAll: () => [], createElement: () => leer(),
              addEventListener() {} },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  navigator: {},
};
vm.createContext(kontext);
vm.runInContext(quelle, kontext);
const kachel = fenster.joltRoute.verkehrKachel;

const planung = [
  { fahrt_id: 11, verkehr_min: 11.7, verkehr_quelle: "TomTom" },
  { fahrt_id: 12, verkehr_min: 0.2, verkehr_quelle: "TomTom" },
  { fahrt_id: 13, verkehr_min: 22.0, verkehr_quelle: "TomTom" },
  { fahrt_id: 14 },
];

console.log("\nEine einzelne Route");
let k = kachel({ fahrt_id: 11 }, [planung[0]]);
pruefe(k === "[Verkehr · TomTom|+12 min|]",
       "auch ohne Variantenkarten steht die Verzögerung bei den Kennzahlen, mit Quelle", k);

console.log("\nMehrere Varianten: jede bekommt ihre eigene");
pruefe(kachel({ fahrt_id: 11 }, planung).includes("+12 min")
       && kachel({ fahrt_id: 13 }, planung).includes("+22 min"),
       "die Kachel gehört zur gewählten Route, nicht zur ersten");

console.log("\nGrenzfälle");
pruefe(kachel({ fahrt_id: 12 }, planung).includes("frei"),
       "unter einer halben Minute steht \"frei\" statt \"+0 min\"",
       kachel({ fahrt_id: 12 }, planung));
pruefe(kachel({ fahrt_id: 13 }, planung).endsWith("|schlecht]"),
       "ab einer Viertelstunde fällt die Kachel auf",
       kachel({ fahrt_id: 13 }, planung));
pruefe(!kachel({ fahrt_id: 11 }, planung).includes("schlecht"),
       "und darunter nicht");
pruefe(kachel({ fahrt_id: 14 }, planung) === "",
       "ohne Angabe von TomTom (kein Schlüssel, Fehler, abgeschaltet) keine Kachel");
pruefe(kachel({ fahrt_id: 99 }, planung) === "",
       "eine Fahrt aus der Liste, zu der keine Planung vorliegt, auch nicht - "
       + "ein Verkehr von gestern wäre schlimmer als keiner");
pruefe(kachel({ fahrt_id: 11 }, []) === "" && kachel({ fahrt_id: 11 }, undefined) === ""
       && kachel({ fahrt_id: 11 }, null) === "",
       "und mit leerer Planung bricht nichts");
pruefe(kachel({ fahrt_id: 11 }, [{ fahrt_id: 11, verkehr_min: "viel" }]) === "",
       "eine Angabe, die keine Zahl ist, wird nicht gezeigt");
pruefe(kachel({ fahrt_id: 11 }, [{ fahrt_id: 11, verkehr_min: 3 }]).includes("TomTom"),
       "fehlt die Quelle, steht TomTom da - es gibt keine andere");

console.log(fehler ? `\n${fehler} Prüfung(en) fehlgeschlagen.` : "\nAlle Prüfungen bestanden.");
process.exit(fehler ? 1 : 0);
