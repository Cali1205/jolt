#!/usr/bin/env node
// Prueft das Anzeigemodell (frontend/anzeige.js): den Zustand der Fahrt als
// wenige Zahlen und Texte fuer eine Live Activity, ein Widget oder eine
// CarPlay-Vorlage - ohne Swift, ohne Mac, ohne Apple.
//
// Geprueft wird vor allem, was in einem Auto schaden koennte: ein erfundenes
// Feld (ein Ladestopp bei einer Aufzeichnung ohne Plan), ein falscher Abstand
// (die Reserve liegt als Position auf der Route, nicht als Entfernung), und
// ein Sender, der oefter meldet, als Apple es erlaubt.
//
//     node tools/check_anzeige.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const quelle = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "anzeige.js"), "utf8");

let fehler = 0;
function pruefe(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) fehler++;
}

const fenster = {};
const meldungen = [];
const kontext = { window: fenster, console: { log: (...a) => meldungen.push(a.join(" ")) },
                  Date, JSON, Math, Number, setTimeout, clearTimeout, Promise };
vm.createContext(kontext);
vm.runInContext(quelle, kontext);
const A = fenster.joltAnzeige;

const JETZT = 1_800_000_000_000;

// Ein Zustand, wie ihn /api/live/{id} liefert: geplante Fahrt, unterwegs.
const geplant = {
  km_auf_route: 100.0, lat: 48.5, lon: 9.1,
  ist_soc: 72.1, soc_gemeldet: true, soc_quelle: "gemessen", soll_soc: 73.0,
  abweichung_pp: -0.9, rest_km: 87.4, prognose_soc_am_ziel: 21.0,
  reserve_bei_km: 160.0, ankunft_verschiebung_min: 12.3,
  naechster_stopp: { id: 5, name: "Ionity Bad Rappenau", km_auf_route: 141.2,
                     geplant_soc: 19.0, erwartet_soc: 17.6 },
};

console.log("\nGeplante Fahrt");
let m = A.modell(geplant, JETZT);
pruefe(m.version === 1 && m.stand === JETZT, "Version und Stand sind dabei");
pruefe(m.soc.text === "72 %" && m.soc.prozent === 72.1 && m.soc.quelle === "gemessen",
       "Ladestand gerundet, mit Quelle", JSON.stringify(m.soc));
pruefe(m.reserve.km === 60 && m.reserve.text === "60 km",
       "Reserve in 60 km - die Differenz aus Position und Reserve-Marke, nicht "
       + "die Marke selbst (160)", JSON.stringify(m.reserve));
pruefe(m.stopp.name === "Ionity Bad Rappenau" && m.stopp.km === 41.2
       && m.stopp.kmText === "41 km",
       "der Stopp in 41 km, wieder als Differenz (141,2 minus 100)", JSON.stringify(m.stopp));
pruefe(m.stopp.ankunftSoc === 18 && m.stopp.ankunftSocText === "18 %"
       && m.stopp.geplant === false,
       "mit dem erwarteten Ladestand (17,6), nicht dem geplanten (19)", JSON.stringify(m.stopp));
pruefe(m.ankunft.text === "+12 min" && m.ankunft.min === 12, "Ankunft +12 min",
       JSON.stringify(m.ankunft));
pruefe(m.rest.text === "87 km", "Rest 87 km", JSON.stringify(m.rest));
pruefe(m.kurz === "72 % · Stopp in 41 km (18 %)",
       "die Kurzzeile fuer die kleinste Anzeige", m.kurz);

console.log("\nAufzeichnung ohne Plan");
const aufzeichnung = { km_auf_route: 0, lat: 48.5, lon: 9.1, ist_soc: 79.2,
                       soc_gemeldet: false, soc_quelle: "zuletzt", soll_soc: null,
                       abweichung_pp: null, rest_km: 0, prognose_soc_am_ziel: null,
                       reserve_bei_km: null, ankunft_verschiebung_min: null,
                       naechster_stopp: null };
m = A.modell(aufzeichnung, JETZT);
pruefe(m.soc.text === "79 %" && m.soc.quelle === "zuletzt",
       "der Ladestand ist da, als zuletzt gemessen gekennzeichnet", JSON.stringify(m.soc));
pruefe(m.stopp === null && m.ankunft === null && m.reserve === null && m.rest === null,
       "Stopp, Ankunft, Reserve und Rest fehlen - kein erfundenes Feld");
