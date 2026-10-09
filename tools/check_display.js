#!/usr/bin/env node
// Checks the display model (frontend/display.js): the state of the trip as
// a few numbers and texts for a Live Activity, a widget or a
// CarPlay template - without Swift, without a Mac, without Apple.
//
// Mainly checked is what could do harm in a car: an invented
// field (a charging stop on a recording without a plan), a wrong distance
// (the reserve is a position on the route, not a distance), and
// a sender that reports more often than Apple allows.
//
//     node tools/check_display.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "display.js"), "utf8");

let failure = 0;
function verify(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) failure++;
}

const timeframe = {};
const reports = [];
const context = { window: timeframe, console: { log: (...a) => reports.push(a.join(" ")) },
                  Date, JSON, Math, Number, setTimeout, clearTimeout, Promise };
vm.createContext(context);
vm.runInContext(source, context);
const A = timeframe.joltDisplay;

const NOW = 1_800_000_000_000;

// A state as /api/live/{id} returns it: planned trip, under way.
const planned = {
  km_on_route: 100.0, lat: 48.5, lon: 9.1,
  actual_soc: 72.1, soc_reported: true, soc_source: "gemessen", plan_soc: 73.0,
  deviation_pp: -0.9, remaining_km: 87.4, forecast_soc_at_target: 21.0,
  reserve_at_km: 160.0, arrival_shift_min: 12.3,
  next_stop: { id: 5, name: "Ionity Bad Rappenau", km_on_route: 141.2,
                     planned_soc: 19.0, expected_soc: 17.6 },
};

console.log("\nGeplante Fahrt");
let m = A.model(planned, NOW);
verify(m.version === 1 && m.as_of === NOW, "Version und Stand sind dabei");
verify(m.soc.text === "72 %" && m.soc.percent === 72.1 && m.soc.source === "gemessen",
       "Ladestand gerundet, mit Quelle", JSON.stringify(m.soc));
verify(m.reserve.km === 60 && m.reserve.text === "60 km",
       "Reserve in 60 km - die Differenz aus Position und Reserve-Marke, nicht "
       + "die Marke selbst (160)", JSON.stringify(m.reserve));
verify(m.stop.name === "Ionity Bad Rappenau" && m.stop.km === 41.2
       && m.stop.kmText === "41 km",
       "der Stopp in 41 km, wieder als Differenz (141,2 minus 100)", JSON.stringify(m.stop));
verify(m.stop.arrivalSoc === 18 && m.stop.arrivalSocText === "18 %"
       && m.stop.planned === false,
       "mit dem erwarteten Ladestand (17,6), nicht dem geplanten (19)", JSON.stringify(m.stop));
verify(m.arrival.text === "+12 min" && m.arrival.min === 12, "Ankunft +12 min",
       JSON.stringify(m.arrival));
verify(m.rest.text === "87 km", "Rest 87 km", JSON.stringify(m.rest));
verify(m.short === "72 % · Stopp in 41 km (18 %)",
       "die Kurzzeile fuer die kleinste Anzeige", m.short);

console.log("\nAufzeichnung ohne Plan");
const recording = { km_on_route: 0, lat: 48.5, lon: 9.1, actual_soc: 79.2,
                       soc_reported: false, soc_source: "zuletzt", plan_soc: null,
                       deviation_pp: null, remaining_km: 0, forecast_soc_at_target: null,
                       reserve_at_km: null, arrival_shift_min: null,
                       next_stop: null };
m = A.model(recording, NOW);
verify(m.soc.text === "79 %" && m.soc.source === "zuletzt",
       "der Ladestand ist da, als zuletzt gemessen gekennzeichnet", JSON.stringify(m.soc));
verify(m.stop === null && m.arrival === null && m.reserve === null && m.rest === null,
       "Stopp, Ankunft, Reserve und Rest fehlen - kein erfundenes Feld");
