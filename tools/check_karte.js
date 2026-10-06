#!/usr/bin/env node
// Prueft die Bedienung der Karte (frontend/karte.js): Ziehen, Kneifen, Mausrad.
//
// Die Karte sprang beim Zoomen und Ziehen unvorhersagbar hin und her. Gesucht
// war also nicht "es zoomt", sondern was ein Mensch sofort merkt:
//   - bleibt der Punkt unter den Fingern unter den Fingern, wenn man kneift?
//   - springt die Karte, wenn nach dem Kneifen ein Finger bleibt?
//   - kommt sie aus dem Zoommodus wieder heraus, wenn ein Zeiger verloren geht?
//
// Ohne Browser: eine Attrappe der Leinwand faengt die Ereignisse ab, und die
// Pruefung spielt sie in der Reihenfolge ein, in der ein Finger sie liefert.
//
//     node tools/check_karte.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const quelle = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "karte.js"), "utf8");

let fehler = 0;
function pruefe(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) fehler++;
}

function neueKarte() {
  const ereignisse = {};
  const bilder = [];
  const leinwand = {
    clientWidth: 400, clientHeight: 300, width: 400, height: 300,
    addEventListener: (name, f) => { ereignisse[name] = f; },
    setPointerCapture() {},
    getBoundingClientRect: () => ({ left: 10, top: 20 }),   // nicht bei (0, 0)
    getContext: () => new Proxy({}, {
      get: (z, n) => (n === "drawImage" ? (...a) => bilder.push(a) : () => {}),
      set: () => true }),
  };
  const fenster = { devicePixelRatio: 1, addEventListener() {} };
  const kontext = {
    window: fenster, document: { getElementById: () => leinwand },
    requestAnimationFrame: (f) => f(), Math, Number, Map, Array, Date,
    Image: function () { this.complete = false; },
    console,
  };
  vm.createContext(kontext);
  vm.runInContext(quelle, kontext);
  const k = fenster.joltKarte;
  k.erstellen("karte");
  const finger = (typ, id, x, y) => ereignisse[typ]({
    pointerId: id, pointerType: "touch", button: 0,
    clientX: x + 10, clientY: y + 20,        // Bildschirm = Leinwand + Versatz
    preventDefault() {}, deltaY: 0 });
  return { k, ereignisse, finger, bilder, leinwand };
}