pruefe(m.rest === null, "und \"0 km Rest\" ist keine Aussage, sondern eine Luecke");
pruefe(m.kurz === "79 %", "die Kurzzeile zeigt nur, was da ist", m.kurz);
const nurGps = A.modell({ ...aufzeichnung, ist_soc: null, soc_quelle: "gemessen" }, JETZT);
pruefe(nurGps.soc === null && nurGps.kurz === "Keine Werte",
       "ohne Ladestand steht dort \"Keine Werte\" - kein 0 %, kein 100 %", nurGps.kurz);

console.log("\nAnkunft");
const ank = (min) => A.modell({ ...geplant, ankunft_verschiebung_min: min }, JETZT).ankunft.text;
pruefe(ank(0.4) === "nach Plan" && ank(-0.4) === "nach Plan", "unter einer halben Minute: nach Plan");
pruefe(ank(12.3) === "+12 min", "zu spaet");
pruefe(ank(-5) === "−5 min", "zu frueh mit echtem Minuszeichen", ank(-5));
pruefe(ank(75) === "+1 h 15", "ueber einer Stunde in Stunden", ank(75));
pruefe(ank(65) === "+1 h 05", "mit fuehrender Null bei den Minuten", ank(65));

console.log("\nEntfernungen");
const stopp = (km) => A.modell({ ...geplant, naechster_stopp: { ...geplant.naechster_stopp,
                                  km_auf_route: 100 + km } }, JETZT).stopp.kmText;
pruefe(stopp(0.4) === "gleich", "unter einem Kilometer: gleich", stopp(0.4));
pruefe(stopp(7.34) === "7,3 km", "unter zehn mit einer Nachkommastelle", stopp(7.34));
pruefe(stopp(41.2) === "41 km", "darueber ganz", stopp(41.2));
const zuRueck = A.modell({ ...geplant, reserve_bei_km: 90 }, JETZT);
pruefe(zuRueck.reserve === null,
       "eine Reserve-Marke hinter der Position ist keine Reichweite - sie fehlt");
const stoppHinten = A.modell({ ...geplant, naechster_stopp: { ...geplant.naechster_stopp,
                                km_auf_route: 80 } }, JETZT);
pruefe(stoppHinten.stopp === null, "ein Stopp, den man schon passiert hat, wird nicht gezeigt");

console.log("\nFehlende und kaputte Angaben");
const ohneSoc = A.modell({ ...geplant, naechster_stopp: { name: "X", km_auf_route: 130,
                          geplant_soc: null, erwartet_soc: null } }, JETZT);
pruefe(ohneSoc.stopp.ankunftSoc === null && ohneSoc.stopp.ankunftSocText === null
       && ohneSoc.kurz === "72 % · Stopp in 30 km",
       "ein Stopp ohne Ladestand: ohne Klammer, nicht \"(null %)\"", ohneSoc.kurz);
const nurGeplant = A.modell({ ...geplant, naechster_stopp: { ...geplant.naechster_stopp,
                              erwartet_soc: null } }, JETZT);
pruefe(nurGeplant.stopp.ankunftSoc === 19 && nurGeplant.stopp.geplant === true,
       "fehlt der erwartete Wert, gilt der geplante - und das steht dabei");
const ohneName = A.modell({ ...geplant, naechster_stopp: { km_auf_route: 130 } }, JETZT);
pruefe(ohneName.stopp.name === "Ladestopp", "ohne Namen steht \"Ladestopp\"");
for (const unsinn of [null, undefined, 5, "x", [], 0]) {
  pruefe(A.modell(unsinn, JETZT) === null, "kein Zustand ergibt kein Modell: " + JSON.stringify(unsinn));
}
const kaputt = A.modell({ ist_soc: NaN, km_auf_route: "viel", soll_soc: 70, reserve_bei_km: Infinity,
                          naechster_stopp: "x", ankunft_verschiebung_min: null, rest_km: undefined }, JETZT);
pruefe(kaputt && kaputt.soc === null && kaputt.stopp === null && kaputt.reserve === null
       && kaputt.ankunft === null && kaputt.rest === null && kaputt.kurz === "Keine Werte",
       "NaN, Unendlich und Text statt Zahlen werden Luecken, keine Anzeige");