verify(m.rest === null, "und \"0 km Rest\" ist keine Aussage, sondern eine Luecke");
verify(m.short === "79 %", "die Kurzzeile zeigt nur, was da ist", m.short);
const onlyGps = A.model({ ...recording, actual_soc: null, soc_source: "gemessen" }, NOW);
verify(onlyGps.soc === null && onlyGps.short === "Keine Werte",
       "ohne Ladestand steht dort \"Keine Werte\" - kein 0 %, kein 100 %", onlyGps.short);

console.log("\nAnkunft");
const arr = (min) => A.model({ ...planned, arrival_shift_min: min }, NOW).arrival.text;
verify(arr(0.4) === "nach Plan" && arr(-0.4) === "nach Plan", "unter einer halben Minute: nach Plan");
verify(arr(12.3) === "+12 min", "zu spaet");
verify(arr(-5) === "−5 min", "zu frueh mit echtem Minuszeichen", arr(-5));
verify(arr(75) === "+1 h 15", "ueber einer Stunde in Stunden", arr(75));
verify(arr(65) === "+1 h 05", "mit fuehrender Null bei den Minuten", arr(65));

console.log("\nEntfernungen");
const stop = (km) => A.model({ ...planned, next_stop: { ...planned.next_stop,
                                  km_on_route: 100 + km } }, NOW).stop.kmText;
verify(stop(0.4) === "gleich", "unter einem Kilometer: gleich", stop(0.4));
verify(stop(7.34) === "7,3 km", "unter zehn mit einer Nachkommastelle", stop(7.34));
verify(stop(41.2) === "41 km", "darueber ganz", stop(41.2));
const toBack = A.model({ ...planned, reserve_at_km: 90 }, NOW);
verify(toBack.reserve === null,
       "eine Reserve-Marke hinter der Position ist keine Reichweite - sie fehlt");
const stopBack = A.model({ ...planned, next_stop: { ...planned.next_stop,
                                km_on_route: 80 } }, NOW);
verify(stopBack.stop === null, "ein Stopp, den man schon passiert hat, wird nicht gezeigt");

console.log("\nFehlende und kaputte Angaben");
const withoutSoc = A.model({ ...planned, next_stop: { name: "X", km_on_route: 130,
                          planned_soc: null, expected_soc: null } }, NOW);
verify(withoutSoc.stop.arrivalSoc === null && withoutSoc.stop.arrivalSocText === null
       && withoutSoc.short === "72 % · Stopp in 30 km",
       "ein Stopp ohne Ladestand: ohne Klammer, nicht \"(null %)\"", withoutSoc.short);
const onlyPlanned = A.model({ ...planned, next_stop: { ...planned.next_stop,
                              expected_soc: null } }, NOW);
verify(onlyPlanned.stop.arrivalSoc === 19 && onlyPlanned.stop.planned === true,
       "fehlt der erwartete Wert, gilt der geplante - und das steht dabei");
const withoutName = A.model({ ...planned, next_stop: { km_on_route: 130 } }, NOW);
verify(withoutName.stop.name === "Ladestopp", "ohne Namen steht \"Ladestopp\"");
for (const nonsense of [null, undefined, 5, "x", [], 0]) {
  verify(A.model(nonsense, NOW) === null, "kein Zustand ergibt kein Modell: " + JSON.stringify(nonsense));
}
const broken = A.model({ actual_soc: NaN, km_on_route: "viel", plan_soc: 70, reserve_at_km: Infinity,
                          next_stop: "x", arrival_shift_min: null, remaining_km: undefined }, NOW);
verify(broken && broken.soc === null && broken.stop === null && broken.reserve === null
       && broken.arrival === null && broken.rest === null && broken.short === "Keine Werte",
       "NaN, Unendlich und Text statt Zahlen werden Luecken, keine Anzeige");
const withoutSource = A.model({ ...planned, soc_source: undefined, soc_reported: false }, NOW);
verify(withoutSource.soc.source === "gerechnet",
       "aeltere Antworten ohne soc_source: nicht gemeldet heisst gerechnet");

