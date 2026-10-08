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
//     node tools/check_driving_state.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "live.js"), "utf8");

let failure = 0;
function verify(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) failure++;
}

const storage = {};
const reports = [];
const K0 = {
  state: { sessionId: 7 },
  report: (t) => reports.push(t),
  at() {}, sliderCouple() {}, sessionRemember() {}, rememberedSession: () => null,
  api: async (fs_path, opt) => { K0.lastBody = opt && opt.body; return { kind: "zustand" }; },
};
const empty = () => new Proxy(function () { return ""; }, {
  get: (z, n) => (n === Symbol.toPrimitive ? () => "" : n === "style" ? {} : empty()),
  apply: () => empty(), set: () => true });
const K = new Proxy(K0, { get: (z, n) => (n in z ? z[n] : empty()) });

/* Ein nachgebauter Dongle. `trennen()` loest wie in echt den Verbindungsabriss
 * aus - daran haengt der automatische Wiederaufbau, und genau der darf am
 * geparkten Auto nicht anspringen. */
const obd = {
  connectedFlag: true, read: 0, separate: 0, bodywork: 0,
  atDropout: null, loops: [],
  set_up(log, atDropout) { obd.atDropout = atDropout; },
  linked: () => obd.connectedFlag,
  obtainable: () => true,
  attach: async () => { obd.connectedFlag = true; },
  handshake: async () => true,
  rawResponse: null,
  readRecord: async () => { obd.read++; return obd.rawResponse ? { ...obd.rawResponse } : { soc_raw: 180, speed_kmh: 0 }; },
  volt: null, voltages: 0,
  voltage: async () => { obd.voltages++; return obd.volt; },
  socFromRaw: () => ({ hmi: 73 }),
  detach() {
    obd.separate++;
    obd.connectedFlag = false;
    if (obd.atDropout) obd.atDropout();
  },
  reconnect(attempt, onward) { obd.bodywork++; obd.loops.push(onward); },
};

let geoCallback = null;
const timeframe = { jolt: K, addEventListener() {}, joltObd: obd,
                  joltBlePlugin: undefined };
const context = {
  window: timeframe, console, setTimeout, clearTimeout, Date, JSON, Math, Promise,
  document: { addEventListener() {}, getElementById: () => empty(),
              querySelector: () => empty(), querySelectorAll: () => [],
              createElement: () => empty(), body: { appendChild() {} },
              visibilityState: "visible" },
  navigator: { geolocation: {
    watchPosition: (f) => { geoCallback = f; return 1; }, clearWatch() {} } },
  localStorage: { getItem: (k) => (k in storage ? storage[k] : null),
                  setItem: (k, v) => { storage[k] = v; },
                  removeItem: (k) => { delete storage[k]; } },
  WebSocket: function () {},
};
vm.createContext(context);
vm.runInContext(source, context);
const live = timeframe.joltLive;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

let now_ts = Date.UTC(2026, 9, 5, 8, 0, 0);
const realNow = Date.now;
Date.now = () => now_ts;

const LAT = 48.4770, LON = 9.1444;
const M_PER_DEGREE_LAT = 111320;

/* Ein Fix `kmh` schnell, `nordM` Meter noerdlich vom Ausgangspunkt. */
async function fix(kmh, nordM = 0, withoutSpeed = false) {
  geoCallback({ coords: {
    latitude: LAT + nordM / M_PER_DEGREE_LAT, longitude: LON,
    speed: withoutSpeed ? null : kmh / 3.6, altitude: null },
    timestamp: now_ts });
  await wait(5);
}
const passes = (s) => { now_ts += s * 1000; };

/* Eine Messrunde: Der Takt der Meldungen ist 12 s - danach faellt ein Fix durch
 * die Drosselung und loest, wenn erlaubt, eine Leserunde aus. */
async function lap(kmh, nordM = 0) {
  passes(13);
  const prior = obd.read;
  await fix(kmh, nordM);
  await wait(20);
  return obd.read - prior;
}