// Wie weit liegt der Ort (lat, lon) gerade von der Stelle (x, y) entfernt?
const abstandPx = (k, ort, x, y) => {
  const p = k.nachSchirm(ort.lat, ort.lon);
  return Math.hypot(p.x - x, p.y - y);
};
// Welcher Ort liegt unter dem Pixel (x, y)? Ueber die Umkehrung der Projektion.
function ortUnter(k, x, y) {
  const a = k.ansicht();
  const n = 256 * Math.pow(2, a.zoom);
  const wx = (a.mitte.lon + 180) / 360 * n;
  const rad = a.mitte.lat * Math.PI / 180;
  const wy = (1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2 * n;
  const px = wx + x - 200, py = wy + y - 150;
  const lon = px / n * 360 - 180;
  const kk = Math.PI - 2 * Math.PI * py / n;
  const lat = 180 / Math.PI * Math.atan(0.5 * (Math.exp(kk) - Math.exp(-kk)));
  return { lat, lon };
}

console.log("Ziehen mit einem Finger");
let t = neueKarte();
t.k.aufPunkt(48.0, 9.0, 10);
const ort = ortUnter(t.k, 200, 150);
t.finger("pointerdown", 1, 200, 150);
for (let i = 1; i <= 10; i++) t.finger("pointermove", 1, 200 + i * 10, 150 + i * 5);
pruefe(abstandPx(t.k, ort, 300, 200) < 1,
       "der Ort unter dem Finger bleibt unter dem Finger, auch bei einem "
       + "Leinwand-Versatz im Fenster",
       abstandPx(t.k, ort, 300, 200).toFixed(2) + " px");
t.finger("pointerup", 1, 300, 200);

console.log("\nKneifen");
t = neueKarte();
t.k.aufPunkt(48.0, 9.0, 10);
t.finger("pointerdown", 1, 150, 150);
t.finger("pointerdown", 2, 250, 150);
const mitteOrt = ortUnter(t.k, 200, 150);
for (let i = 1; i <= 10; i++) {            // auseinander: 100 -> 200 px
  t.finger("pointermove", 1, 150 - i * 5, 150);
  t.finger("pointermove", 2, 250 + i * 5, 150);
}
const a = t.k.ansicht();
pruefe(Math.abs(a.zoom - 11) < 0.05,
       "doppelter Fingerabstand = eine Zoomstufe mehr", a.zoom.toFixed(3));
pruefe(abstandPx(t.k, mitteOrt, 200, 150) < 1,
       "der Ort zwischen den Fingern bleibt zwischen den Fingern",
       abstandPx(t.k, mitteOrt, 200, 150).toFixed(2) + " px");

console.log("\nKneifen um einen Punkt abseits der Mitte");
t = neueKarte();
t.k.aufPunkt(48.0, 9.0, 10);
t.finger("pointerdown", 1, 40, 60);
t.finger("pointerdown", 2, 80, 60);
const ecke = ortUnter(t.k, 60, 60);
for (let i = 1; i <= 8; i++) {
  t.finger("pointermove", 1, 40 - i * 2, 60);
  t.finger("pointermove", 2, 80 + i * 2, 60);
}
pruefe(abstandPx(t.k, ecke, 60, 60) < 1,
       "auch in der Ecke bleibt der Inhalt unter den Fingern (frueher: um die "
       + "Kartenmitte gezoomt, der Inhalt wanderte weg)",
       abstandPx(t.k, ecke, 60, 60).toFixed(2) + " px");

console.log("\nEin Finger bleibt nach dem Kneifen");
t = neueKarte();
t.k.aufPunkt(48.0, 9.0, 10);
t.finger("pointerdown", 1, 100, 150);
t.finger("pointerdown", 2, 300, 150);
for (let i = 1; i <= 5; i++) {
  t.finger("pointermove", 1, 100 + i * 10, 150);
  t.finger("pointermove", 2, 300 - i * 10, 150);
}
t.finger("pointerup", 2, 250, 150);        // der zweite Finger hebt ab
const vorher = t.k.ansicht();
t.finger("pointermove", 1, 151, 150);      // der erste bewegt sich einen Pixel
const nachher = t.k.ansicht();
const sprung = Math.hypot(
  (nachher.mitte.lon - vorher.mitte.lon) * 256 * Math.pow(2, nachher.zoom) / 360,
  (nachher.mitte.lat - vorher.mitte.lat) * 256 * Math.pow(2, nachher.zoom) / 360);
pruefe(sprung < 3 && Math.abs(nachher.zoom - vorher.zoom) < 1e-9,
       "ein Pixel Bewegung schiebt die Karte um etwa einen Pixel - nicht um "
       + "die Strecke bis zur Position vor dem Kneifen (der Sprung)",
       sprung.toFixed(1) + " px");
const unter = ortUnter(t.k, 151, 150);
t.finger("pointermove", 1, 200, 180);
pruefe(abstandPx(t.k, unter, 200, 180) < 1,
       "und der Finger zieht die Karte danach 1:1");
t.finger("pointerup", 1, 200, 180);

console.log("\nZeiger geht verloren");
t = neueKarte();
t.k.aufPunkt(48.0, 9.0, 10);
t.finger("pointerdown", 1, 100, 150);
t.finger("pointerdown", 2, 300, 150);
t.ereignisse["pointercancel"]({ pointerId: 2 });     // das System nimmt ihn weg
t.ereignisse["pointercancel"]({ pointerId: 1 });
const z0 = t.k.ansicht().zoom;
t.finger("pointerdown", 3, 200, 150);
t.finger("pointermove", 3, 250, 150);
pruefe(Math.abs(t.k.ansicht().zoom - z0) < 1e-9,
       "nach verlorenen Zeigern bleibt die Karte nicht im Zoommodus: ein neuer "
       + "Finger zieht nur");
t.ereignisse["lostpointercapture"]({ pointerId: 3 });
t.finger("pointerdown", 4, 100, 100);
t.finger("pointermove", 4, 100, 100);
pruefe(true, "lostpointercapture raeumt auf, ohne zu werfen");

console.log("\nMausrad");
t = neueKarte();
t.k.aufPunkt(48.0, 9.0, 10);
const radOrt = ortUnter(t.k, 330, 70);
t.ereignisse["wheel"]({ deltaY: -100, deltaMode: 0, ctrlKey: false,
                        clientX: 340, clientY: 90, preventDefault() {} });
pruefe(t.k.ansicht().zoom > 10.3 && t.k.ansicht().zoom < 10.5,
       "eine Raste vergroessert um knapp eine halbe Stufe", String(t.k.ansicht().zoom));
pruefe(abstandPx(t.k, radOrt, 330, 70) < 1,
       "und zoomt um den Mauszeiger, nicht um die Mitte",
       abstandPx(t.k, radOrt, 330, 70).toFixed(2) + " px");
t.ereignisse["wheel"]({ deltaY: 4, deltaMode: 0, ctrlKey: false,
                        clientX: 340, clientY: 90, preventDefault() {} });
pruefe(Math.abs(t.k.ansicht().zoom - 10.4 + 0.016) < 0.05,
       "ein kleiner Trackpad-Schritt aendert nur ein wenig (proportional)");

console.log("\nGrenzen");
t = neueKarte();
t.k.aufPunkt(48.0, 9.0, 17);
t.ereignisse["wheel"]({ deltaY: -100, deltaMode: 0, ctrlKey: false,
                        clientX: 200, clientY: 150, preventDefault() {} });
pruefe(t.k.ansicht().zoom === 17, "nicht ueber die groesste Stufe hinaus");
t.k.aufPunkt(84.9, 9.0, 4);
t.finger("pointerdown", 1, 200, 150);
t.finger("pointermove", 1, 200, 5000);        // weit nach unten ziehen
const lat = t.k.ansicht().mitte.lat;
pruefe(Number.isFinite(lat) && lat <= 85.06,
       "ueber den Rand der Welt hinaus gibt es keine unsinnigen Breiten", String(lat));

console.log("\nKacheln");
t = neueKarte();
t.k.aufPunkt(48.0, 9.0, 10);
pruefe(t.bilder.length === 0, "ohne geladene Kacheln wird nichts gezeichnet, nichts bricht");

console.log(fehler ? `\n${fehler} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
process.exit(fehler ? 1 : 0);
