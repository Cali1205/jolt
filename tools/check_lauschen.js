#!/usr/bin/env node
/* Prueft das passive Mithoeren (obd-kern.js: lauschen).
 *
 * Es soll an einem ladenden, verriegelten Auto beobachten, ohne die
 * Alarmanlage zu wecken. Was sich ohne Auto pruefen laesst, ist das, woran
 * der Alarm haengt - **was gesendet wird**:
 *   - bis ATMA nur AT-Befehle (bleiben im Dongle), danach nur noch ein
 *     Stopp-Zeichen und der Handshake (wieder nur AT-Befehle),
 *   - keine Abfrage wie 22028C oder 0100, auch nicht von einer Leserunde,
 *     die zufaellig dazwischenkommt,
 *   - ATCSM1 (stilles Mitlesen) und ATCRA (Filter weg) vor ATMA.
 * Dazu die Auswertung: Kennungen, Haeufigkeit, Veraenderlichkeit.
 *
 *     node tools/check_lauschen.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FRONTEND = path.join(__dirname, "..", "frontend");
let fehler = 0;

function pruefe(ok, text, zusatz) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "   " + (zusatz || "")));
  if (!ok) fehler += 1;
}

/* Ein ELM327, der nach ATMA Frames schickt, bis irgendein Zeichen kommt, dann
 * "STOPPED" und die Eingabeaufforderung. */