(async () => {
  live.positionTrace();
  live.dongleUse();                 // Dongle gilt als in Benutzung, verbunden

  console.log("\nStart: Telefon weiss noch nichts");
  verify(live.driving_state() === "steht" && !live.readAllowed(),
         "ohne Bewegung wird nicht gelesen");
  verify(await lap(0) === 0, "auch eine Runde im Stand fragt das Auto nichts");

  console.log("\nLosfahren");
  await fix(30); passes(1);
  verify(live.driving_state() === "steht",
         "ein einzelner schneller Fix reicht nicht - GPS springt auch");
  await fix(30);
  verify(live.driving_state() === "faehrt", "zwei hintereinander: Das Auto faehrt");
  verify(await lap(60) === 1, "jetzt wird gelesen");
  verify(obd.bodywork === 0, "und der Dongle war da - es gab nichts aufzubauen");

  console.log("\nAmpel");
  for (let i = 0; i < 4; i++) { passes(1); await fix(0); }
  verify(live.driving_state() === "faehrt",
         "vier Sekunden Stillstand aendern nichts");
  verify(await lap(60) === 1, "gelesen wird weiter");

  console.log("\nStau: laenger als zehn Sekunden");
  for (let i = 0; i < 12; i++) { passes(1); await fix(0); }
  verify(live.driving_state() === "steht", "nach zehn Sekunden Stillstand: steht");
  verify(await lap(0) === 0, "es wird nichts mehr gefragt");
  verify(obd.separate === 0 && obd.connectedFlag,
         "aber der Dongle bleibt verbunden - eine Ampel kostet keinen Neuaufbau");
  await fix(40); passes(1); await fix(40);
  verify(live.driving_state() === "faehrt" && await lap(50) === 1,
         "und beim Anfahren wird sofort wieder gelesen, ohne Verbindungsaufbau");
  verify(obd.bodywork === 0, "ohne dass etwas neu verbunden werden musste");

  console.log("\nAussteigen und weggehen");
  for (let i = 0; i < 12; i++) { passes(1); await fix(0, 0); }
  const reportsPrior = reports.length;
  await fix(5, 10); passes(2); await fix(5, 20);
  verify(obd.separate === 0, "zehn, zwanzig Meter zu Fuss: noch nicht getrennt");
  passes(2); await fix(5, 40);
  verify(live.driving_state() === "geparkt" && obd.separate === 1,
         "ueber 25 m vom Halteort entfernt: geparkt, Dongle getrennt",
         `${live.driving_state()}, getrennt ${obd.separate}`);
  verify(obd.bodywork === 0,
         "und das Trennen loest keinen Wiederaufbau aus - sonst wuerde jolt am " +
         "abgeschlossenen Auto sofort wieder verbinden", String(obd.bodywork));
  verify(reports.length > reportsPrior &&
         /weggegangen/.test(reports[reports.length - 1]),
         "und sagt es", reports[reports.length - 1]);
  verify(await lap(5, 60) === 0, "weiter zu Fuss: nichts wird gefragt");
  verify(obd.bodywork === 0, "und nichts aufgebaut");

  console.log("\nGehen ist kein Fahren");
  for (let i = 0; i < 6; i++) { passes(2); await fix(7, 60 + i * 10); }
  for (let i = 0; i < 4; i++) { passes(2); await fix(14, 120 + i * 20); }
  verify(live.driving_state() === "geparkt" && obd.bodywork === 0,
         "auch zuegiges Gehen oder Joggen (unter 15 km/h) holt den Dongle nicht");

  console.log("\nFahrrad oder Bus: schnell, aber ohne Halt dazwischen");
  await fix(25, 200); passes(1); await fix(25, 230);
  verify(live.driving_state() === "geparkt" && obd.bodywork === 0,
         "ohne dass vorher gestanden wurde, gilt Schnellsein nicht - es koennte " +
         "jeder Weg sein, nur nicht der zum Auto");

  console.log("\nWieder einsteigen und losfahren");
  for (let i = 0; i < 3; i++) { passes(1); await fix(0, 230); }
  await fix(30, 240); passes(1);
  verify(obd.bodywork === 0, "ein schneller Fix: noch nichts");
  await fix(30, 250);
  verify(live.driving_state() === "faehrt" && obd.bodywork === 1,
         "zwei schnelle Fixes nach einem Halt: Der Dongle wird geholt",
         `${live.driving_state()}, Aufbauten ${obd.bodywork}`);
  verify(/Fahrt erkannt/.test(reports[reports.length - 1]),
         "und es wird gesagt", reports[reports.length - 1]);
  const loop = obd.loops[obd.loops.length - 1];
  verify(loop() === true, "der Wiederaufbau laeuft, solange das Auto faehrt");
  obd.connectedFlag = true;
  verify(await lap(50) === 1, "und danach wird gelesen");

  console.log("\nDrei Minuten Stillstand");
  for (let i = 0; i < 4; i++) { passes(60); await fix(0, 250); }
  verify(live.driving_state() === "geparkt" && !obd.connectedFlag,
         "wer drei Minuten steht, wird geparkt und getrennt - auch ohne wegzugehen",
         live.driving_state());
  const abort = obd.loops[obd.loops.length - 1];
  verify(abort() === false,
         "und eine noch laufende Wiederaufbau-Schleife endet");

  console.log("\nKein Dongle in Reichweite");
  await fix(0, 250); await fix(30, 260); passes(1); await fix(30, 270);
  const bodywork = obd.bodywork;
  const wl = obd.loops[obd.loops.length - 1];
  let responses = [];
  for (let i = 0; i < 42; i++) responses.push(wl());
  verify(responses.slice(0, 40).every((x) => x === true) && responses[40] === false,
         "bei Fahrt gibt jolt erst nach vierzig Fehlversuchen auf - eine Fahrt "
         + "ohne Dongle von Hand neu verbinden zu müssen war der Fehler",
         JSON.stringify(responses));
  verify(live.driving_state() === "geparkt",
         "und parkt, statt den Rest der Fahrt weiter anzuklopfen");
  await fix(30, 300); passes(1); await fix(30, 330);
  verify(obd.bodywork === bodywork,
         "ohne Halt dazwischen kein neuer Versuch");

  console.log("\nVon Hand verbinden");
  for (let i = 0; i < 3; i++) { passes(1); await fix(0, 330); }
  await live.connectDongle();
  verify(live.readAllowed(), "nach dem Tippen auf Verbinden wird im Stand gelesen");
  for (let i = 0; i < 4; i++) { passes(60); await fix(0, 330); }
  verify(live.driving_state() !== "geparkt" && live.readAllowed(),
         "auch nach vier Minuten Stillstand - das ist, was man damit will");
  for (let i = 0; i < 8; i++) { passes(60); await fix(0, 330); }
  verify(!live.readAllowed(),
         "nach zehn Minuten greift die Automatik wieder - das Lesen endet",
         live.driving_state());
  for (let i = 0; i < 4; i++) { passes(60); await fix(0, 330); }
  verify(live.driving_state() === "geparkt" && !obd.connectedFlag,
         "und nach drei weiteren Minuten Stillstand ist der Dongle getrennt",
         live.driving_state());

  console.log("\nHandshake unvollständig");
  const handshakeOld = obd.handshake;
  let attempts = 0;
  obd.connectedFlag = true;
  obd.handshake = async () => { attempts++; return attempts >= 2; };
  verify(await live.handshakeSafe() === true && attempts === 2,
         "ein unvollständiger Handshake wird einmal wiederholt");
  attempts = 0;
  obd.handshake = async () => { attempts++; return false; };
  verify(await live.handshakeSafe() === true && attempts === 2,
         "bleibt er unvollständig, der Dongle aber verbunden, gilt er als benutzbar");
  obd.connectedFlag = false;
  verify(await live.handshakeSafe() === false,
         "ohne Verbindung gilt er nicht");
  obd.handshake = handshakeOld;
  obd.connectedFlag = true;

  console.log("\nOhne Automatik");
  live.setAuto(false);
  obd.connectedFlag = true;
  verify(live.readAllowed() && await lap(0) === 1,
         "mit ausgeschalteter Automatik wird wie frueher auch im Stand gelesen");
  live.setAuto(true);

  console.log("\nGeraet liefert keine Geschwindigkeit");
  for (let i = 0; i < 3; i++) { passes(1); await fix(0, 330); }
  passes(2); await fix(0, 350, true);          // 20 m in 2 s = 36 km/h
  passes(2); await fix(0, 370, true);
  verify(live.driving_state() === "faehrt",
         "fehlt die Geschwindigkeit (iOS im Browser), wird sie aus zwei Positionen " +
         "gerechnet", live.driving_state());

  console.log("\n12-V-Spannung: Das Auto geht aus");
  /* ATRV misst der ELM-Chip selbst, ohne den CAN-Bus zu beruehren. Faellt die
   * Spannung im Stand ab, ist das Auto aus - noch bevor jemand abschliesst. */
  const ticks = async (n, kmh, nordM = 330) => {
    for (let i = 0; i < n; i++) { passes(2); await fix(kmh, nordM); await live.examineVoltage(); }
  };
  obd.connectedFlag = true; obd.volt = 14.0;
  await fix(30, 400); passes(1); await fix(30, 420);
  verify(live.driving_state() === "faehrt", "Ausgangslage: Das Auto faehrt");
  await ticks(6, 50, 440);
  verify(obd.voltages >= 6, "waehrend der Fahrt wird die Spannung gemessen",
         String(obd.voltages));

  await ticks(3, 0, 440);
  verify(live.driving_state() !== "geparkt",
         "steht das Auto mit unveraenderter Spannung (Ampel, laedt, Fahrer sitzt " +
         "drin), aendert sich nichts", live.driving_state());

  obd.volt = 12.5;
  await ticks(1, 0, 440);
  verify(live.driving_state() !== "geparkt",
         "ein einzelner niedriger Wert reicht nicht - ein Lastspruung ist keine Aus",
         live.driving_state());
  obd.volt = 14.0; await ticks(1, 0, 440);
  obd.volt = 12.5; await ticks(1, 0, 440);
  verify(live.driving_state() !== "geparkt",
         "und er zaehlt nicht mit dem naechsten zusammen, wenn dazwischen " +
         "wieder alles normal war");

  const separatePrior = obd.separate, bodyworkPrior = obd.bodywork;
  const t0 = now_ts;
  await ticks(2, 0, 440);
  verify(live.driving_state() === "geparkt" && obd.separate === separatePrior + 1,
         "zwei niedrige Werte hintereinander im Stand: Das Auto ist aus, Dongle " +
         "getrennt", `${live.driving_state()}, getrennt ${obd.separate - separatePrior}`);
  verify(now_ts - t0 < 10000,
         "und zwar nach Sekunden, nicht erst nach den zehn Sekunden Stillstand " +
         "oder den drei Minuten", `${(now_ts - t0) / 1000} s`);
  verify(obd.bodywork === bodyworkPrior,
         "ohne dass das Trennen einen Wiederaufbau ausloest");
  verify(/12-V/.test(reports[reports.length - 1]),
         "und es wird gesagt, warum", reports[reports.length - 1]);
  verify(await lap(0) === 0, "danach fragt jolt das Auto nichts mehr");
  const measurements = obd.voltages;
  await ticks(3, 0, 440);
  verify(obd.voltages === measurements,
         "und misst auch die Spannung nicht mehr - der Dongle ist getrennt");

  console.log("\n12-V-Spannung: Fahrt ohne Grundlage");
  for (let i = 0; i < 3; i++) { passes(1); await fix(0, 440); }
  await fix(30, 460); passes(1); await fix(30, 480);
  obd.connectedFlag = true;
  verify(live.driving_state() === "faehrt", "wieder losgefahren");
  obd.volt = 14.0;
  await ticks(2, 50, 500);                       // zu wenige Werte
  for (let i = 0; i < 3; i++) { passes(1); await fix(0, 500); }
  obd.volt = 12.5;
  await ticks(3, 0, 500);
  verify(live.driving_state() !== "geparkt",
         "ohne genug Werte aus der Fahrt entscheidet die Spannung nichts - die " +
         "Regeln ueber Stand und Weg bleiben", live.driving_state());

  console.log("\n12-V-Spannung: Abfall waehrend der Fahrt");
  obd.volt = 14.0;
  await fix(30, 520); passes(1); await fix(30, 540);
  await ticks(6, 60, 560);
  obd.volt = 12.5;
  await ticks(4, 60, 600);
  verify(live.driving_state() === "faehrt",
         "ein Abfall bei Fahrtgeschwindigkeit ist keine Parkposition");

  console.log("\n12-V-Spannung: Unbrauchbare Antwort");
  obd.volt = null;
  await ticks(3, 0, 600);
  verify(live.driving_state() !== "geparkt", "keine Antwort: nichts passiert");
  obd.volt = 99;
  await ticks(3, 0, 600);
  verify(live.driving_state() !== "geparkt", "und ein Unsinnswert auch nicht");

  console.log("\n12-V-Spannung im Messpunkt");
  obd.volt = 14.0; obd.connectedFlag = true;
  await fix(30, 620); passes(1); await fix(30, 640);
  await ticks(1, 50, 650);
  await lap(50, 660);
  await wait(20);
  const sent = K0.lastBody && K0.lastBody.points
    ? K0.lastBody.points[K0.lastBody.points.length - 1] : null;
  verify(!!sent && sent.raw_values && sent.raw_values.batt_v === 14,
         "die Spannung geht mit dem Messpunkt hinaus - damit sich die Schwelle " +
         "spaeter an echten Fahrten nachpruefen laesst",
         JSON.stringify(sent && sent.raw_values));

  console.log("\n12-V-Spannung im Stand: auch ohne Fahrzeugabfrage");
  for (let i = 0; i < 12; i++) { passes(1); await fix(0, 660); }
  obd.volt = 13.9;
  await live.examineVoltage();
  const readPrior = obd.read;
  await lap(0, 660);
  await wait(20);
  const withoutCan = K0.lastBody && K0.lastBody.points
    ? K0.lastBody.points[K0.lastBody.points.length - 1] : null;
  verify(obd.read === readPrior && !live.readAllowed(),
         "im Stand wird das Auto nicht gefragt");
  verify(!!withoutCan && withoutCan.raw_values && withoutCan.raw_values.batt_v === 13.9,
         "die Spannung geht trotzdem mit - dort fällt sie beim Ausschalten, und " +
         "ohne diesen Punkt stünde der Abfall nirgends",
         JSON.stringify(withoutCan && withoutCan.raw_values));
  verify(Object.keys(withoutCan.raw_values).join() === "batt_v",
         "und nur sie: Ein Punkt ohne Fahrzeugabfrage behauptet keine Zähler");

  console.log("\nUngueltige Werte aus dem Auto");
  // Das Tempo-Byte steht bei "ungueltig" auf 255 (in den gespeicherten Fahrten
  // kommt das vor), die Aussentemperatur b0/2-50 ergibt bei 0xFF 77,5 Grad. Der
  // Server lehnt beides mit 422 ab - und frueher haette das den ganzen Stapel
  // gekostet. Hier gehen sie gar nicht erst hinaus.
  obd.connectedFlag = true; obd.volt = 14.0;
  const gone_out = () => K0.lastBody.points[K0.lastBody.points.length - 1];
  obd.rawResponse = { soc_raw: 180, speed_kmh: 255, outside_temp_c: 77.5 };
  await fix(30, 700); passes(1); await fix(30, 720);
  await lap(50, 740);
  await wait(20);
  verify(Math.abs(gone_out().speed_kmh - 50) < 0.01,
         "255 km/h aus dem Auto bedeuten ungueltig: Es gilt das Tempo des GPS (50)",
         String(gone_out().speed_kmh));
  verify(gone_out().outside_temp_c === undefined || gone_out().outside_temp_c === null,
         "und 77,5 Grad bleiben leer, statt hinauszugehen", String(gone_out().outside_temp_c));
  verify(gone_out().raw_values && gone_out().raw_values.speed_kmh === 255,
         "die Rohwerte bleiben, wie das Auto sie lieferte - sie sind Befund, keine Rechnung");

  obd.rawResponse = { soc_raw: 180, speed_kmh: 88, outside_temp_c: 14 };
  await lap(50, 760);
  await wait(20);
  verify(gone_out().speed_kmh === 88 && gone_out().outside_temp_c === 14,
         "plausible Werte aus dem Auto schlagen weiter das GPS");
  obd.rawResponse = { soc_raw: 180, speed_kmh: 0, outside_temp_c: -60 };
  await lap(50, 780);
  await wait(20);
  verify(gone_out().speed_kmh === 0 && gone_out().outside_temp_c === -60,
         "auch 0 km/h und -60 Grad (noch in den Grenzen)");
  obd.rawResponse = null;

  Date.now = realNow;
  console.log(failure ? `\n${failure} Pruefung(en) fehlgeschlagen.`
                     : "\nAlle Pruefungen bestanden.");
  process.exit(failure ? 1 : 0);
})();
