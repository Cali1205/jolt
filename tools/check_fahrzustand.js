#!/usr/bin/env node
// Prueft, wann jolt den Dongle fragen darf: nur bei Fahrt, nie am verriegelten
// Auto - und dass es danach von selbst wieder verbindet.
//
// Ob ein Auto verriegelt ist, laesst sich ohne Fragen nicht erfahren. jolt
// schliesst deshalb aus der Bewegung des Telefons: Fahrtgeschwindigkeit heisst
// "sitzt im Auto", Stillstand und Weggehen heissen "nicht fragen". Geprueft
// wird die Logik gegen einen nachgebauten Dongle; ob eine bestimmte
// Alarmanlage ruhig bleibt, zeigt nur das Auto.
//
//     node tools/check_fahrzustand.js
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
const K0 = {
  zustand: { sitzungId: 7 },
  melden: (t) => meldungen.push(t),
  an() {}, reglerKoppeln() {}, sitzungMerken() {}, gemerkteSitzung: () => null,
  api: async () => ({ typ: "zustand" }),
};
const leer = () => new Proxy(function () { return ""; }, {
  get: (z, n) => (n === Symbol.toPrimitive ? () => "" : n === "style" ? {} : leer()),
  apply: () => leer(), set: () => true });
const K = new Proxy(K0, { get: (z, n) => (n in z ? z[n] : leer()) });

/* Ein nachgebauter Dongle. `trennen()` loest wie in echt den Verbindungsabriss
 * aus - daran haengt der automatische Wiederaufbau, und genau der darf am
 * geparkten Auto nicht anspringen. */
const obd = {
  verbundenFlag: true, gelesen: 0, getrennt: 0, aufbauten: 0,
  beiAbriss: null, schleifen: [],
  einrichten(log, beiAbriss) { obd.beiAbriss = beiAbriss; },
  verbunden: () => obd.verbundenFlag,
  verfuegbar: () => true,
  anschliessen: async () => { obd.verbundenFlag = true; },
  handshake: async () => true,
  satzLesen: async () => { obd.gelesen++; return { soc_roh: 180, tempo_kmh: 0 }; },
  socAusRoh: () => ({ hmi: 73 }),
  trennen() {
    obd.getrennt++;
    obd.verbundenFlag = false;
    if (obd.beiAbriss) obd.beiAbriss();
  },
  wiederverbinden(versuch, weiter) { obd.aufbauten++; obd.schleifen.push(weiter); },
};

let geoRueckruf = null;
const fenster = { jolt: K, addEventListener() {}, joltObd: obd,
                  joltBlePlugin: undefined };
const kontext = {
  window: fenster, console, setTimeout, clearTimeout, Date, JSON, Math, Promise,
  document: { addEventListener() {}, getElementById: () => leer(),
              querySelector: () => leer(), querySelectorAll: () => [],
              createElement: () => leer(), body: { appendChild() {} },
              visibilityState: "visible" },
  navigator: { geolocation: {
    watchPosition: (f) => { geoRueckruf = f; return 1; }, clearWatch() {} } },
  localStorage: { getItem: (k) => (k in speicher ? speicher[k] : null),
                  setItem: (k, v) => { speicher[k] = v; },
                  removeItem: (k) => { delete speicher[k]; } },
  WebSocket: function () {},
};
vm.createContext(kontext);
vm.runInContext(quelle, kontext);
const live = fenster.joltLive;
const warte = (ms) => new Promise((r) => setTimeout(r, ms));

let jetzt = Date.UTC(2026, 9, 5, 8, 0, 0);
const echtesNow = Date.now;
Date.now = () => jetzt;

const LAT = 48.4770, LON = 9.1444;
const M_JE_GRAD_LAT = 111320;

/* Ein Fix `kmh` schnell, `nordM` Meter noerdlich vom Ausgangspunkt. */
async function fix(kmh, nordM = 0, ohneGeschwindigkeit = false) {
  geoRueckruf({ coords: {
    latitude: LAT + nordM / M_JE_GRAD_LAT, longitude: LON,
    speed: ohneGeschwindigkeit ? null : kmh / 3.6, altitude: null },
    timestamp: jetzt });
  await warte(5);
}
const vergeht = (s) => { jetzt += s * 1000; };

/* Eine Messrunde: Der Takt der Meldungen ist 12 s - danach faellt ein Fix durch
 * die Drosselung und loest, wenn erlaubt, eine Leserunde aus. */
