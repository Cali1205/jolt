#!/usr/bin/env node
// Prueft die Messpunkt-Warteschlange aus frontend/live.js gegen einen Server,
// der ausfaellt: Funkloch, Nachreichen in Stapeln, 409/422. Ohne Browser, ohne
// Netz - live.js laeuft in Node gegen Attrappen.
//
//     node tools/check_puffer.js
const fs = require("fs");
const vm = require("vm");

const quelle = fs.readFileSync(
  require("path").join(__dirname, "..", "frontend", "live.js"), "utf8");

let fehler = 0;
function pruefe(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) fehler++;
}

const speicher = {};
const meldungen = [];
let netz = true;           // false = "Server nicht erreichbar"
let nextStatus = null;     // erzwungener HTTP-Status
let schlechteZeit = null;  // ein Stapel mit diesem Punkt wird mit 422 abgelehnt
let abgelehnteAnfragen = 0;
const posts = [];          // was beim Server ankam

const leer = () => new Proxy(function () { return ""; }, {
  get: (z, n) => (n === Symbol.toPrimitive ? () => "" : n === "style" ? {} : leer()),
  apply: () => leer(), set: () => true });
const K0 = {
  zustand: { sitzungId: 7 },
  melden: (t) => meldungen.push(t),
  an() {}, reglerKoppeln() {}, sitzungMerken() {}, gemerkteSitzung: () => null,
  api: async (pfad, opt) => {
    if (!netz) throw new Error("Server nicht erreichbar.");
    if (nextStatus) {
      const e = new Error("HTTP " + nextStatus); e.status = nextStatus; throw e;
    }
    if (schlechteZeit && opt.body && opt.body.punkte
        && opt.body.punkte.some((p) => p.zeit === schlechteZeit)) {
      abgelehnteAnfragen++;
      const e = new Error("HTTP 422"); e.status = 422; throw e;
    }
    posts.push({ pfad, body: opt.body });
    return { typ: "zustand", n: posts.length };
  },
};
const K = new Proxy(K0, { get: (z, n) => (n in z ? z[n] : leer()) });
let geoCallback = null;
const fensterEreignisse = {};
const element = () => ({ style: {}, addEventListener() {}, play: async () => {},
  pause() {}, setAttribute() {}, appendChild() {}, hidden: false });
const fenster = {
  jolt: K,
  addEventListener: (n, f) => { fensterEreignisse[n] = f; },
  joltBlePlugin: undefined,
};
const kontext = {
  window: fenster, console, setTimeout, clearTimeout, Date, JSON, Math,
  document: { addEventListener() {}, getElementById: () => leer(), querySelector: () => leer(), querySelectorAll: () => [],
              createElement: element, body: { appendChild() {} },
              visibilityState: "visible" },
  navigator: { geolocation: {
    watchPosition: (f) => { geoCallback = f; return 1; },
    clearWatch() {} } },
  localStorage: {
    getItem: (k) => (k in speicher ? speicher[k] : null),
    setItem: (k, v) => { speicher[k] = v; },
    removeItem: (k) => { delete speicher[k]; } },
  WebSocket: function () {},
};
kontext.window.localStorage = kontext.localStorage;
vm.createContext(kontext);
vm.runInContext(quelle, kontext);
const live = fenster.joltLive;
// zustandAnzeigen braucht DOM - fuer den Test ohne Wirkung.

const warte = (ms) => new Promise((r) => setTimeout(r, ms));
let t = Date.now() - 600000;
async function punkt(geschwindigkeit = null) {
  t += 12000;
  // Die Sperre gegen zu haeufige Meldungen umgehen: jede Meldung ist 12 s
  // spaeter nach Messzeit, aber der Takt in der Funktion nutzt die Wanduhr.
  kontext.Date = Date;
  geoCallback({ coords: { latitude: 52, longitude: 10, speed: geschwindigkeit,
                          altitude: null }, timestamp: t });
  await warte(20);
}