const ohneQuelle = A.modell({ ...geplant, soc_quelle: undefined, soc_gemeldet: false }, JETZT);
pruefe(ohneQuelle.soc.quelle === "gerechnet",
       "aeltere Antworten ohne soc_quelle: nicht gemeldet heisst gerechnet");

console.log("\nSender: drosseln");
function bauen(opt) {
  const t = { jetzt: JETZT, geplant: [], gesendet: [] };
  // Die Abstaende des Tests stehen fest, unabhaengig vom Vorgabewert der App.
  const s = A.sender({
    abstandMs: 10000,
    jetzt: () => t.jetzt,
    planen: (f, ms) => { const e = { f, ms, aktiv: true }; t.geplant.push(e); return e; },
    loeschen: (e) => { if (e) e.aktiv = false; },
    ziel: (x) => { t.gesendet.push(x); },
    ...(opt || {}) });
  t.s = s;
  t.laufen = () => { const e = t.geplant.filter((x) => x.aktiv); t.geplant = [];
                     e.forEach((x) => { x.aktiv = false; x.f(); }); };
  return t;
}
const soc = (p) => ({ ...geplant, ist_soc: p });

let t = bauen();
t.s.melden(soc(72));
pruefe(t.gesendet.length === 1, "die erste Meldung geht sofort hinaus");
t.jetzt += 3000; t.s.melden(soc(71));
t.jetzt += 2000; t.s.melden(soc(70));
pruefe(t.gesendet.length === 1,
       "drei und fuenf Sekunden spaeter nicht - Apple erlaubt hoechstens alle zehn",
       String(t.gesendet.length));
pruefe(t.geplant.filter((x) => x.aktiv).length === 1 && t.geplant[0].ms === 7000,
       "stattdessen wird die Meldung fuer die erlaubte Zeit vorgemerkt: nach der "
       + "ersten zu fruehen (3 s nach der Sendung) in 7 s, und die zweite setzt "
       + "keinen zweiten Zeitgeber",
       JSON.stringify(t.geplant.map((x) => x.ms)));
t.jetzt += 5000; t.laufen();
pruefe(t.gesendet.length === 2 && t.gesendet[1].soc.prozent === 70,
       "und gesendet wird das Neueste, nicht das, was zuerst wartete",
       JSON.stringify(t.gesendet.map((x) => x && x.soc.prozent)));
t.jetzt += 12000; t.s.melden(soc(69));
pruefe(t.gesendet.length === 3, "nach zwoelf Sekunden wieder sofort");

console.log("\nSender: nichts Neues");
t = bauen();
t.s.melden(geplant);
t.jetzt += 12000; t.s.melden(geplant);
pruefe(t.gesendet.length === 1, "unveraenderte Werte werden nicht erneut gesendet");
t.jetzt += 30000; t.s.melden(geplant);
pruefe(t.gesendet.length === 1, "auch nach vierzig Sekunden nicht");
t.jetzt += 25000; t.s.melden(geplant);
pruefe(t.gesendet.length === 2 && t.gesendet[1].stand === t.jetzt,
       "aber nach einer Minute ein Herzschlag - die Anzeige zeigt ihr Alter, und "
       + "ein ruhiger Ladestand soll sie nicht alt aussehen lassen",
       String(t.gesendet.length));
t.jetzt += 12000; t.s.melden({ ...geplant, km_auf_route: 100.1 });
pruefe(t.gesendet.length === 2 || t.gesendet.length === 3, "(Kontrolle)");
t = bauen();
t.s.melden(geplant); t.jetzt += 12000;
t.s.melden({ ...geplant, km_auf_route: 101, naechster_stopp: geplant.naechster_stopp });
pruefe(t.gesendet.length === 2, "ändert sich ein Wert (Entfernung), geht es hinaus");

console.log("\nSender: Ende");
t = bauen();
t.s.melden(geplant);
t.s.beenden();
pruefe(t.gesendet.length === 2 && t.gesendet[1] === null,
       "beenden meldet null: Die Fahrt ist zu Ende, die Anzeige soll verschwinden");
t.s.beenden();
pruefe(t.gesendet.length === 2, "und nur einmal");
t.jetzt += 12000; t.s.melden(geplant);
pruefe(t.gesendet.length === 3 && t.gesendet[2] !== null,
       "eine neue Fahrt beginnt eine neue Anzeige");