console.log("\nSender: drosseln");
function build(opt) {
  const t = { now_ts: NOW, planned: [], sent: [] };
  // The test's distances are fixed, independent of the app's default.
  const s = A.sender({
    spacingMs: 10000,
    now_ts: () => t.now_ts,
    schedule: (f, ms) => { const e = { f, ms, active: true }; t.planned.push(e); return e; },
    remove: (e) => { if (e) e.active = false; },
    destination: (x) => { t.sent.push(x); },
    ...(opt || {}) });
  t.s = s;
  t.run = () => { const e = t.planned.filter((x) => x.active); t.planned = [];
                     e.forEach((x) => { x.active = false; x.f(); }); };
  return t;
}
const soc = (p) => ({ ...planned, actual_soc: p });

let t = build();
t.s.report(soc(72));
verify(t.sent.length === 1, "die erste Meldung geht sofort hinaus");
t.now_ts += 3000; t.s.report(soc(71));
t.now_ts += 2000; t.s.report(soc(70));
verify(t.sent.length === 1,
       "drei und fuenf Sekunden spaeter nicht - Apple erlaubt hoechstens alle zehn",
       String(t.sent.length));
verify(t.planned.filter((x) => x.active).length === 1 && t.planned[0].ms === 7000,
       "stattdessen wird die Meldung fuer die erlaubte Zeit vorgemerkt: nach der "
       + "ersten zu fruehen (3 s nach der Sendung) in 7 s, und die zweite setzt "
       + "keinen zweiten Zeitgeber",
       JSON.stringify(t.planned.map((x) => x.ms)));
t.now_ts += 5000; t.run();
verify(t.sent.length === 2 && t.sent[1].soc.percent === 70,
       "und gesendet wird das Neueste, nicht das, was zuerst wartete",
       JSON.stringify(t.sent.map((x) => x && x.soc.percent)));
t.now_ts += 12000; t.s.report(soc(69));
verify(t.sent.length === 3, "nach zwoelf Sekunden wieder sofort");

console.log("\nSender: nichts Neues");
t = build();
t.s.report(planned);
t.now_ts += 12000; t.s.report(planned);
verify(t.sent.length === 1, "unveraenderte Werte werden nicht erneut gesendet");
t.now_ts += 30000; t.s.report(planned);
verify(t.sent.length === 1, "auch nach vierzig Sekunden nicht");
t.now_ts += 25000; t.s.report(planned);
verify(t.sent.length === 2 && t.sent[1].as_of === t.now_ts,
       "aber nach einer Minute ein Herzschlag - die Anzeige zeigt ihr Alter, und "
       + "ein ruhiger Ladestand soll sie nicht alt aussehen lassen",
       String(t.sent.length));
t.now_ts += 12000; t.s.report({ ...planned, km_on_route: 100.1 });
verify(t.sent.length === 2 || t.sent.length === 3, "(Kontrolle)");
t = build();
t.s.report(planned); t.now_ts += 12000;
t.s.report({ ...planned, km_on_route: 101, next_stop: planned.next_stop });
verify(t.sent.length === 2, "ändert sich ein Wert (Entfernung), geht es hinaus");

console.log("\nSender: Ende");
t = build();
t.s.report(planned);
t.s.finish();
verify(t.sent.length === 2 && t.sent[1] === null,
       "beenden meldet null: Die Fahrt ist zu Ende, die Anzeige soll verschwinden");
t.s.finish();
verify(t.sent.length === 2, "und nur einmal");
t.now_ts += 12000; t.s.report(planned);
verify(t.sent.length === 3 && t.sent[2] !== null,
       "eine neue Fahrt beginnt eine neue Anzeige");
t = build();
t.s.report(soc(72)); t.now_ts += 2000; t.s.report(soc(71));
t.s.finish();
t.run();
verify(t.sent.length === 2 && t.sent[1] === null,
       "eine vorgemerkte Meldung kommt nach dem Ende nicht mehr: sie zeigte die Fahrt, "
       + "die vorbei ist", JSON.stringify(t.sent.map((x) => x === null ? null : x.soc.percent)));
t = build();
t.s.finish();
verify(t.sent.length === 0, "wer nie gemeldet hat, muss auch nichts beenden");

