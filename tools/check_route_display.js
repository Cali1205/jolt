#!/usr/bin/env node
// Prueft die Verkehrskachel der gewaehlten Route (frontend/route.js).
//
// Der Verkehr kommt von TomTom und wird nicht gespeichert: Er steht nur in der
// Antwort der letzten Planung. Die Kachel muss ihn deshalb dort suchen, ueber
// die Fahrt-ID - und darf bei einer Fahrt, zu der keine Planung vorliegt,
// nichts zeigen. Ein Verkehr von gestern waere schlimmer als keiner.
//
//     node tools/check_route_display.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "route.js"), "utf8");

let failure = 0;
function verify(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) failure++;
}

const K = {
  state: {},
  num: (x, put) => Number(x).toFixed(put || 0).replace(".", ","),
  duration: (m) => m + " min",
  valueTile: (name, text, variety) => `[${name}|${text}|${variety || ""}]`,
};
const empty = () => new Proxy(function () { return ""; }, {
  get: (z, n) => (n === Symbol.toPrimitive ? () => "" : n === "style" ? {} : empty()),
  apply: () => empty(), set: () => true });
const timeframe = { jolt: K, addEventListener() {} };
const context = {
  window: timeframe, console, setTimeout, clearTimeout, Date, JSON, Math, Number,
  document: { getElementById: () => empty(), querySelector: () => empty(),
              querySelectorAll: () => [], createElement: () => empty(),
              addEventListener() {} },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  navigator: {},
};
vm.createContext(context);
vm.runInContext(source, context);
const tile = timeframe.joltRoute.trafficTile;

const planning = [
  { trip_id: 11, traffic_min: 11.7, traffic_source: "TomTom" },
  { trip_id: 12, traffic_min: 0.2, traffic_source: "TomTom" },
  { trip_id: 13, traffic_min: 22.0, traffic_source: "TomTom" },
  { trip_id: 14 },
];

console.log("\nEine einzelne Route");
let k = tile({ trip_id: 11 }, [planning[0]]);
verify(k === "[Verkehr · TomTom|+12 min|]",
       "auch ohne Variantenkarten steht die Verzögerung bei den Kennzahlen, mit Quelle", k);

console.log("\nMehrere Varianten: jede bekommt ihre eigene");
verify(tile({ trip_id: 11 }, planning).includes("+12 min")
       && tile({ trip_id: 13 }, planning).includes("+22 min"),
       "die Kachel gehört zur gewählten Route, nicht zur ersten");

console.log("\nGrenzfälle");
verify(tile({ trip_id: 12 }, planning).includes("frei"),
       "unter einer halben Minute steht \"frei\" statt \"+0 min\"",
       tile({ trip_id: 12 }, planning));
verify(tile({ trip_id: 13 }, planning).endsWith("|schlecht]"),
       "ab einer Viertelstunde fällt die Kachel auf",
       tile({ trip_id: 13 }, planning));
verify(!tile({ trip_id: 11 }, planning).includes("schlecht"),
       "und darunter nicht");
verify(tile({ trip_id: 14 }, planning) === "",
       "ohne Angabe von TomTom (kein Schlüssel, Fehler, abgeschaltet) keine Kachel");
verify(tile({ trip_id: 99 }, planning) === "",
       "eine Fahrt aus der Liste, zu der keine Planung vorliegt, auch nicht - "
       + "ein Verkehr von gestern wäre schlimmer als keiner");
verify(tile({ trip_id: 11 }, []) === "" && tile({ trip_id: 11 }, undefined) === ""
       && tile({ trip_id: 11 }, null) === "",
       "und mit leerer Planung bricht nichts");
verify(tile({ trip_id: 11 }, [{ trip_id: 11, traffic_min: "viel" }]) === "",
       "eine Angabe, die keine Zahl ist, wird nicht gezeigt");
verify(tile({ trip_id: 11 }, [{ trip_id: 11, traffic_min: 3 }]).includes("TomTom"),
       "fehlt die Quelle, steht TomTom da - es gibt keine andere");

console.log("\nPrognose statt live");
const forecast = { trip_id: 21, traffic_min: 28, traffic_source: "TomTom", traffic_basis: "prognose" };
verify(tile({ trip_id: 21 }, [forecast]).startsWith("[Verkehr (Prognose) · TomTom|+28 min|"),
       "für eine spätere Abfahrt steht \"Prognose\" an der Kachel - eine Zahl für "
       + "Freitag 16 Uhr ist keine Messung von jetzt", tile({ trip_id: 21 }, [forecast]));
verify(tile({ trip_id: 21 }, [{ ...forecast, traffic_basis: "live" }])
       .startsWith("[Verkehr · TomTom|"),
       "live bleibt ohne den Zusatz");
verify(tile({ trip_id: 11 }, planning).startsWith("[Verkehr · TomTom|"),
       "und ohne Angabe der Basis auch (ältere Antworten)");

console.log("\nAbfahrt aus dem Formularfeld");
const iso = timeframe.joltRoute.departureIso;
verify(iso("") === null && iso(null) === null && iso(undefined) === null,
       "leer heisst jetzt");
verify(iso("kein Datum") === null, "und Unlesbares auch");
const w = "2026-10-09T16:00";
const e = iso(w);
verify(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(e),
       "sonst ein Zeitpunkt in UTC mit Z - der Server liest damit keine "
       + "Ortszeit falsch", String(e));
verify(new Date(e).getTime() === new Date(w).getTime(),
       "und es ist derselbe Moment wie die Eingabe, in welcher Zeitzone das "
       + "Gerät auch steht");
const field = timeframe.joltRoute.localForField(new Date(2026, 9, 9, 7, 5));
verify(field === "2026-10-09T07:05", "min und max des Felds sind Ortszeit mit "
       + "führenden Nullen", field);

console.log(failure ? `\n${failure} Prüfung(en) fehlgeschlagen.` : "\nAlle Prüfungen bestanden.");
process.exit(failure ? 1 : 0);