t = bauen();
t.s.melden(soc(72)); t.jetzt += 2000; t.s.melden(soc(71));
t.s.beenden();
t.laufen();
pruefe(t.gesendet.length === 2 && t.gesendet[1] === null,
       "eine vorgemerkte Meldung kommt nach dem Ende nicht mehr: sie zeigte die Fahrt, "
       + "die vorbei ist", JSON.stringify(t.gesendet.map((x) => x === null ? null : x.soc.prozent)));
t = bauen();
t.s.beenden();
pruefe(t.gesendet.length === 0, "wer nie gemeldet hat, muss auch nichts beenden");

console.log("\nSender: Ziel");
t = bauen({ ziel: null });
let ok = true;
try { t.s.melden(geplant); t.s.beenden(); } catch (e) { ok = false; }
pruefe(ok, "ohne Ziel (noch kein Plugin) passiert nichts - und nichts bricht");
meldungen.length = 0;
t = bauen({ ziel: () => { throw new Error("kein Plugin"); } });
ok = true;
try { for (let i = 0; i < 4; i++) { t.jetzt += 12000; t.s.melden(soc(70 - i)); } } catch (e) { ok = false; }
pruefe(ok, "ein Ziel, das wirft, bricht die Oberflaeche nicht");
pruefe(meldungen.filter((x) => /kein Plugin/.test(x)).length === 1,
       "und der Fehler steht einmal im Protokoll, nicht bei jeder Meldung",
       String(meldungen.length));
t = bauen({ ziel: () => Promise.reject(new Error("abgelehnt")) });
ok = true;
try { t.s.melden(geplant); } catch (e) { ok = false; }
pruefe(ok, "auch ein abgelehntes Versprechen bricht nichts");
t = bauen(); t.s.zielSetzen(null); t.s.melden(geplant);
pruefe(t.gesendet.length === 0, "das Ziel laesst sich wieder abnehmen");

console.log("\nStandard-Sender der Oberflaeche");
ok = true;
try { A.melden(geplant); A.beenden(); A.zielSetzen(null); } catch (e) { ok = false; }
pruefe(ok && A.MIN_ABSTAND_MS === 15000,
       "melden, beenden und zielSetzen laufen ohne Ziel durch; 15 s Mindestabstand");

console.log("\nVerlauf: 1, 5, 30 und 60 Minuten");
/* Eine Fahrt mit 70 km/h und 15 kWh/100 km: 1,1667 km und 0,175 kWh je
 * Minute, ein Punkt alle zwölf Sekunden, 70 Minuten lang. Danach stimmt jedes
 * Fenster auf die Nachkommastelle, und der Test sagt, wo die Rechnung abweicht. */
function fahrt(minuten, optionen) {
  const o = { kwh100: 15, kmh: 70, ...(optionen || {}) };
  const spur = [];
  const ende = JETZT;
  const beginn = ende - minuten * 60000;
  let km = 100, netto = 50, entl = 80, gel = 3;
  for (let ms = beginn; ms <= ende; ms += 12000) {
    spur.push({ zeit: ms, gps: km, netto, entl, gel });
    const dkm = o.kmh * 12 / 3600;
    km += dkm;
    const dkwh = dkm * o.kwh100 / 100;
    netto += dkwh;
    entl += dkwh * 1.2;     // 20 % mehr entnommen ...
    gel += dkwh * 0.2;      // ... und ein Sechstel davon zurueckgespeist
  }
  return spur;
}
const stand = (extras) => A.modell(geplant, JETZT, extras);

let v = stand({ spur: fahrt(70) }).verlauf;
pruefe(v && v.fenster.map((f) => f.min).join() === "1,5,30,60",
       "vier Fenster: 1, 5, 30, 60 Minuten", JSON.stringify(v && v.fenster.map((f) => f.min)));
pruefe(v.fenster.every((f) => Math.abs(f.kwh100 - 15) < 0.15),
       "jedes zeigt 15 kWh/100 km", JSON.stringify(v.fenster.map((f) => f.kwh100)));
pruefe(v.fenster.every((f) => Math.abs(f.kw - 10.5) < 0.15),
       "und 10,5 kW (70 km/h mal 0,15 kWh/km)", JSON.stringify(v.fenster.map((f) => f.kw)));
pruefe(v.fenster[0].text === "15,0" && v.fenster[0].kwText === "10,5",
       "Texte mit Komma, fertig fuer die Anzeige", JSON.stringify(v.fenster[0]));
