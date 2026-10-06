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
  const s = A.sender({
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
pruefe(ok && A.MIN_ABSTAND_MS === 10000,
       "melden, beenden und zielSetzen laufen ohne Ziel durch; 10 s Mindestabstand");

console.log(fehler ? `\n${fehler} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
process.exit(fehler ? 1 : 0);