console.log("\nSender: Ziel");
t = build({ destination: null });
let ok = true;
try { t.s.report(planned); t.s.finish(); } catch (e) { ok = false; }
verify(ok, "ohne Ziel (noch kein Plugin) passiert nichts - und nichts bricht");
reports.length = 0;
t = build({ destination: () => { throw new Error("kein Plugin"); } });
ok = true;
try { for (let i = 0; i < 4; i++) { t.now_ts += 12000; t.s.report(soc(70 - i)); } } catch (e) { ok = false; }
verify(ok, "ein Ziel, das wirft, bricht die Oberflaeche nicht");
verify(reports.filter((x) => /kein Plugin/.test(x)).length === 1,
       "und der Fehler steht einmal im Protokoll, nicht bei jeder Meldung",
       String(reports.length));
t = build({ destination: () => Promise.reject(new Error("abgelehnt")) });
ok = true;
try { t.s.report(planned); } catch (e) { ok = false; }
verify(ok, "auch ein abgelehntes Versprechen bricht nichts");
t = build(); t.s.setTarget(null); t.s.report(planned);
verify(t.sent.length === 0, "das Ziel laesst sich wieder abnehmen");

console.log("\nStandard-Sender der Oberflaeche");
ok = true;
try { A.report(planned); A.finish(); A.setTarget(null); } catch (e) { ok = false; }
verify(ok && A.MIN_SPACING_MS === 15000,
       "melden, beenden und setTarget laufen ohne Ziel durch; 15 s Mindestabstand");

console.log("\nVerlauf: 1, 5, 30 und 60 Minuten");
/* A trip at 70 km/h and 15 kWh/100 km: 1.1667 km and 0.175 kWh per
 * minute, one point every twelve seconds, 70 minutes long. Then every
 * window matches to the decimal place, and the test says where the calculation deviates. */
function trip(mins, options) {
  const o = { kwh100: 15, kmh: 70, ...(options || {}) };
  const track = [];
  const end = NOW;
  const onset = end - mins * 60000;
  let km = 100, net = 50, disch = 80, chg = 3;
  for (let ms = onset; ms <= end; ms += 12000) {
    track.push({ timestamp: ms, gps: km, net, disch, chg });
    const dkm = o.kmh * 12 / 3600;
    km += dkm;
    const dkwh = dkm * o.kwh100 / 100;
    net += dkwh;
    disch += dkwh * 1.2;     // 20 % more drawn ...
    chg += dkwh * 0.2;      // ... and a sixth of that fed back
  }
  return track;
}
const as_of = (extras) => A.model(planned, NOW, extras);

let v = as_of({ track: trip(70) }).history;
verify(v && v.timeframe.map((f) => f.min).join() === "1,5,30,60",
       "vier Fenster: 1, 5, 30, 60 Minuten", JSON.stringify(v && v.timeframe.map((f) => f.min)));
verify(v.timeframe.every((f) => Math.abs(f.kwh100 - 15) < 0.15),
       "jedes zeigt 15 kWh/100 km", JSON.stringify(v.timeframe.map((f) => f.kwh100)));
verify(v.timeframe.every((f) => Math.abs(f.kw - 10.5) < 0.15),
       "und 10,5 kW (70 km/h mal 0,15 kWh/km)", JSON.stringify(v.timeframe.map((f) => f.kw)));
verify(v.timeframe[0].text === "15,0" && v.timeframe[0].kwText === "10,5",
       "Texte mit Komma, fertig fuer die Anzeige", JSON.stringify(v.timeframe[0]));
verify(v.bar.length === 6 && v.bar.every((b) => b !== null && Math.abs(b - 15) < 0.3),
       "sechs Balken zu fuenf Minuten, alle um 15", JSON.stringify(v.bar));
verify(v.trip && Math.abs(v.trip.kwh100 - 15) < 0.2 && v.trip.text === "15,0" && v.trip.km > 70,
       "Durchschnitt der ganzen Fahrt: 15 kWh/100 km ueber mehr als 70 km", JSON.stringify(v.trip));
verify(as_of({ track: trip(0.4) }).history === null || !as_of({ track: trip(0.4) }).history.trip,
       "unter einem Kilometer gibt es noch keinen Fahrt-Durchschnitt");
