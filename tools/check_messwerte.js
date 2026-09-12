#!/usr/bin/env node
/* Die Tabelle muss dieselben Zahlen liefern wie die Funktionen davor.
 *
 * Bis zu diesem Umbau stand jede Umrechnung als eigene Funktion in
 * obd-kern.js. Jetzt steht sie als Zeile in messwerte.js, und ein
 * Interpreter setzt sie um. Genau bei so einer Umstellung gehen Vorzeichen,
 * Skalierung und Bytereihenfolge still daneben - `soc_roh` ist ein Byte
 * geteilt durch 2,5, der Batteriestrom `(Rohwert - 150000)/100` ueber vier
 * Bytes, und beide saehen auch falsch noch plausibel aus.
 *
 * Deshalb stehen die **alten Funktionen unveraendert hier drin** und
 * dienen als Vorgabe. Verglichen wird ueber zufaellige und gezielt
 * gewaehlte Bytefolgen, darunter die am Fahrzeug gemessenen. Weicht eine
 * Zeile ab, nennt die Ausgabe den Namen, die Bytes und beide Zahlen.
 *
 * Diese Datei ist bewusst redundant. Sie darf erst verschwinden, wenn die
 * Tabelle einmal am Auto bestaetigt ist.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FRONTEND = path.join(__dirname, "..", "frontend");

/* ---------- Die Vorgabe: der Stand vor dem Umbau ---------- */

const ALT = {
  soc_roh: (b) => b[0],
  spannung_v: (b) => (b.length >= 2 ? (b[0] * 256 + b[1]) / 4 : null),
  strom_a: (b) => (b.length >= 4
    ? ((b[0] * 16777216) + (b[1] * 65536) + (b[2] * 256) + b[3] - 150000) / 100
    : null),
  entladen_kwh: (b) => {
    if (b.length < 16) return null;
    const roh = (b[12] * 16777216) + (b[13] * 65536) + (b[14] * 256) + b[15];
    return Math.abs((roh >= 2147483648 ? roh - 4294967296 : roh) / 8583.07);
  },
  geladen_kwh: (b) => (b.length >= 12
    ? ((b[8] * 16777216) + (b[9] * 65536) + (b[10] * 256) + b[11]) / 8583.07
    : null),
  ladegrenze_a: (b) => (b.length >= 2 ? (b[0] * 256 + b[1]) / 5 : null),
  betriebsart: (b) => b[0],
  ptc_strom_a: (b) => (b.length ? b[0] / 4 : null),
  tempo_kmh: (b) => b[0],
  nebenverbrauch_kw: (b) => (b.length >= 2 ? (b[0] * 256 + b[1]) / 10 : null),
  km_stand: (b) => (b.length >= 3
    ? (b[0] * 65536) + (b[1] * 256) + b[2] : null),
  dcdc_strom_a: (b) => (b.length >= 2 ? (b[0] * 256 + b[1]) / 16 : null),
  akku_kwh: (b) => {
    if (b.length < 4) return null;
    const roh = (b[0] * 16777216) + (b[1] * 65536) + (b[2] * 256) + b[3];
    const kwh = roh / 1310.77 / 1000;
    return (kwh >= 10 && kwh <= 200) ? kwh : null;
  },
  reichweite_km: (b) => {
    if (b.length < 2) return null;
    const km = (b[0] * 256) + b[1];
    return (km >= 0 && km <= 999) ? km : null;
  },
  batterie_c: (b) => (b.length ? (b[0] / 2) - 40 : null),
  kompressor_w: (b) => {
    if (b.length < 7) return null;
    const w = (b[5] * 256) + b[6];
    return (w >= 0 && w <= 8000) ? w : null;
  },
  kompressor_upm: (b) => (b.length >= 5 ? (b[3] * 256) + b[4] : null),
  kompressor_an: (b) => (b.length ? (b[0] & 1) : null),
  aussentemp_c: (b) => (b.length ? b[0] / 2 - 50 : null),
  innentemp_c: (b) => (b.length >= 2 ? ((b[0] * 256 + b[1]) / 5) - 40 : null),
};

/* Die Zieladresse und die Datenkennung gehoeren zur Umrechnung: Eine Zeile
 * mit richtiger Formel und falscher Adresse liefert gar nichts, und eine
 * mit falscher Datenkennung die Zahl eines anderen Parameters. */