function dongle(gesendet, optionen) {
  const o = optionen || {};
  const hoerer = [];
  let monitor = null;
  const ausgeben = (text) => {
    // In Haeppchen von je sieben Zeichen: BLE liefert so, mitten durch Zeilen.
    for (let i = 0; i < text.length; i += 7) {
      const wert = new DataView(new TextEncoder().encode(text.slice(i, i + 7)).buffer);
      for (const f of hoerer) f({ target: { value: wert } });
    }
  };
  const ch = {
    uuid: "fff1",
    properties: { write: true, writeWithoutResponse: true, notify: true },
    addEventListener(t, f) { hoerer.push(f); },
    removeEventListener() {},
    async startNotifications() {},
    async writeValueWithoutResponse(d) {
      const roh = new TextDecoder().decode(d);
      const t = roh.trim();
      gesendet.push(roh === "\r" ? "<STOPP>" : t);
      if (monitor) {                     // jedes Zeichen beendet das Mithoeren
        clearInterval(monitor); monitor = null;
        setTimeout(() => ausgeben("STOPPED\r\r>"), 1);
        return;
      }
      if (t === "ATMA") {
        let i = 0;
        monitor = setInterval(() => {
          i += 1;
          const frames = o.still ? [] : [
            `7B0 06 62 02 8C B4 00 00\r`,
            `17FE007B 04 62 02 8C ${(i % 4 + 16).toString(16).toUpperCase()}\r`,
            ...(i % 2 ? ["7B0 06 62 02 8C B4 00 00\r"] : []),
          ];
          if (o.rauschen && i === 2) frames.push("BUFFER FULL\r");
          ausgeben(frames.join(""));
        }, 20);
        return;
      }
      const antwort = (o.unbekannt || []).includes(t) ? "?"
        : (t === "ATZ" ? "ELM327 v2.3" : (t.startsWith("AT") ? "OK" : "NO DATA"));
      setTimeout(() => ausgeben(antwort + "\r\r>"), 1);
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
    setTimeout, clearTimeout, setInterval, clearInterval, console,
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
  console.log("Was gesendet wird");
  let gesendet = [];
  let f = umgebung(dongle(gesendet));
  let O = f.joltObd;
  await O.verbinden();
  await O.handshake();
  gesendet.length = 0;

  // Waehrend des Mithoerens versucht jemand zu lesen: Es darf nichts hinausgehen.
  let waehrend = null;
  const lauf = O.lauschen({ protokoll: "6", dauer_ms: 600 });
  await new Promise((w) => setTimeout(w, 250));
  waehrend = {
    lauscht: O.lauscht(),
    konsole: await O.konsole("ATRV").then(() => "gesendet", (e) => e.message),
    satz: await O.satzLesen(0).then(() => "gelesen", (e) => e.message),
    spannung: await O.spannung(),
  };
  const e = await lauf;

  const nachAtma = gesendet.slice(gesendet.indexOf("ATMA") + 1);
  const vorAtma = gesendet.slice(0, gesendet.indexOf("ATMA"));
  pruefe(gesendet.includes("ATMA"), "ATMA wird gesendet");
  pruefe(vorAtma.every((b) => /^AT/.test(b)),
         "alles vor ATMA sind AT-Befehle (bleiben im Dongle)", vorAtma.join(" "));
  pruefe(nachAtma.every((b) => b === "<STOPP>" || /^AT/.test(b)),
         "danach nur noch das Stopp-Zeichen und AT-Befehle (der Handshake)",
         nachAtma.join(" "));
  pruefe(!gesendet.some((b) => /^[0-9A-F]{4,}$/.test(b)),
         "an keiner Stelle eine Abfrage (22028C, 0100 ...) - nichts, was ein "
         + "Steuergeraet weckt", gesendet.join(" "));
  const csm = gesendet.indexOf("ATCSM1"), cra = gesendet.indexOf("ATCRA"), ma = gesendet.indexOf("ATMA");
  pruefe(csm > -1 && cra > -1 && csm < ma && cra < ma,
         "ATCSM1 (stilles Mitlesen) und ATCRA (Filter weg) kommen vor ATMA");
  pruefe(gesendet.indexOf("ATSP6") > -1 && gesendet.indexOf("ATSP6") < ma,
         "das gewaehlte Protokoll steht vor ATMA");
  pruefe(gesendet.indexOf("ATS1") > -1 && gesendet.indexOf("ATCAF0") > -1,
         "Leerzeichen an, Formatierung aus: rohe Frames, lesbar zerlegt");

  console.log("\nWaehrenddessen");
  pruefe(waehrend.lauscht === true, "lauscht() meldet es - die Oberflaeche liest dann nicht");
  pruefe(/lauscht/.test(waehrend.konsole), "ein Befehl aus der Konsole wird abgewiesen",
         waehrend.konsole);
  pruefe(/lauscht/.test(waehrend.satz), "eine Leserunde wird abgewiesen", waehrend.satz);
  pruefe(waehrend.spannung === null, "die Spannungspruefung auch - nichts geht hinaus");
  pruefe(!gesendet.includes("ATRV") && !gesendet.some((b) => b === "22028C"),
         "und es ist auch nichts davon beim Dongle angekommen");

  console.log("\nAuswertung");
  pruefe(e.gesamt > 6 && e.ids.length === 2, "Frames gezaehlt, zwei Kennungen",
         JSON.stringify({ g: e.gesamt, ids: e.ids.map((i) => i.id) }));
  const a = e.ids.find((i) => i.id === "7B0"), b = e.ids.find((i) => i.id === "17FE007B");
  pruefe(a && a.varianten === 1 && a.letzte === "06 62 02 8C B4 00 00",
         "eine Kennung mit immer gleichem Inhalt: eine Variante", JSON.stringify(a));
  pruefe(b && b.varianten >= 3 && b.varianten <= 4,
         "eine mit wechselndem Inhalt: mehrere Varianten (so sieht ein Messwert aus)",
         JSON.stringify(b));
  pruefe(a.n > b.n && e.ids[0].id === "7B0",
         "sortiert nach Haeufigkeit", e.ids.map((i) => i.id + ":" + i.n).join(" "));
  pruefe(a.proSek > 5 && a.proSek < 100, "mit Rate je Sekunde", String(a.proSek));
  pruefe(e.proben.length > 0 && e.proben.length <= 30, "und ein paar Rohzeilen als Probe");

  console.log("\nDanach");
  pruefe(O.lauscht() === false, "lauscht() ist wieder aus");
  pruefe(gesendet.indexOf("ATZ", ma) > -1 && gesendet.includes("ATSP7") &&
         gesendet.lastIndexOf("ATSP7") > ma && gesendet.lastIndexOf("ATSH" + "FC007B") > ma,
         "der Handshake stellt Protokoll, Filter und Adresse wieder her");
  gesendet.length = 0;
  const antwort = await O.konsole("ATRV").then(() => "ok", (x) => x.message);
  pruefe(antwort === "ok", "danach geht die Konsole wieder", antwort);

  console.log("\nStille und Stoerungen");
  gesendet = [];
  f = umgebung(dongle(gesendet, { still: true }));
  O = f.joltObd;
  await O.verbinden();
  const still = await O.lauschen({ dauer_ms: 500 });
  pruefe(still.gesamt === 0 && still.ids.length === 0,
         "ein stiller Bus ergibt: nichts angekommen - ohne Fehler");

  gesendet = [];
  f = umgebung(dongle(gesendet, { rauschen: true, unbekannt: ["ATCSM1"] }));
  O = f.joltObd;
  await O.verbinden();
  const laut = await O.lauschen({ protokoll: "7", dauer_ms: 500 });
  pruefe(laut.hinweise.some((h) => /BUFFER FULL/.test(h)),
         "BUFFER FULL erscheint als Hinweis und nicht als Frame", JSON.stringify(laut.hinweise));
  pruefe(laut.hinweise.some((h) => /ATCSM1/.test(h)),
         "ein Dongle, der ATCSM1 nicht kennt, wird gemeldet - das Ergebnis ist dann "
         + "mit Vorsicht zu lesen", JSON.stringify(laut.hinweise));
  pruefe(gesendet.includes("ATSP7"), "Protokoll 7 laesst sich waehlen");

  console.log("\nOhne Verbindung");
  f = umgebung(dongle([]));
  const ohne = await f.joltObd.lauschen({}).then(() => "lief", (x) => x.message);
  pruefe(/nicht verbunden/.test(ohne), "ohne Dongle: Fehler statt Absturz", ohne);

  console.log(fehler ? `\n${fehler} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
  process.exit(fehler ? 1 : 0);
}

main().catch((x) => { console.log("Abbruch:", x); process.exit(1); });