verify(v.regen && v.regen.percent === 17 && v.regen.mins >= 55,
       "Rekuperation: ein Sechstel der entnommenen Energie kam zurueck (17 %)",
       JSON.stringify(v.regen));

// The consumption changes: the short windows follow faster, the long ones smooth.
const expensiveThenCheap = (() => {
  const a = trip(50, { kwh100: 25 }).filter((p) => p.timestamp < NOW - 10 * 60000);
  const b = trip(10, { kwh100: 10 });
  const dkm = a[a.length - 1].gps - b[0].gps, dn = a[a.length - 1].net - b[0].net;
  return a.concat(b.map((p) => ({ ...p, gps: p.gps + dkm, net: p.net + dn })));
})();
v = as_of({ track: expensiveThenCheap }).history;
verify(v.timeframe[0].kwh100 < 11 && v.timeframe[1].kwh100 < 11
       && v.timeframe[2].kwh100 > 13 && v.timeframe[3].kwh100 > v.timeframe[2].kwh100,
       "kurze Fenster zeigen den Umschwung (10), lange glaetten ihn (um 20)",
       JSON.stringify(v.timeframe.map((f) => f.kwh100)));

// Honesty: no window without a trace behind it.
v = as_of({ track: trip(12) }).history;
verify(v.timeframe[0].kwh100 !== null && v.timeframe[1].kwh100 !== null
       && v.timeframe[2].kwh100 === null && v.timeframe[3].kwh100 === null,
       "nach zwoelf Minuten Fahrt gibt es 1 und 5, aber keinen 30- und 60-Minuten-Schnitt",
       JSON.stringify(v.timeframe.map((f) => f.kwh100)));
verify(v.timeframe[2].text === "–" && v.timeframe[3].kwText === "–",
       "und der Text ist ein Strich, keine Null");

// Standing: energy without distance - kWh/100 is not possible, kW is.
const standing = trip(10, { kmh: 0 });
standing.forEach((p, i) => { p.net = 50 + i * 0.004; });   // 1.2 kW
v = as_of({ track: standing }).history;
verify(v.timeframe[0].kwh100 === null && v.timeframe[0].kw !== null && Math.abs(v.timeframe[1].kw - 1.2) < 0.1,
       "im Stand: kein kWh/100 (keine Strecke), aber die Leistung (1,2 kW)",
       JSON.stringify(v.timeframe.slice(0, 2)));

// Trace is there (dongle gone): no "Jetzt".
const old = trip(70).filter((p) => p.timestamp < NOW - 2 * 60000);
verify(as_of({ track: old }).history === null,
       "ist der letzte Punkt zwei Minuten alt, steht kein Verlauf da - kein altes Jetzt");
const withoutGps = as_of({ track: trip(70).map((p) => ({ ...p, gps: null })) }).history;
verify(withoutGps && withoutGps.timeframe.every((f) => f.kw === null) && withoutGps.bar === null,
       "ohne GPS-Strecke keine Fenster und keine Balken, auch wenn die Zaehler da sind - "
       + "die Strecke aus dem Kilometerstand waere fuer eine Minute zu grob");
verify(as_of().history === null && as_of({ track: [] }).history === null
       && as_of({ track: [trip(1)[0]] }).history === null,
       "ohne Spur, mit leerer oder einpunktiger: null");
verify(as_of({ track: "x" }).history === null && as_of({ track: [null, null] }).history === null,
       "Unsinn statt einer Spur bricht nichts");

console.log("\nNebenverbraucher");
const now_ts = NOW;
const vals = {
  aux_load_kw: { val: 1.84, timestamp: now_ts - 20000 },
  ptc_current_a: { val: 5.0, timestamp: now_ts - 20000 },
  voltage_v: { val: 380, timestamp: now_ts - 20000 },
  compressor_w: { val: 450, timestamp: now_ts - 5 * 60000 },
  batterie_c: { val: 27.4, timestamp: now_ts - 20000 },
};
let n = as_of({ vals }).aux;
verify(n.kw === 1.8 && n.text === "1,8 kW" && n.source === "gemessen",
       "der gemessene Nebenverbrauch, mit Quelle", JSON.stringify(n));
