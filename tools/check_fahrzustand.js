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
  api: async (pfad, opt) => { K0.letzterBody = opt && opt.body; return { typ: "zustand" }; },
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
  rohAntwort: null,
  satzLesen: async () => { obd.gelesen++; return obd.rohAntwort ? { ...obd.rohAntwort } : { soc_roh: 180, tempo_kmh: 0 }; },
  volt: null, spannungen: 0,
  spannung: async () => { obd.spannungen++; return obd.volt; },
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

  console.log("\nHandshake unvollständig");
  const handshakeAlt = obd.handshake;
  let versuche = 0;
  obd.verbundenFlag = true;
  obd.handshake = async () => { versuche++; return versuche >= 2; };
  pruefe(await live.handshakeSicher() === true && versuche === 2,
         "ein unvollständiger Handshake wird einmal wiederholt");
  versuche = 0;
  obd.handshake = async () => { versuche++; return false; };
  pruefe(await live.handshakeSicher() === true && versuche === 2,
         "bleibt er unvollständig, der Dongle aber verbunden, gilt er als benutzbar");
  obd.verbundenFlag = false;
  pruefe(await live.handshakeSicher() === false,
         "ohne Verbindung gilt er nicht");
  obd.handshake = handshakeAlt;
  obd.verbundenFlag = true;

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

  console.log("\n12-V-Spannung: Das Auto geht aus");
  /* ATRV misst der ELM-Chip selbst, ohne den CAN-Bus zu beruehren. Faellt die
   * Spannung im Stand ab, ist das Auto aus - noch bevor jemand abschliesst. */
  const ticks = async (n, kmh, nordM = 330) => {
    for (let i = 0; i < n; i++) { vergeht(2); await fix(kmh, nordM); await live.spannungPruefen(); }
  };
  obd.verbundenFlag = true; obd.volt = 14.0;
  await fix(30, 400); vergeht(1); await fix(30, 420);
  pruefe(live.fahrzustand() === "faehrt", "Ausgangslage: Das Auto faehrt");
  await ticks(6, 50, 440);
  pruefe(obd.spannungen >= 6, "waehrend der Fahrt wird die Spannung gemessen",
         String(obd.spannungen));

  await ticks(3, 0, 440);
  pruefe(live.fahrzustand() !== "geparkt",
         "steht das Auto mit unveraenderter Spannung (Ampel, laedt, Fahrer sitzt " +
         "drin), aendert sich nichts", live.fahrzustand());

  obd.volt = 12.5;
  await ticks(1, 0, 440);
  pruefe(live.fahrzustand() !== "geparkt",
         "ein einzelner niedriger Wert reicht nicht - ein Lastspruung ist keine Aus",
         live.fahrzustand());
  obd.volt = 14.0; await ticks(1, 0, 440);
  obd.volt = 12.5; await ticks(1, 0, 440);
  pruefe(live.fahrzustand() !== "geparkt",
         "und er zaehlt nicht mit dem naechsten zusammen, wenn dazwischen " +
         "wieder alles normal war");

  const getrenntVor = obd.getrennt, aufbautenVor = obd.aufbauten;
  const t0 = jetzt;
  await ticks(2, 0, 440);
  pruefe(live.fahrzustand() === "geparkt" && obd.getrennt === getrenntVor + 1,
         "zwei niedrige Werte hintereinander im Stand: Das Auto ist aus, Dongle " +
         "getrennt", `${live.fahrzustand()}, getrennt ${obd.getrennt - getrenntVor}`);
  pruefe(jetzt - t0 < 10000,
         "und zwar nach Sekunden, nicht erst nach den zehn Sekunden Stillstand " +
         "oder den drei Minuten", `${(jetzt - t0) / 1000} s`);
  pruefe(obd.aufbauten === aufbautenVor,
         "ohne dass das Trennen einen Wiederaufbau ausloest");
  pruefe(/12-V/.test(meldungen[meldungen.length - 1]),
         "und es wird gesagt, warum", meldungen[meldungen.length - 1]);
  pruefe(await runde(0) === 0, "danach fragt jolt das Auto nichts mehr");
  const messungen = obd.spannungen;
  await ticks(3, 0, 440);
  pruefe(obd.spannungen === messungen,
         "und misst auch die Spannung nicht mehr - der Dongle ist getrennt");

  console.log("\n12-V-Spannung: Fahrt ohne Grundlage");
  for (let i = 0; i < 3; i++) { vergeht(1); await fix(0, 440); }
  await fix(30, 460); vergeht(1); await fix(30, 480);
  obd.verbundenFlag = true;
  pruefe(live.fahrzustand() === "faehrt", "wieder losgefahren");
  obd.volt = 14.0;
  await ticks(2, 50, 500);                       // zu wenige Werte
  for (let i = 0; i < 3; i++) { vergeht(1); await fix(0, 500); }
  obd.volt = 12.5;
  await ticks(3, 0, 500);
  pruefe(live.fahrzustand() !== "geparkt",
         "ohne genug Werte aus der Fahrt entscheidet die Spannung nichts - die " +
         "Regeln ueber Stand und Weg bleiben", live.fahrzustand());

  console.log("\n12-V-Spannung: Abfall waehrend der Fahrt");
  obd.volt = 14.0;
  await fix(30, 520); vergeht(1); await fix(30, 540);
  await ticks(6, 60, 560);
  obd.volt = 12.5;
  await ticks(4, 60, 600);
  pruefe(live.fahrzustand() === "faehrt",
         "ein Abfall bei Fahrtgeschwindigkeit ist keine Parkposition");

  console.log("\n12-V-Spannung: Unbrauchbare Antwort");
  obd.volt = null;
  await ticks(3, 0, 600);
  pruefe(live.fahrzustand() !== "geparkt", "keine Antwort: nichts passiert");
  obd.volt = 99;
  await ticks(3, 0, 600);
  pruefe(live.fahrzustand() !== "geparkt", "und ein Unsinnswert auch nicht");

  console.log("\n12-V-Spannung im Messpunkt");
  obd.volt = 14.0; obd.verbundenFlag = true;
  await fix(30, 620); vergeht(1); await fix(30, 640);
  await ticks(1, 50, 650);
  await runde(50, 660);
  await warte(20);
  const gesendet = K0.letzterBody && K0.letzterBody.punkte
    ? K0.letzterBody.punkte[K0.letzterBody.punkte.length - 1] : null;
  pruefe(!!gesendet && gesendet.rohwerte && gesendet.rohwerte.batt_v === 14,
         "die Spannung geht mit dem Messpunkt hinaus - damit sich die Schwelle " +
         "spaeter an echten Fahrten nachpruefen laesst",
         JSON.stringify(gesendet && gesendet.rohwerte));

  console.log("\n12-V-Spannung im Stand: auch ohne Fahrzeugabfrage");
  for (let i = 0; i < 12; i++) { vergeht(1); await fix(0, 660); }
  obd.volt = 13.9;
  await live.spannungPruefen();
  const gelesenVor = obd.gelesen;
  await runde(0, 660);
  await warte(20);
  const ohneCan = K0.letzterBody && K0.letzterBody.punkte
    ? K0.letzterBody.punkte[K0.letzterBody.punkte.length - 1] : null;
  pruefe(obd.gelesen === gelesenVor && !live.lesenErlaubt(),
         "im Stand wird das Auto nicht gefragt");
  pruefe(!!ohneCan && ohneCan.rohwerte && ohneCan.rohwerte.batt_v === 13.9,
         "die Spannung geht trotzdem mit - dort fällt sie beim Ausschalten, und " +
         "ohne diesen Punkt stünde der Abfall nirgends",
         JSON.stringify(ohneCan && ohneCan.rohwerte));
  pruefe(Object.keys(ohneCan.rohwerte).join() === "batt_v",
         "und nur sie: Ein Punkt ohne Fahrzeugabfrage behauptet keine Zähler");

  console.log("\nUngueltige Werte aus dem Auto");
  // Das Tempo-Byte steht bei "ungueltig" auf 255 (in den gespeicherten Fahrten
  // kommt das vor), die Aussentemperatur b0/2-50 ergibt bei 0xFF 77,5 Grad. Der
  // Server lehnt beides mit 422 ab - und frueher haette das den ganzen Stapel
  // gekostet. Hier gehen sie gar nicht erst hinaus.
  obd.verbundenFlag = true; obd.volt = 14.0;
  const hinausgegangen = () => K0.letzterBody.punkte[K0.letzterBody.punkte.length - 1];
  obd.rohAntwort = { soc_roh: 180, tempo_kmh: 255, aussentemp_c: 77.5 };
  await fix(30, 700); vergeht(1); await fix(30, 720);
  await runde(50, 740);
  await warte(20);
  pruefe(Math.abs(hinausgegangen().tempo_kmh - 50) < 0.01,
         "255 km/h aus dem Auto bedeuten ungueltig: Es gilt das Tempo des GPS (50)",
         String(hinausgegangen().tempo_kmh));
  pruefe(hinausgegangen().aussentemp_c === undefined || hinausgegangen().aussentemp_c === null,
         "und 77,5 Grad bleiben leer, statt hinauszugehen", String(hinausgegangen().aussentemp_c));
  pruefe(hinausgegangen().rohwerte && hinausgegangen().rohwerte.tempo_kmh === 255,
         "die Rohwerte bleiben, wie das Auto sie lieferte - sie sind Befund, keine Rechnung");

  obd.rohAntwort = { soc_roh: 180, tempo_kmh: 88, aussentemp_c: 14 };
  await runde(50, 760);
  await warte(20);
  pruefe(hinausgegangen().tempo_kmh === 88 && hinausgegangen().aussentemp_c === 14,
         "plausible Werte aus dem Auto schlagen weiter das GPS");
  obd.rohAntwort = { soc_roh: 180, tempo_kmh: 0, aussentemp_c: -60 };
  await runde(50, 780);
  await warte(20);
  pruefe(hinausgegangen().tempo_kmh === 0 && hinausgegangen().aussentemp_c === -60,
         "auch 0 km/h und -60 Grad (noch in den Grenzen)");
  obd.rohAntwort = null;

  Date.now = echtesNow;
  console.log(fehler ? `\n${fehler} Pruefung(en) fehlgeschlagen.`
                     : "\nAlle Pruefungen bestanden.");
  process.exit(fehler ? 1 : 0);
})();