pruefe(v.balken.length === 6 && v.balken.every((b) => b !== null && Math.abs(b - 15) < 0.3),
       "sechs Balken zu fuenf Minuten, alle um 15", JSON.stringify(v.balken));
pruefe(v.rekup && v.rekup.prozent === 17 && v.rekup.minuten >= 55,
       "Rekuperation: ein Sechstel der entnommenen Energie kam zurueck (17 %)",
       JSON.stringify(v.rekup));

// Der Verbrauch aendert sich: die kurzen Fenster folgen schneller, die langen glaetten.
const teuerDannBillig = (() => {
  const a = fahrt(50, { kwh100: 25 }).filter((p) => p.zeit < JETZT - 10 * 60000);
  const b = fahrt(10, { kwh100: 10 });
  const dkm = a[a.length - 1].gps - b[0].gps, dn = a[a.length - 1].netto - b[0].netto;
  return a.concat(b.map((p) => ({ ...p, gps: p.gps + dkm, netto: p.netto + dn })));
})();
v = stand({ spur: teuerDannBillig }).verlauf;
pruefe(v.fenster[0].kwh100 < 11 && v.fenster[1].kwh100 < 11
       && v.fenster[2].kwh100 > 13 && v.fenster[3].kwh100 > v.fenster[2].kwh100,
       "kurze Fenster zeigen den Umschwung (10), lange glaetten ihn (um 20)",
       JSON.stringify(v.fenster.map((f) => f.kwh100)));

// Ehrlichkeit: kein Fenster ohne Spur dahinter.
v = stand({ spur: fahrt(12) }).verlauf;
pruefe(v.fenster[0].kwh100 !== null && v.fenster[1].kwh100 !== null
       && v.fenster[2].kwh100 === null && v.fenster[3].kwh100 === null,
       "nach zwoelf Minuten Fahrt gibt es 1 und 5, aber keinen 30- und 60-Minuten-Schnitt",
       JSON.stringify(v.fenster.map((f) => f.kwh100)));
pruefe(v.fenster[2].text === "–" && v.fenster[3].kwText === "–",
       "und der Text ist ein Strich, keine Null");

// Stand: Energie ohne Strecke - kWh/100 geht nicht, kW schon.
const stehend = fahrt(10, { kmh: 0 });
stehend.forEach((p, i) => { p.netto = 50 + i * 0.004; });   // 1,2 kW
v = stand({ spur: stehend }).verlauf;
pruefe(v.fenster[0].kwh100 === null && v.fenster[0].kw !== null && Math.abs(v.fenster[1].kw - 1.2) < 0.1,
       "im Stand: kein kWh/100 (keine Strecke), aber die Leistung (1,2 kW)",
       JSON.stringify(v.fenster.slice(0, 2)));

// Spur steht (Dongle weg): kein "Jetzt".
const alt = fahrt(70).filter((p) => p.zeit < JETZT - 2 * 60000);
pruefe(stand({ spur: alt }).verlauf === null,
       "ist der letzte Punkt zwei Minuten alt, steht kein Verlauf da - kein altes Jetzt");
const ohneGps = stand({ spur: fahrt(70).map((p) => ({ ...p, gps: null })) }).verlauf;
pruefe(ohneGps && ohneGps.fenster.every((f) => f.kw === null) && ohneGps.balken === null,
       "ohne GPS-Strecke keine Fenster und keine Balken, auch wenn die Zaehler da sind - "
       + "die Strecke aus dem Kilometerstand waere fuer eine Minute zu grob");
pruefe(stand().verlauf === null && stand({ spur: [] }).verlauf === null
       && stand({ spur: [fahrt(1)[0]] }).verlauf === null,
       "ohne Spur, mit leerer oder einpunktiger: null");
pruefe(stand({ spur: "x" }).verlauf === null && stand({ spur: [null, null] }).verlauf === null,
       "Unsinn statt einer Spur bricht nichts");

console.log("\nNebenverbraucher");
const jetzt = JETZT;
const werte = {
  nebenverbrauch_kw: { wert: 1.84, zeit: jetzt - 20000 },
  ptc_strom_a: { wert: 5.0, zeit: jetzt - 20000 },
  spannung_v: { wert: 380, zeit: jetzt - 20000 },
  kompressor_w: { wert: 450, zeit: jetzt - 5 * 60000 },
  batterie_c: { wert: 27.4, zeit: jetzt - 20000 },
};
let n = stand({ werte }).neben;
pruefe(n.kw === 1.8 && n.text === "1,8 kW" && n.quelle === "gemessen",
       "der gemessene Nebenverbrauch, mit Quelle", JSON.stringify(n));