verify(n.heatingKw === 1.9 && n.heatingText === "1,9 kW",
       "Heizung = Strom mal Packspannung (5 A mal 380 V)", JSON.stringify(n));
verify(n.climateKw === 0.5 && n.climateText === "0,5 kW",
       "Klimakompressor in kW (450 W, fuenf Minuten alt ist noch gut)", JSON.stringify(n));
verify(n.batterieC === 27 && n.batterieText === "27 °C", "Batterietemperatur");
n = as_of({ vals: { ...vals, aux_load_kw: undefined },
            aux: { kw: 0.9, timestamp: now_ts - 60000 } }).aux;
verify(n.kw === 0.9 && n.source === "geschaetzt",
       "ohne Messung gilt die Naeherung aus dem Stand - und heisst so", JSON.stringify(n));
n = as_of({ vals: { ptc_current_a: { val: 5, timestamp: now_ts } } }).aux;
verify(n === null,
       "Heizstrom ohne Packspannung ergibt keine Heizleistung - und ohne alles nichts");
n = as_of({ vals: { aux_load_kw: { val: 1.5, timestamp: now_ts - 20 * 60000 } } }).aux;
verify(n === null, "ein Wert, der zwanzig Minuten alt ist, wird nicht mehr gezeigt");
n = as_of({ vals: { aux_load_kw: { val: NaN, timestamp: now_ts }, batterie_c: { val: "warm", timestamp: now_ts } } }).aux;
verify(n === null, "NaN und Text werden Luecken");
verify(as_of().aux === null && as_of({ vals: null }).aux === null,
       "ohne Werte: null");

console.log("\nLadestopps fuer CarPlay");
const plan3 = { stops: [
  { name: "Ionity Bad Rappenau", km_on_route: 141.2, arrival_soc: 18.6, departure_soc: 80,
    charge_time_minutes: 24.6, operator: "Ionity", max_kw: 350 },
  { name: "Fastned", km_on_route: 260, arrival_soc: 15, departure_soc: 70, charge_time_minutes: 21 },
  { name: "Schon vorbei", km_on_route: 40, arrival_soc: 30, departure_soc: 80, charge_time_minutes: 20 },
  { name: "Ohne Daten", km_on_route: 300 },
] };
let sl = A.model(planned, NOW, { plan: plan3 }).stopList;
verify(sl && sl.length === 3 && sl[0].name === "Ionity Bad Rappenau",
       "die Stopps vor einem, in Fahrtrichtung; was schon hinter einem liegt, fehlt",
       JSON.stringify(sl && sl.map((x) => x.name)));
verify(sl[0].km === 41.2 && sl[0].kmText === "41 km",
       "Entfernung vom jetzigen Standort (Kilometer 100), nicht vom Start", JSON.stringify(sl[0]));
verify(sl[0].arrivalSocText === "19 %" && sl[0].departureSocText === "80 %"
       && sl[0].chargeTimeText === "25 min" && sl[0].operator === "Ionity" && sl[0].powerKw === 350,
       "Ladestand bei Ankunft und Abfahrt, Ladezeit, Betreiber, Leistung", JSON.stringify(sl[0]));
verify(sl[2].name === "Ohne Daten" && sl[2].arrivalSocText === null && sl[2].chargeTimeText === null
       && sl[2].operator === null && sl[2].powerKw === null,
       "ein Stopp ohne Angaben bekommt Luecken statt erfundener Werte", JSON.stringify(sl[2]));
const many = { stops: Array.from({ length: 20 }, (_, i) => ({ name: "S" + i, km_on_route: 110 + i * 10 })) };
verify(A.model(planned, NOW, { plan: many }).stopList.length === 8,
       "hoechstens acht - mehr passt in keine Vorlage");
verify(A.model(planned, NOW).stopList === null
       && A.model(planned, NOW, { plan: { stops: [] } }).stopList === null
       && A.model(planned, NOW, { plan: null }).stopList === null,
       "ohne Plan oder ohne Stopps: null");