const ALT_ADRESSEN = {
  soc_roh: ["22028C", "FC007B", true, 0],
  spannung_v: ["221E3B", "FC007B", false, 0],
  strom_a: ["221E3D", "FC007B", false, 0],
  entladen_kwh: ["221E32", "FC007B", false, 0],
  ladegrenze_a: ["221E1B", "FC007B", false, 0],
  betriebsart: ["227448", "FC007B", false, 0],
  ptc_strom_a: ["221620", "FC007B", false, 0],
  tempo_kmh: ["22F40D", "FC007B", false, 0],
  nebenverbrauch_kw: ["220364", "FC0076", false, 0],
  km_stand: ["22295A", "FC0076", false, 0],
  dcdc_strom_a: ["22465B", "FC00B9", false, 10],
  akku_kwh: ["222AB2", "710", false, 40],
  reichweite_km: ["222AB6", "710", false, 10],
  batterie_c: ["222A0B", "FC007B", false, 10],
  kompressor_w: ["220800", "746", false, 20],
  aussentemp_c: ["222609", "746", false, 20],
  innentemp_c: ["222613", "746", false, 20],
};

/* ---------- Das Neue laden ---------- */

function neuLaden() {
  const fenster = {
    console, TextEncoder, TextDecoder, DataView, Uint8Array,
    setTimeout, clearTimeout, navigator: {}, localStorage: {
      getItem: () => null, setItem: () => {},
    },
  };
  fenster.window = fenster;
  const kontext = vm.createContext(fenster);
  for (const datei of ["messwerte.js", "obd-ble-nativ.js", "obd-kern.js"]) {
    vm.runInContext(fs.readFileSync(path.join(FRONTEND, datei), "utf8"),
                    kontext, { filename: datei });
  }
  return fenster;
}

/* ---------- Bytefolgen zum Vergleichen ---------- */

/* Erst die Grenzfaelle, dann Zufall. Die Grenzfaelle sind die wichtigeren:
 * Dort entscheidet sich, ob eine zu kurze Antwort null ergibt statt einer
 * aus `undefined` gerechneten Zahl. */
function probefolgen() {
  const folgen = [];
  for (let n = 0; n <= 20; n += 1) {
    folgen.push(new Array(n).fill(0));                    // alles null
    folgen.push(new Array(n).fill(255));                  // alles gesetzt
    folgen.push(Array.from({ length: n }, (_, i) => i));   // aufsteigend
  }
  // Der am Fahrzeug gemessene Entladezaehler: 0xF7141E0D auf b12..b15.
  const gemessen = new Array(16).fill(0);
  gemessen[12] = 0xF7; gemessen[13] = 0x14;
  gemessen[14] = 0x1E; gemessen[15] = 0x0D;
  folgen.push(gemessen);
  // Der bestaetigte Ladestand: Rohwert 0xB4.
  folgen.push([0xB4]);
  // Die Kompressor-Messung "an" aus dem Kommentar in messwerte.js.
  folgen.push([0x51, 0x24, 0xC0, 0x24, 0xC0, 0x0A, 0x3A, 0x0E, 0, 0, 0]);

  let saat = 20260912;
  const wuerfel = () => {
    // Ein fester, einfacher Generator: Der Lauf muss wiederholbar sein,
    // sonst ist ein roter Lauf morgen wieder gruen.
    saat = (saat * 1103515245 + 12345) & 0x7FFFFFFF;
    return saat % 256;
  };
  for (let i = 0; i < 400; i += 1) {
    const n = 1 + (wuerfel() % 20);
    folgen.push(Array.from({ length: n }, wuerfel));
  }
  return folgen;
}

/* `auswerten` im Kern schiebt jeden Wert durch `sauber()`, das undefined
 * und NaN auf null abbildet. Die alten Funktionen gaben fuer eine leere
 * Antwort teils `undefined` zurueck (`b[0]`), die neue Formel gibt null -
 * hinter `sauber` ist das derselbe Wert. Verglichen wird deshalb danach. */
function sauber(wert) {
  return (wert === null || wert === undefined || Number.isNaN(wert))
    ? null : wert;
}

/* Fliesskomma: `roh / 1310.77 / 1000` und `roh / 1310770` sind mathematisch
 * dasselbe, in doppelter Genauigkeit aber nicht bitgleich. Der Unterschied
 * liegt bei rund 1e-16 relativ - fuer eine Kilowattstunde mit einer
 * Nachkommastelle bedeutungslos. Eine echte Verwechslung von Vorzeichen,
 * Teiler oder Bytelage liegt dagegen um Groessenordnungen daneben. */
function gleich(a, b) {
  if (a === null || b === null) return a === b;
  if (a === b) return true;
  const nenner = Math.max(Math.abs(a), Math.abs(b), 1e-12);
  return Math.abs(a - b) / nenner < 1e-9;
}