pruefe(n.heizungKw === 1.9 && n.heizungText === "1,9 kW",
       "Heizung = Strom mal Packspannung (5 A mal 380 V)", JSON.stringify(n));
pruefe(n.klimaKw === 0.5 && n.klimaText === "0,5 kW",
       "Klimakompressor in kW (450 W, fuenf Minuten alt ist noch gut)", JSON.stringify(n));
pruefe(n.batterieC === 27 && n.batterieText === "27 °C", "Batterietemperatur");
n = stand({ werte: { ...werte, nebenverbrauch_kw: undefined },
            neben: { kw: 0.9, zeit: jetzt - 60000 } }).neben;
pruefe(n.kw === 0.9 && n.quelle === "geschaetzt",
       "ohne Messung gilt die Naeherung aus dem Stand - und heisst so", JSON.stringify(n));
n = stand({ werte: { ptc_strom_a: { wert: 5, zeit: jetzt } } }).neben;
pruefe(n === null,
       "Heizstrom ohne Packspannung ergibt keine Heizleistung - und ohne alles nichts");
n = stand({ werte: { nebenverbrauch_kw: { wert: 1.5, zeit: jetzt - 20 * 60000 } } }).neben;
pruefe(n === null, "ein Wert, der zwanzig Minuten alt ist, wird nicht mehr gezeigt");
n = stand({ werte: { nebenverbrauch_kw: { wert: NaN, zeit: jetzt }, batterie_c: { wert: "warm", zeit: jetzt } } }).neben;
pruefe(n === null, "NaN und Text werden Luecken");
pruefe(stand().neben === null && stand({ werte: null }).neben === null,
       "ohne Werte: null");

console.log("\nLadestopps fuer CarPlay");
const plan3 = { stopps: [
  { name: "Ionity Bad Rappenau", km_auf_route: 141.2, ankunft_soc: 18.6, abfahrt_soc: 80,
    ladezeit_minuten: 24.6, betreiber: "Ionity", max_kw: 350 },
  { name: "Fastned", km_auf_route: 260, ankunft_soc: 15, abfahrt_soc: 70, ladezeit_minuten: 21 },
  { name: "Schon vorbei", km_auf_route: 40, ankunft_soc: 30, abfahrt_soc: 80, ladezeit_minuten: 20 },
  { name: "Ohne Daten", km_auf_route: 300 },
] };
let sl = A.modell(geplant, JETZT, { plan: plan3 }).stoppListe;
pruefe(sl && sl.length === 3 && sl[0].name === "Ionity Bad Rappenau",
       "die Stopps vor einem, in Fahrtrichtung; was schon hinter einem liegt, fehlt",
       JSON.stringify(sl && sl.map((x) => x.name)));
pruefe(sl[0].km === 41.2 && sl[0].kmText === "41 km",
       "Entfernung vom jetzigen Standort (Kilometer 100), nicht vom Start", JSON.stringify(sl[0]));
pruefe(sl[0].ankunftSocText === "19 %" && sl[0].abfahrtSocText === "80 %"
       && sl[0].ladezeitText === "25 min" && sl[0].betreiber === "Ionity" && sl[0].leistungKw === 350,
       "Ladestand bei Ankunft und Abfahrt, Ladezeit, Betreiber, Leistung", JSON.stringify(sl[0]));
pruefe(sl[2].name === "Ohne Daten" && sl[2].ankunftSocText === null && sl[2].ladezeitText === null
       && sl[2].betreiber === null && sl[2].leistungKw === null,
       "ein Stopp ohne Angaben bekommt Luecken statt erfundener Werte", JSON.stringify(sl[2]));
const viele = { stopps: Array.from({ length: 20 }, (_, i) => ({ name: "S" + i, km_auf_route: 110 + i * 10 })) };
pruefe(A.modell(geplant, JETZT, { plan: viele }).stoppListe.length === 8,
       "hoechstens acht - mehr passt in keine Vorlage");
pruefe(A.modell(geplant, JETZT).stoppListe === null
       && A.modell(geplant, JETZT, { plan: { stopps: [] } }).stoppListe === null
       && A.modell(geplant, JETZT, { plan: null }).stoppListe === null,
       "ohne Plan oder ohne Stopps: null");