verify(A.model({ actual_soc: 70 }, NOW, { plan: plan3 }).stopList === null,
       "eine Aufzeichnung ohne Plan zeigt keine Stopps, auch wenn ein alter Plan mitkommt");
verify(A.model({ ...planned, km_on_route: null }, NOW, { plan: plan3 }).stopList === null,
       "ohne Position auf der Route keine Entfernung - und deshalb keine Liste");
const large = JSON.stringify(A.model(planned, NOW, { plan: plan3 })).length;
verify(large < 3000, "das ganze Modell bleibt klein (unter 3 KB)", String(large));

console.log("\nNatives Ziel: die Live Activity");
function freshnessPage(shell) {
  const w = shell ? { joltBlePlugin: shell } : {};
  const k = { window: w, console: { log() {} }, Date, JSON, Math, Number,
              setTimeout, clearTimeout, Promise };
  vm.createContext(k);
  vm.runInContext(source, k);
  return w.joltDisplay;
}
const calls = [];
const plugin = {
  refresh: async (a) => { calls.push(["aktualisieren", a]); },
  finish: async () => { calls.push(["beenden"]); },
};
let page = freshnessPage({ JoltDisplay: plugin, Capacitor: { isNativePlatform: () => true } });
page.report(planned);
verify(calls.length === 1 && calls[0][0] === "aktualisieren"
       && typeof calls[0][1].json === "string",
       "in der iOS-App geht das Modell als JSON-Zeichenkette an das Plugin");
const sent = JSON.parse(calls[0][1].json);
verify(sent.version === 1 && sent.soc && sent.short && typeof sent.as_of === "number",
       "mit den Schluesseln, die Swift liest (soc, kurz, stand ...)", calls[0][1].json);
page.finish();
verify(calls.length === 2 && calls[1][0] === "beenden",
       "und am Fahrtende wird die Anzeige beendet");
calls.length = 0;
page = freshnessPage({ JoltDisplay: plugin, Capacitor: { isNativePlatform: () => false } });
page.report(planned);
verify(calls.length === 0, "ausserhalb der App (Browser, Bluefy) passiert nichts");
page = freshnessPage(null);
ok = true;
try { page.report(planned); page.finish(); } catch (e) { ok = false; }
verify(ok, "und ohne Plugin-Huelle laeuft alles durch");

