#!/usr/bin/env node
/* Prueft die Beobachtung im Dongle-Baustein - Protokoll, Zaehler, Konsole.
 *
 * Die Einstellungen-Ansicht zeigt, was `joltObd.diagnose()` und
 * `joltObd.protokoll()` liefern. Stimmen diese Zahlen nicht, fuehrt die
 * Diagnose in die Irre - und sie wird gerade dann gebraucht, wenn im Auto
 * etwas nicht stimmt, also genau dort, wo man sich auf sie verlassen muss.
 *
 * Gegen einen nachgebildeten Dongle (Web Bluetooth) laufen lassen:
 *   verbinden -> Handshake -> Satz lesen -> Zaehler und Protokoll ansehen.
 * Dazu der Gegenbeweis, dass die Beobachtung nichts veraendert: Was
 * gesendet wird, ist dieselbe Befehlsfolge wie ohne.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FRONTEND = path.join(__dirname, "..", "frontend");
let fehler = 0;

function pruefe(sollGelten, text, zusatz) {
  if (sollGelten) {
    console.log("  ok    " + text);
  } else {
    console.log("  FEHLT " + text + (zusatz ? "   " + zusatz : ""));
    fehler += 1;
  }
}

/* Ein ELM327, der auf bekannte Abfragen antwortet, auf eine gar nicht
 * (Zeitablauf) und auf den Rest mit NO DATA. */
function dongle(gesendet, optionen) {
  const hoerer = [];
  const antworten = {
    "ATZ": "ELM327 v2.3", "ATRV": "12.6V",
    "22028C": "17FE007B 04 62028C B4",
  };
  const ch = {
    uuid: "fff1",
    properties: { write: true, writeWithoutResponse: true, notify: true },
    addEventListener(t, f) { hoerer.push(f); },
    removeEventListener() {},
    async startNotifications() {},
    async writeValueWithoutResponse(d) {
      const t = new TextDecoder().decode(d).trim();
      gesendet.push(t);
      if (optionen && optionen.stumm && optionen.stumm.includes(t)) return;
      const a = antworten[t] !== undefined ? antworten[t]
        : (t.startsWith("AT") ? "OK" : "NO DATA");
      setTimeout(() => {
        const text = a + "\r\r>";
        for (const teil of [text.slice(0, 6), text.slice(6)]) {
          const wert = new DataView(new TextEncoder().encode(teil).buffer);
          for (const f of hoerer) f({ target: { value: wert } });
        }
      }, 1);
    },
  };
  const dienst = { uuid: "fff0", async getCharacteristics() { return [ch]; } };
  const geraet = {
    name: "IOS-Vlink", addEventListener() {},
    gatt: { connected: true,
      async connect() { return { async getPrimaryServices() { return [dienst]; } }; },
      disconnect() { this.connected = false; } },
  };
  return { requestDevice: async () => geraet, getDevices: async () => [geraet] };
}

function umgebung(bluetooth) {
  const speicher = {};
  const fenster = {
    localStorage: {
      getItem: (k) => (k in speicher ? speicher[k] : null),
      setItem: (k, v) => { speicher[k] = String(v); },
      removeItem: (k) => { delete speicher[k]; },
    },
    navigator: { bluetooth },
    TextEncoder, TextDecoder, DataView, Uint8Array,
    setTimeout, clearTimeout, console,
  };
  fenster.window = fenster;
  const kontext = vm.createContext(fenster);
  for (const datei of ["obd-ble-nativ.js", "messwerte.js", "obd-kern.js"]) {
    vm.runInContext(fs.readFileSync(path.join(FRONTEND, datei), "utf8"),
                    kontext, { filename: datei });
  }
  return fenster;
}