pruefe(A.modell({ ist_soc: 70 }, JETZT, { plan: plan3 }).stoppListe === null,
       "eine Aufzeichnung ohne Plan zeigt keine Stopps, auch wenn ein alter Plan mitkommt");
pruefe(A.modell({ ...geplant, km_auf_route: null }, JETZT, { plan: plan3 }).stoppListe === null,
       "ohne Position auf der Route keine Entfernung - und deshalb keine Liste");
const grosse = JSON.stringify(A.modell(geplant, JETZT, { plan: plan3 })).length;
pruefe(grosse < 3000, "das ganze Modell bleibt klein (unter 3 KB)", String(grosse));

console.log("\nNatives Ziel: die Live Activity");
function frischeSeite(huelle) {
  const w = huelle ? { joltBlePlugin: huelle } : {};
  const k = { window: w, console: { log() {} }, Date, JSON, Math, Number,
              setTimeout, clearTimeout, Promise };
  vm.createContext(k);
  vm.runInContext(quelle, k);
  return w.joltAnzeige;
}
const aufrufe = [];
const plugin = {
  aktualisieren: async (a) => { aufrufe.push(["aktualisieren", a]); },
  beenden: async () => { aufrufe.push(["beenden"]); },
};
let seite = frischeSeite({ JoltAnzeige: plugin, Capacitor: { isNativePlatform: () => true } });
seite.melden(geplant);
pruefe(aufrufe.length === 1 && aufrufe[0][0] === "aktualisieren"
       && typeof aufrufe[0][1].json === "string",
       "in der iOS-App geht das Modell als JSON-Zeichenkette an das Plugin");
const gesendet = JSON.parse(aufrufe[0][1].json);
pruefe(gesendet.version === 1 && gesendet.soc && gesendet.kurz && typeof gesendet.stand === "number",
       "mit den Schluesseln, die Swift liest (soc, kurz, stand ...)", aufrufe[0][1].json);
seite.beenden();
pruefe(aufrufe.length === 2 && aufrufe[1][0] === "beenden",
       "und am Fahrtende wird die Anzeige beendet");
aufrufe.length = 0;
seite = frischeSeite({ JoltAnzeige: plugin, Capacitor: { isNativePlatform: () => false } });
seite.melden(geplant);
pruefe(aufrufe.length === 0, "ausserhalb der App (Browser, Bluefy) passiert nichts");
seite = frischeSeite(null);
ok = true;
try { seite.melden(geplant); seite.beenden(); } catch (e) { ok = false; }
pruefe(ok, "und ohne Plugin-Huelle laeuft alles durch");