console.log("\nKacheln fuer CarPlay: Stil und Bilder");
{
  const storage = {};
  function pageWithTiles(beforehand) {
    const aufrufe2 = [];
    const f = { localStorage: {
      getItem: (k) => (k in storage ? storage[k] : null),
      setItem: (k, v) => { storage[k] = String(v); } } };
    f.window = f;
    f.document = { createElement: () => ({
      getContext: () => new Proxy({}, { get: (z, n) => (n === "measureText" ? () => ({ width: 10 })
        : (n === "createLinearGradient" ? () => ({ addColorStop() {} }) : () => {})),
        set: () => true }),
      toDataURL: () => "data:image/png;base64,QUJD" }) };
    f.joltBlePlugin = { Capacitor: { isNativePlatform: () => true },
      JoltDisplay: { refresh: (a) => { aufrufe2.push(["aktualisieren", a]); return Promise.resolve(); },
                     finish: () => { aufrufe2.push(["beenden"]); return Promise.resolve(); } } };
    const k2 = { window: f, document: f.document, console: { log() {} }, Date, JSON, Math, Number, setTimeout,
                 clearTimeout, Promise };
    vm.createContext(k2);
    // The remembered style is read on load: prepare first, then load.
    if (beforehand) beforehand(storage);
    vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "frontend", "tiles.js"), "utf8"), k2);
    vm.runInContext(source, k2);
    return { A2: f.joltDisplay, aufrufe2, f };
  }

  // Rows
  const { A2 } = pageWithTiles();
  let r = null;
  const using = (soc, aux, regen) => ({ soc: soc === null ? null : { percent: soc },
    aux: aux === null ? null : { kw: aux },
    history: regen === null ? null : { regen: { percent: regen } } });
  r = A2.seriesListAppend(r, using(70, 1.5, 20), NOW);
  r = A2.seriesListAppend(r, using(69, 1.8, 21), NOW + 20000);
  verify(JSON.stringify(A2.seriesListExcerpt(r).soc) === "[70,69]" && A2.seriesListExcerpt(r).aux.length === 2,
         "die Reihen sammeln die Stichproben der Kacheln");
  verify(A2.seriesListExcerpt(A2.seriesListAppend(null, using(70, null, null), NOW)).soc === null,
         "ein einziger Punkt ist keine Linie");
  r = A2.seriesListAppend(r, using(60, 1, 10), NOW + 20000 + 4 * 60000);
  verify(A2.seriesListExcerpt(r).soc === null,
         "eine Luecke von ueber drei Minuten beginnt die Reihe neu - es ist eine andere Fahrt");
  let long = null;
  for (let i = 0; i < 100; i++) long = A2.seriesListAppend(long, using(50 + i / 10, 1, 1), NOW + i * 60000 * 1.5);
  const span = long.points[long.points.length - 1].timestamp - long.points[0].timestamp;
  verify(span <= 30 * 60000, "die Reihe haelt hoechstens dreissig Minuten");
  verify(A2.seriesListExcerpt(A2.seriesListAppend(null, using(null, null, null), NOW)).soc === null
         && A2.seriesListExcerpt(A2.seriesListAppend(null, null, NOW)).aux === null,
         "fehlende Werte stoeren nicht");

  // Style
  verify(A2.look() === "klassisch", "ohne Wahl gilt 'klassisch' - bisheriges Verhalten");
  verify(A2.setStyle("quatsch") === false && A2.look() === "klassisch",
         "ein unbekannter Stil wird abgelehnt");

  // Sending in the app
  let s1 = pageWithTiles();
  let gesendet1 = JSON.parse(JSON.stringify(s1.A2.withImages(m, NOW)));
  verify(gesendet1.look === "klassisch" && gesendet1.tileImages === undefined,
         "Stil 'klassisch': keine Bilder im Modell - Swift zeichnet");
  s1.A2.setStyle("a");
  gesendet1 = JSON.parse(JSON.stringify(s1.A2.withImages(m, NOW + 20000)));
  const placeNames = ["soc", "arrival", "reserve", "consumption", "aux", "regen", "stops"];
  verify(gesendet1.look === "a" && placeNames.every((n) => typeof gesendet1.tileImages[n] === "string"),
         "Stil 'a': ein Bild je Kachelplatz im Modell", JSON.stringify(Object.keys(gesendet1.tileImages || {})));
  verify(storage["jolt-carplay-stil"] === "a", "die Wahl wird auf dem Geraet gemerkt");
  verify(s1.A2.look() === "a", "und gilt");
  const s2 = pageWithTiles((sp) => { sp["jolt-carplay-stil"] = "b"; });
  verify(s2.A2.look() === "b", "nach einem Neuladen gilt die gemerkte Wahl");

  // On a switch, it is sent again right away
  const s3 = pageWithTiles();
  s3.A2.report(planned);                       // goes out first
  const earlier = s3.aufrufe2.length;
  s3.A2.setStyle("b");
  verify(s3.aufrufe2.length === earlier + 1 && JSON.parse(s3.aufrufe2[earlier][1].json).look === "b",
         "ein Wechsel des Stils schickt die Anzeige sofort neu - sichtbar im Auto, nicht erst nach der naechsten Meldung");
  const withB = JSON.parse(s3.aufrufe2[earlier][1].json);
  verify(withB.soc && withB.short && Object.keys(withB.tileImages).length === 7,
         "mit dem vollen Modell und sieben Bildern");
  verify(JSON.stringify(A.model(planned, NOW)).indexOf("tileImages") === -1,
         "das Modell des Senders traegt keine Bilder: Der Vergleich 'hat sich etwas geaendert' haengt nicht an Pixeln");
}

console.log(failure ? `\n${failure} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
process.exit(failure ? 1 : 0);