/* ---------- Lauf ---------- */

function main() {
  const f = neuLaden();
  let fehler = 0;

  if (f.joltObd.TABELLE_FEHLER.length) {
    console.log("Tabelle meldet Fehler:");
    for (const t of f.joltObd.TABELLE_FEHLER) console.log("  " + t);
    fehler += f.joltObd.TABELLE_FEHLER.length;
  }

  // Die Lesefunktionen stecken im Modul; erreichbar sind sie ueber den
  // Umweg, den auch die Aufzeichnung geht. Deshalb wird hier direkt auf die
  // aufgeloeste Tabelle zugegriffen.
  const tabelle = f.window.joltMesswerte;
  const felder = f.joltObd.FELDER;

  // 1. Vollstaendigkeit: Kein Wert darf beim Umbau verlorengegangen sein.
  const alteNamen = Object.keys(ALT).sort();
  const neueNamen = felder.map((x) => x.name).sort();
  const fehlend = alteNamen.filter((n) => !neueNamen.includes(n));
  const ueberzaehlig = neueNamen.filter((n) => !alteNamen.includes(n));
  console.log(`Messwerte: ${neueNamen.length} in der Tabelle, `
              + `${alteNamen.length} vorher`);
  if (fehlend.length) {
    console.log("  FEHLT: " + fehlend.join(", ")); fehler += fehlend.length;
  }
  if (ueberzaehlig.length) {
    console.log("  NEU (nicht in der Vorgabe): " + ueberzaehlig.join(", "));
  }

  // 2. Datenkennung, Zieladresse und Takt.
  console.log("\nDatenkennung, Adresse und Takt:");
  let kopfFehler = 0;
  for (const zeile of tabelle.werte) {
    const soll = ALT_ADRESSEN[zeile.name];
    if (!soll) continue;
    const adresse = tabelle.adressen[zeile.adresse];
    const [did, sh, pflicht, selten] = soll;
    const abweichung = [];
    if (zeile.did !== did) abweichung.push(`did ${zeile.did} statt ${did}`);
    if (!adresse) abweichung.push(`Adresse "${zeile.adresse}" unbekannt`);
    else if (adresse.sh !== sh) abweichung.push(`sh ${adresse.sh} statt ${sh}`);
    if (!!zeile.pflicht !== pflicht) abweichung.push("pflicht weicht ab");
    if ((zeile.selten || 0) !== selten) {
      abweichung.push(`selten ${zeile.selten || 0} statt ${selten}`);
    }
    if (abweichung.length) {
      console.log(`  FEHLER ${zeile.name}: ${abweichung.join(", ")}`);
      kopfFehler += 1;
    }
  }
  console.log(kopfFehler ? `  ${kopfFehler} Abweichung(en)` : "  alle gleich");
  fehler += kopfFehler;

  // 3. Die Umrechnungen selbst.
  console.log("\nUmrechnungen gegen die Vorgabe:");
  const folgen = probefolgen();
  const lesenNeu = {};
  for (const zeile of tabelle.werte) {
    lesenNeu[zeile.name] = f.joltObd.lesenFuer(zeile.name);
    for (const w of zeile.auch || []) {
      lesenNeu[w.name] = f.joltObd.lesenFuer(w.name);
    }
  }

  for (const name of alteNamen) {
    const alt = ALT[name];
    const neu = lesenNeu[name];
    if (!neu) {
      console.log(`  FEHLER ${name}: keine Lesefunktion in der Tabelle`);
      fehler += 1;
      continue;
    }
    let abweichungen = 0;
    let beispiel = null;
    for (const bytes of folgen) {
      const a = sauber(alt(bytes));
      const b = sauber(neu(bytes));
      if (!gleich(a, b)) {
        abweichungen += 1;
        if (!beispiel) beispiel = { bytes, a, b };
      }
    }
    if (abweichungen) {
      console.log(`  FEHLER ${name}: ${abweichungen} von ${folgen.length} `
                  + `Folgen weichen ab`);
      console.log(`         Bytes [${beispiel.bytes.join(", ")}]`);
      console.log(`         vorher ${beispiel.a}, jetzt ${beispiel.b}`);
      fehler += 1;
    } else {
      console.log(`  ok     ${name} (${folgen.length} Folgen gleich)`);
    }
  }

  console.log(fehler === 0
    ? "\nDie Tabelle rechnet wie die Funktionen davor."
    : `\n${fehler} Abweichung(en).`);
  process.exit(fehler === 0 ? 0 : 1);
}

main();