console.log("\nKacheln fuer CarPlay: Stil und Bilder");
{
  const speicher = {};
  function seiteMitKacheln(vorab) {
    const aufrufe2 = [];
    const f = { localStorage: {
      getItem: (k) => (k in speicher ? speicher[k] : null),
      setItem: (k, v) => { speicher[k] = String(v); } } };
    f.window = f;
    f.document = { createElement: () => ({
      getContext: () => new Proxy({}, { get: (z, n) => (n === "measureText" ? () => ({ width: 10 })
        : (n === "createLinearGradient" ? () => ({ addColorStop() {} }) : () => {})),
        set: () => true }),
      toDataURL: () => "data:image/png;base64,QUJD" }) };
    f.joltBlePlugin = { Capacitor: { isNativePlatform: () => true },
      JoltAnzeige: { aktualisieren: (a) => { aufrufe2.push(["aktualisieren", a]); return Promise.resolve(); },
                     beenden: () => { aufrufe2.push(["beenden"]); return Promise.resolve(); } } };
    const k2 = { window: f, document: f.document, console: { log() {} }, Date, JSON, Math, Number, setTimeout,
                 clearTimeout, Promise };
    vm.createContext(k2);
    // Der gemerkte Stil wird beim Laden gelesen: erst vorbereiten, dann laden.
    if (vorab) vorab(speicher);
    vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "frontend", "kacheln.js"), "utf8"), k2);
    vm.runInContext(quelle, k2);
    return { A2: f.joltAnzeige, aufrufe2, f };
  }

  // Reihen
  const { A2 } = seiteMitKacheln();
  let r = null;
  const mit = (soc, neben, rekup) => ({ soc: soc === null ? null : { prozent: soc },
    neben: neben === null ? null : { kw: neben },
    verlauf: rekup === null ? null : { rekup: { prozent: rekup } } });
  r = A2.reihenAnfuegen(r, mit(70, 1.5, 20), JETZT);
  r = A2.reihenAnfuegen(r, mit(69, 1.8, 21), JETZT + 20000);
  pruefe(JSON.stringify(A2.reihenAuszug(r).soc) === "[70,69]" && A2.reihenAuszug(r).neben.length === 2,
         "die Reihen sammeln die Stichproben der Kacheln");
  pruefe(A2.reihenAuszug(A2.reihenAnfuegen(null, mit(70, null, null), JETZT)).soc === null,
         "ein einziger Punkt ist keine Linie");
  r = A2.reihenAnfuegen(r, mit(60, 1, 10), JETZT + 20000 + 4 * 60000);
  pruefe(A2.reihenAuszug(r).soc === null,
         "eine Luecke von ueber drei Minuten beginnt die Reihe neu - es ist eine andere Fahrt");
  let lang = null;
  for (let i = 0; i < 100; i++) lang = A2.reihenAnfuegen(lang, mit(50 + i / 10, 1, 1), JETZT + i * 60000 * 1.5);
  const spanne = lang.punkte[lang.punkte.length - 1].zeit - lang.punkte[0].zeit;
  pruefe(spanne <= 30 * 60000, "die Reihe haelt hoechstens dreissig Minuten");
  pruefe(A2.reihenAuszug(A2.reihenAnfuegen(null, mit(null, null, null), JETZT)).soc === null
         && A2.reihenAuszug(A2.reihenAnfuegen(null, null, JETZT)).neben === null,
         "fehlende Werte stoeren nicht");

  // Stil
  pruefe(A2.stil() === "klassisch", "ohne Wahl gilt 'klassisch' - bisheriges Verhalten");
  pruefe(A2.stilSetzen("quatsch") === false && A2.stil() === "klassisch",
         "ein unbekannter Stil wird abgelehnt");

  // Senden in der App
  let s1 = seiteMitKacheln();
  let gesendet1 = JSON.parse(JSON.stringify(s1.A2.mitBildern(m, JETZT)));
  pruefe(gesendet1.stil === "klassisch" && gesendet1.kachelBilder === undefined,
         "Stil 'klassisch': keine Bilder im Modell - Swift zeichnet");
  s1.A2.stilSetzen("a");
  gesendet1 = JSON.parse(JSON.stringify(s1.A2.mitBildern(m, JETZT + 20000)));
  const platzNamen = ["soc", "ankunft", "reserve", "verbrauch", "neben", "rekup", "stopps"];
  pruefe(gesendet1.stil === "a" && platzNamen.every((n) => typeof gesendet1.kachelBilder[n] === "string"),
         "Stil 'a': ein Bild je Kachelplatz im Modell", JSON.stringify(Object.keys(gesendet1.kachelBilder || {})));
  pruefe(speicher["jolt-carplay-stil"] === "a", "die Wahl wird auf dem Geraet gemerkt");
  pruefe(s1.A2.stil() === "a", "und gilt");
  const s2 = seiteMitKacheln((sp) => { sp["jolt-carplay-stil"] = "b"; });
  pruefe(s2.A2.stil() === "b", "nach einem Neuladen gilt die gemerkte Wahl");

  // Beim Wechsel wird gleich neu gesendet
  const s3 = seiteMitKacheln();
  s3.A2.melden(geplant);                       // geht als erstes hinaus
  const vorher = s3.aufrufe2.length;
  s3.A2.stilSetzen("b");
  pruefe(s3.aufrufe2.length === vorher + 1 && JSON.parse(s3.aufrufe2[vorher][1].json).stil === "b",
         "ein Wechsel des Stils schickt die Anzeige sofort neu - sichtbar im Auto, nicht erst nach der naechsten Meldung");
  const mitB = JSON.parse(s3.aufrufe2[vorher][1].json);
  pruefe(mitB.soc && mitB.kurz && Object.keys(mitB.kachelBilder).length === 7,
         "mit dem vollen Modell und sieben Bildern");
  pruefe(JSON.stringify(A.modell(geplant, JETZT)).indexOf("kachelBilder") === -1,
         "das Modell des Senders traegt keine Bilder: Der Vergleich 'hat sich etwas geaendert' haengt nicht an Pixeln");
}

console.log(fehler ? `\n${fehler} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
process.exit(fehler ? 1 : 0);