async function runde(kmh, nordM = 0) {
  vergeht(13);
  const vor = obd.gelesen;
  await fix(kmh, nordM);
  await warte(20);
  return obd.gelesen - vor;
}

(async () => {
  live.positionVerfolgen();
  live.dongleNutzen();                 // Dongle gilt als in Benutzung, verbunden

  console.log("\nStart: Telefon weiss noch nichts");
  pruefe(live.fahrzustand() === "steht" && !live.lesenErlaubt(),
         "ohne Bewegung wird nicht gelesen");
  pruefe(await runde(0) === 0, "auch eine Runde im Stand fragt das Auto nichts");

  console.log("\nLosfahren");
  await fix(30); vergeht(1);
  pruefe(live.fahrzustand() === "steht",
         "ein einzelner schneller Fix reicht nicht - GPS springt auch");
  await fix(30);
  pruefe(live.fahrzustand() === "faehrt", "zwei hintereinander: Das Auto faehrt");
  pruefe(await runde(60) === 1, "jetzt wird gelesen");
  pruefe(obd.aufbauten === 0, "und der Dongle war da - es gab nichts aufzubauen");

  console.log("\nAmpel");
  for (let i = 0; i < 4; i++) { vergeht(1); await fix(0); }
  pruefe(live.fahrzustand() === "faehrt",
         "vier Sekunden Stillstand aendern nichts");
  pruefe(await runde(60) === 1, "gelesen wird weiter");

  console.log("\nStau: laenger als zehn Sekunden");
  for (let i = 0; i < 12; i++) { vergeht(1); await fix(0); }
  pruefe(live.fahrzustand() === "steht", "nach zehn Sekunden Stillstand: steht");
  pruefe(await runde(0) === 0, "es wird nichts mehr gefragt");
  pruefe(obd.getrennt === 0 && obd.verbundenFlag,
         "aber der Dongle bleibt verbunden - eine Ampel kostet keinen Neuaufbau");
  await fix(40); vergeht(1); await fix(40);
  pruefe(live.fahrzustand() === "faehrt" && await runde(50) === 1,
         "und beim Anfahren wird sofort wieder gelesen, ohne Verbindungsaufbau");
  pruefe(obd.aufbauten === 0, "ohne dass etwas neu verbunden werden musste");

  console.log("\nAussteigen und weggehen");
  for (let i = 0; i < 12; i++) { vergeht(1); await fix(0, 0); }
  const meldungenVor = meldungen.length;
  await fix(5, 10); vergeht(2); await fix(5, 20);
  pruefe(obd.getrennt === 0, "zehn, zwanzig Meter zu Fuss: noch nicht getrennt");
  vergeht(2); await fix(5, 40);
  pruefe(live.fahrzustand() === "geparkt" && obd.getrennt === 1,
         "ueber 25 m vom Halteort entfernt: geparkt, Dongle getrennt",
         `${live.fahrzustand()}, getrennt ${obd.getrennt}`);
  pruefe(obd.aufbauten === 0,
         "und das Trennen loest keinen Wiederaufbau aus - sonst wuerde jolt am " +
         "abgeschlossenen Auto sofort wieder verbinden", String(obd.aufbauten));
  pruefe(meldungen.length > meldungenVor &&
         /weggegangen/.test(meldungen[meldungen.length - 1]),
         "und sagt es", meldungen[meldungen.length - 1]);
  pruefe(await runde(5, 60) === 0, "weiter zu Fuss: nichts wird gefragt");
  pruefe(obd.aufbauten === 0, "und nichts aufgebaut");

  console.log("\nGehen ist kein Fahren");
  for (let i = 0; i < 6; i++) { vergeht(2); await fix(7, 60 + i * 10); }
  for (let i = 0; i < 4; i++) { vergeht(2); await fix(14, 120 + i * 20); }
  pruefe(live.fahrzustand() === "geparkt" && obd.aufbauten === 0,
         "auch zuegiges Gehen oder Joggen (unter 15 km/h) holt den Dongle nicht");

  console.log("\nFahrrad oder Bus: schnell, aber ohne Halt dazwischen");
  await fix(25, 200); vergeht(1); await fix(25, 230);
  pruefe(live.fahrzustand() === "geparkt" && obd.aufbauten === 0,
         "ohne dass vorher gestanden wurde, gilt Schnellsein nicht - es koennte " +
         "jeder Weg sein, nur nicht der zum Auto");

  console.log("\nWieder einsteigen und losfahren");
  for (let i = 0; i < 3; i++) { vergeht(1); await fix(0, 230); }
  await fix(30, 240); vergeht(1);
  pruefe(obd.aufbauten === 0, "ein schneller Fix: noch nichts");
  await fix(30, 250);
  pruefe(live.fahrzustand() === "faehrt" && obd.aufbauten === 1,
         "zwei schnelle Fixes nach einem Halt: Der Dongle wird geholt",
         `${live.fahrzustand()}, Aufbauten ${obd.aufbauten}`);
  pruefe(/Fahrt erkannt/.test(meldungen[meldungen.length - 1]),
         "und es wird gesagt", meldungen[meldungen.length - 1]);
  const schleife = obd.schleifen[obd.schleifen.length - 1];
  pruefe(schleife() === true, "der Wiederaufbau laeuft, solange das Auto faehrt");
  obd.verbundenFlag = true;
  pruefe(await runde(50) === 1, "und danach wird gelesen");

  console.log("\nDrei Minuten Stillstand");
  for (let i = 0; i < 4; i++) { vergeht(60); await fix(0, 250); }
  pruefe(live.fahrzustand() === "geparkt" && !obd.verbundenFlag,
         "wer drei Minuten steht, wird geparkt und getrennt - auch ohne wegzugehen",
         live.fahrzustand());
  const abbruch = obd.schleifen[obd.schleifen.length - 1];
  pruefe(abbruch() === false,
         "und eine noch laufende Wiederaufbau-Schleife endet");

  console.log("\nKein Dongle in Reichweite");
  await fix(0, 250); await fix(30, 260); vergeht(1); await fix(30, 270);
  const aufbauten = obd.aufbauten;
  const wl = obd.schleifen[obd.schleifen.length - 1];
  let antworten = [];
  for (let i = 0; i < 10; i++) antworten.push(wl());
  pruefe(antworten.slice(0, 8).every((x) => x === true) && antworten[8] === false,
         "nach acht Fehlversuchen gibt jolt auf",
         JSON.stringify(antworten));
  pruefe(live.fahrzustand() === "geparkt",
         "und parkt, statt den Rest der Fahrt weiter anzuklopfen");
  await fix(30, 300); vergeht(1); await fix(30, 330);
  pruefe(obd.aufbauten === aufbauten,
         "ohne Halt dazwischen kein neuer Versuch");

  console.log("\nVon Hand verbinden");
  for (let i = 0; i < 3; i++) { vergeht(1); await fix(0, 330); }
  await live.dongleVerbinden();
  pruefe(live.lesenErlaubt(), "nach dem Tippen auf Verbinden wird im Stand gelesen");
  for (let i = 0; i < 4; i++) { vergeht(60); await fix(0, 330); }
  pruefe(live.fahrzustand() !== "geparkt" && live.lesenErlaubt(),
         "auch nach vier Minuten Stillstand - das ist, was man damit will");
  for (let i = 0; i < 8; i++) { vergeht(60); await fix(0, 330); }
  pruefe(!live.lesenErlaubt(),
         "nach zehn Minuten greift die Automatik wieder - das Lesen endet",
         live.fahrzustand());
  for (let i = 0; i < 4; i++) { vergeht(60); await fix(0, 330); }
  pruefe(live.fahrzustand() === "geparkt" && !obd.verbundenFlag,
         "und nach drei weiteren Minuten Stillstand ist der Dongle getrennt",
         live.fahrzustand());

  console.log("\nOhne Automatik");
  live.autoSetzen(false);
  obd.verbundenFlag = true;
  pruefe(live.lesenErlaubt() && await runde(0) === 1,
         "mit ausgeschalteter Automatik wird wie frueher auch im Stand gelesen");
  live.autoSetzen(true);

  console.log("\nGeraet liefert keine Geschwindigkeit");
  for (let i = 0; i < 3; i++) { vergeht(1); await fix(0, 330); }
  vergeht(2); await fix(0, 350, true);          // 20 m in 2 s = 36 km/h
  vergeht(2); await fix(0, 370, true);
  pruefe(live.fahrzustand() === "faehrt",
         "fehlt die Geschwindigkeit (iOS im Browser), wird sie aus zwei Positionen " +
         "gerechnet", live.fahrzustand());

  Date.now = echtesNow;
  console.log(fehler ? `\n${fehler} Pruefung(en) fehlgeschlagen.`
                     : "\nAlle Pruefungen bestanden.");
  process.exit(fehler ? 1 : 0);
})();