(async () => {
  live.positionVerfolgen();
  fenster.joltBlePlugin = undefined;

  // Die Meldesperre (12 s Wanduhr) schlaegt im Test zu - deshalb ueber
  // visibilitychange zuruecksetzen waere noetig; einfacher: Date.now patchen.
  let jetzt = Date.now();
  const echtesNow = Date.now;
  Date.now = () => (jetzt += 13000);

  console.log("\nNetz da");
  await punkt();
  pruefe(posts.length === 1 && posts[0].pfad === "/api/live/7/punkte"
         && posts[0].body.punkte.length === 1, "ein Punkt geht als Stapel von eins hinaus",
         JSON.stringify(posts));
  pruefe(typeof posts[0].body.punkte[0].zeit === "string"
         && posts[0].body.punkte[0].zeit.endsWith("Z"),
         "mit Messzeit im ISO-Format", posts[0].body.punkte[0].zeit);
  pruefe(!speicher["jolt-puffer-7"], "und nichts bleibt im Speicher liegen");

  console.log("\nFunkloch");
  netz = false;
  await punkt(); await punkt(); await punkt();
  pruefe(posts.length === 1, "ohne Netz kommt nichts an");
  const gesichert = JSON.parse(speicher["jolt-puffer-7"] || "[]");
  pruefe(gesichert.length === 3, "drei Punkte liegen gesichert im Speicher",
         String(gesichert.length));
  pruefe(meldungen.filter((m) => /gesammelt/.test(m)).length === 1,
         "gemeldet wird das Funkloch einmal, nicht bei jedem Punkt",
         JSON.stringify(meldungen));

  console.log("\nNetz kommt zurueck");
  netz = true;
  await punkt();
  const letzter = posts[posts.length - 1].body.punkte;
  pruefe(letzter.length === 4, "alle vier gehen in einem Stapel hinaus",
         String(letzter.length));
  const zeiten = letzter.map((p) => p.zeit);
  pruefe(JSON.stringify(zeiten) === JSON.stringify([...zeiten].sort())
         && new Set(zeiten).size === 4,
         "in Messreihenfolge und mit verschiedenen Zeiten");
  pruefe(!speicher["jolt-puffer-7"], "danach ist der Speicher leer");
  pruefe(meldungen.some((m) => /nachgereicht/.test(m)),
         "und es wird gesagt, dass nachgereicht wurde");

  console.log("\nSitzung beendet");
  nextStatus = 409;
  const vorher = posts.length;
  await punkt();
  pruefe(!speicher["jolt-puffer-7"] && posts.length === vorher,
         "409 leert die Warteschlange, statt sie ewig zu wiederholen");
  nextStatus = null;

  console.log("\nUngueltiger Stapel");
  netz = true; nextStatus = 422;
  await punkt();
  pruefe(!speicher["jolt-puffer-7"], "422 verwirft den Stapel, er verstopft nichts");
  nextStatus = null;

  console.log("\nGrosser Rueckstau");
  netz = false;
  for (let i = 0; i < 250; i++) await punkt();
  netz = true;
  const vor = posts.length;
  await punkt();
  const neu = posts.slice(vor).map((p) => p.body.punkte.length);
  pruefe(neu.length === 3 && neu[0] === 100 && neu[1] === 100 && neu[2] === 51,
         "251 Punkte gehen in Stapeln zu 100", JSON.stringify(neu));

  console.log("\nEin schlechter Punkt im Stapel");
  // Der Server lehnt seit der Absicherung Unmoegliches mit 422 ab. Frueher
  // flog dann der ganze Stapel raus - wegen eines Punktes bis zu 99 gute.
  netz = false;
  const messzeiten = [];
  for (let i = 0; i < 12; i++) { await punkt(); messzeiten.push(new Date(t).toISOString()); }
  schlechteZeit = messzeiten[5];
  netz = true;
  abgelehnteAnfragen = 0;
  meldungen.length = 0;
  const vorStapel = posts.length;
  await punkt();                       // 13 Punkte warten: Stapel mit dem schlechten
  const angekommen = posts.slice(vorStapel).flatMap((x) => x.body.punkte.map((p) => p.zeit));
  pruefe(angekommen.length === 12 && !angekommen.includes(schlechteZeit),
         "genau der schlechte Punkt fehlt, die zwoelf anderen sind angekommen",
         `${angekommen.length} angekommen, schlechter dabei: ${angekommen.includes(schlechteZeit)}`);
  pruefe(JSON.stringify(angekommen) === JSON.stringify([...angekommen].sort()),
         "in Messreihenfolge");
  pruefe(abgelehnteAnfragen >= 1 && abgelehnteAnfragen <= 5,
         "durch Halbieren gefunden - wenige Anfragen, nicht eine je Punkt",
         String(abgelehnteAnfragen));
  pruefe(!speicher["jolt-puffer-7"], "und nichts bleibt liegen");
  pruefe(meldungen.some((m) => /1 Messpunkt wurde vom Server abgelehnt/.test(m)),
         "der Nutzer erfaehrt, dass einer verworfen wurde", JSON.stringify(meldungen));
  schlechteZeit = null;
  // Danach geht es mit voller Stapelgroesse weiter.
  netz = false;
  for (let i = 0; i < 150; i++) await punkt();
  netz = true;
  const vor2 = posts.length;
  await punkt();
  const gr = posts.slice(vor2).map((x) => x.body.punkte.length);
  pruefe(gr[0] === 100, "danach wieder volle Stapel zu 100", JSON.stringify(gr));

  console.log("\nDer einzige Punkt ist schlecht");
  netz = false;
  await punkt();
  schlechteZeit = new Date(t).toISOString();
  netz = true;
  abgelehnteAnfragen = 0;
  const vor3 = posts.length;
  schlechteZeit = new Date(t).toISOString();
  // Der Punkt ist schon im Puffer: er wird gesendet, abgelehnt, verworfen.
  await punkt();                       // dieser zweite geht durch
  pruefe(abgelehnteAnfragen >= 1 && !speicher["jolt-puffer-7"],
         "ein einzelner abgelehnter Punkt wird verworfen und verstopft nichts",
         String(abgelehnteAnfragen));
  schlechteZeit = null;

  console.log("\nWerte, die der Server ablehnen wuerde");
  const jungster = () => { const b = posts[posts.length - 1].body.punkte;
                          return b[b.length - 1]; };
  netz = true;
  await punkt(25);
  pruefe(Math.abs(jungster().tempo_kmh - 90) < 1e-9, "25 m/s sind 90 km/h", String(jungster().tempo_kmh));
  await punkt(null);
  pruefe(jungster().tempo_kmh === null, "kein Tempo bleibt leer");
  await punkt(-1);
  pruefe(jungster().tempo_kmh === null,
         "-1 (das Geraet weiss es nicht) wird nicht als Rueckwaertsfahrt gesendet",
         String(jungster().tempo_kmh));
  await punkt(300);
  pruefe(jungster().tempo_kmh === null,
         "1080 km/h sind ein Messfehler und gehen nicht hinaus", String(jungster().tempo_kmh));
  await punkt(NaN);
  pruefe(jungster().tempo_kmh === null, "NaN auch nicht");
  await punkt(250 / 3.6);
  pruefe(jungster().tempo_kmh !== null, "250 km/h sind noch gueltig");

  Date.now = echtesNow;
  console.log(fehler ? `\n${fehler} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
  process.exit(fehler ? 1 : 0);
})();