async function main() {
  console.log("\nZaehler und Protokoll");
  const gesendet = [];
  const f = umgebung(dongle(gesendet));
  const O = f.joltObd;

  const vorher = O.diagnose();
  pruefe(vorher.verbunden === false && vorher.runden.n === 0
         && vorher.transport === "web",
         "vor dem Verbinden: getrennt, keine Runden, Zugang 'web'");

  await O.verbinden();
  await O.handshake();
  let d = O.diagnose();
  pruefe(d.verbunden && d.verbindung.geraet === "IOS-Vlink"
         && typeof d.verbindung.seit === "number",
         "nach dem Verbinden: Geraet und Zeitpunkt stehen drin");
  pruefe(d.befehle.gesendet === gesendet.length && d.befehle.beantwortet === gesendet.length
         && d.befehle.zeitablauf === 0,
         "jeder gesendete Befehl ist gezaehlt und beantwortet",
         `${d.befehle.gesendet}/${d.befehle.beantwortet}/${gesendet.length}`);
  pruefe(d.befehle.letzterEmpfang !== null && d.befehle.letzteMs !== null,
         "letzte Antwort und Antwortzeit sind vermerkt");

  const satz = await O.satzLesen(0);
  d = O.diagnose();
  pruefe(satz.soc_roh === 180, "der Ladestand wird wie bisher gelesen (0xB4 = 180)");
  pruefe(d.runden.n === 1 && d.runden.fehler === 0 && d.runden.letzteMs !== null,
         "eine Runde ist gezaehlt");
  pruefe(d.messwerte.soc_roh.ok === 1 && d.messwerte.soc_roh.wert === 180,
         "der Ladestand zaehlt als ok, mit letztem Wert");
  pruefe(d.messwerte.strom_a.leer === 1 && d.messwerte.strom_a.ok === 0,
         "NO DATA zaehlt als 'leer' - das Steuergeraet antwortet, die Kennung passt nicht");
  pruefe(d.letzterSatz && d.letzterSatz.werte.soc_roh === 180,
         "der letzte Satz steht zur Anzeige bereit");

  // Selten gelesene Werte: in Runde 0 dran, in Runde 1 nicht.
  const vorherN = d.messwerte.akku_kwh ? (d.messwerte.akku_kwh.ok + d.messwerte.akku_kwh.leer + d.messwerte.akku_kwh.fehler) : 0;
  await O.satzLesen(1);
  d = O.diagnose();
  const nachherN = d.messwerte.akku_kwh.ok + d.messwerte.akku_kwh.leer + d.messwerte.akku_kwh.fehler;
  pruefe(nachherN === vorherN, "was in der Runde nicht dran ist, wird nicht mitgezaehlt");

  console.log("\nProtokoll");
  const log = O.protokoll(false);
  pruefe(log.length > 10 && log.some((z) => z.art === "raus" && z.text === "22028C")
         && log.some((z) => z.art === "rein" && z.text.includes("62028C")),
         "gesendete und empfangene Zeilen stehen im Protokoll, mit Richtung");
  pruefe(log.every((z) => typeof z.zeit === "number"), "jede Zeile traegt eine Zeit");
  pruefe(O.protokoll(true).every((z) => /NO DATA|FEHLER|Zeit|keine Antwort|verspätet|getrennt|fehlgeschlagen|ERROR|UNABLE|BUS/i.test(z.text))
         && O.protokoll(true).length > 0,
         "der Filter 'nur Auffaelliges' zeigt nur Auffaelliges");
  log[0].text = "veraendert";
  pruefe(O.protokoll(false)[0].text !== "veraendert",
         "das Protokoll nach aussen ist eine Kopie");

  // Ringpuffer
  const f2 = umgebung(dongle([]));
  await f2.joltObd.verbinden();
  for (let i = 0; i < 400; i++) await f2.joltObd.befehl("ATRV");
  pruefe(f2.joltObd.protokoll(false).length <= 600,
         "das Protokoll waechst nicht unbegrenzt (Ringpuffer)",
         String(f2.joltObd.protokoll(false).length));

  console.log("\nKonsole");
  const vorBefehl = gesendet.length;
  await O.konsole("ATSH123");
  pruefe(gesendet[vorBefehl] === "ATSH123",
         "ein von Hand gesendeter Befehl geht unveraendert hinaus (ohne Umformen ausser dem Trimmen)");
  pruefe(O.diagnose().letzteAdresse === null,
         "danach ist die gemerkte Adresse verworfen - die naechste Runde setzt sie neu");
  const nachKonsole = gesendet.length;
  await O.satzLesen(2);
  pruefe(gesendet.slice(nachKonsole).some((t) => t.startsWith("ATSH")),
         "und setzt sie dann wirklich neu");

  console.log("\nAusfaelle");
  const gesendet3 = [];
  const f3 = umgebung(dongle(gesendet3, { stumm: ["ATRV"] }));
  await f3.joltObd.verbinden();
  let wurf = null;
  try { await f3.joltObd.befehl("ATRV", 40); } catch (e) { wurf = e; }
  const d3 = f3.joltObd.diagnose();
  pruefe(wurf && /Zeit/.test(wurf.message) && d3.befehle.zeitablauf === 1
         && d3.befehle.beantwortet === 0,
         "ein Befehl ohne Antwort ist als Zeitablauf gezaehlt, nicht als beantwortet");
  pruefe(f3.joltObd.protokoll(true).some((z) => /keine Antwort/.test(z.text)),
         "und steht als Auffaelliges im Protokoll");

  console.log("\nZuruecksetzen und Vergessen");
  O.zaehlerZuruecksetzen();
  d = O.diagnose();
  pruefe(d.runden.n === 0 && d.befehle.gesendet === 0
         && Object.keys(d.messwerte).length === 0,
         "Zaehler zuruecksetzen leert Runden, Befehle und Messwerte");
  pruefe(d.verbunden === true, "und trennt nichts");
  O.vergessen();
  pruefe(O.diagnose().gemerktesGeraet === null && O.diagnose().verbindung.geraet === "",
         "Vergessen loescht das gemerkte Geraet");
  pruefe(f.localStorage.getItem("jolt-ble-geraet") === null,
         "und die Merkkennung der nativen Bruecke");

  O.trennen();
  d = O.diagnose();
  pruefe(d.verbunden === false && d.verbindung.seit === null,
         "Trennen setzt 'verbunden seit' zurueck");

  console.log(fehler ? `\n${fehler} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
  process.exit(fehler ? 1 : 0);
}

main().catch((e) => { console.log("ABBRUCH", e); process.exit(2); });
