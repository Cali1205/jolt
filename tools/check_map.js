#!/usr/bin/env node
// Checks the map controls (frontend/map.js): drag, pinch, mouse wheel.
//
// The map jumped back and forth unpredictably when zooming and dragging. So what was
// sought was not "it zooms" but what a person notices immediately:
//   - does the point under the fingers stay under the fingers when pinching?
//   - does the map jump if one finger stays after pinching?
//   - does it leave zoom mode again when a pointer is lost?
//
// Without a browser: a stand-in for the canvas intercepts the events, and the
// check plays them in the order in which a finger delivers them.
//
//     node tools/check_map.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "map.js"), "utf8");

let failure = 0;
function verify(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) failure++;
}

function newMap() {
  const events = {};
  const pictures = [];
  const buttons = [];
  const holder = { style: {}, appendChild: (k) => buttons.push(k) };
  const canvas = {
    parentElement: holder,
    clientWidth: 400, clientHeight: 300, width: 400, height: 300,
    addEventListener: (name, f) => { events[name] = f; },
    setPointerCapture() {},
    getBoundingClientRect: () => ({ left: 10, top: 20 }),   // not at (0, 0)
    getContext: () => new Proxy({}, {
      get: (z, n) => (n === "drawImage" ? (...a) => pictures.push(a)
        : n === "measureText" ? () => ({ width: 40 }) : () => {}),
      set: () => true }),
  };
  const timeframe = { devicePixelRatio: 1, addEventListener() {} };
  const context = {
    window: timeframe,
    document: {
      getElementById: () => canvas,
      createElement: () => {
        const k = { style: {}, hidden: false, tap: null,
                    addEventListener: (n, f) => { if (n === "click") k.tap = f; } };
        return k;
      } },
    requestAnimationFrame: (f) => f(), Math, Number, Map, Array, Date,
    Image: function () { this.complete = false; },
    console,
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  const k = timeframe.joltMap;
  k.create("map");
  const finger = (kind, id, x, y) => events[kind]({
    pointerId: id, pointerType: "touch", button: 0,
    clientX: x + 10, clientY: y + 20,        // screen = canvas + offset
    preventDefault() {}, deltaY: 0 });
  return { k, events, finger, pictures, canvas, btn: () => buttons[0],
           timeframe };
}

// How far is the place (lat, lon) from the point (x, y)?
const spacingPx = (k, city, x, y) => {
  const p = k.pastScreen(city.lat, city.lon);
  return Math.hypot(p.x - x, p.y - y);
};
// Which place is under the pixel (x, y)? Via the inverse of the projection.
function cityUnder(k, x, y) {
  const a = k.view();
  const n = 256 * Math.pow(2, a.zoom);
  const wx = (a.middle.lon + 180) / 360 * n;
  const rad = a.middle.lat * Math.PI / 180;
  const wy = (1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2 * n;
  const px = wx + x - 200, py = wy + y - 150;
  const lon = px / n * 360 - 180;
  const kk = Math.PI - 2 * Math.PI * py / n;
  const lat = 180 / Math.PI * Math.atan(0.5 * (Math.exp(kk) - Math.exp(-kk)));
  return { lat, lon };
}

console.log("Ziehen mit einem Finger");
let t = newMap();
t.k.onPoint(48.0, 9.0, 10);
const city = cityUnder(t.k, 200, 150);
t.finger("pointerdown", 1, 200, 150);
for (let i = 1; i <= 10; i++) t.finger("pointermove", 1, 200 + i * 10, 150 + i * 5);
verify(spacingPx(t.k, city, 300, 200) < 1,
       "der Ort unter dem Finger bleibt unter dem Finger, auch bei einem "
       + "Leinwand-Versatz im Fenster",
       spacingPx(t.k, city, 300, 200).toFixed(2) + " px");
t.finger("pointerup", 1, 300, 200);

console.log("\nKneifen");
t = newMap();
t.k.onPoint(48.0, 9.0, 10);
t.finger("pointerdown", 1, 150, 150);
t.finger("pointerdown", 2, 250, 150);
const middleCity = cityUnder(t.k, 200, 150);
for (let i = 1; i <= 10; i++) {            // apart: 100 -> 200 px
  t.finger("pointermove", 1, 150 - i * 5, 150);
  t.finger("pointermove", 2, 250 + i * 5, 150);
}
const a = t.k.view();
verify(Math.abs(a.zoom - 11) < 0.05,
       "doppelter Fingerabstand = eine Zoomstufe mehr", a.zoom.toFixed(3));
verify(spacingPx(t.k, middleCity, 200, 150) < 1,
       "der Ort zwischen den Fingern bleibt zwischen den Fingern",
       spacingPx(t.k, middleCity, 200, 150).toFixed(2) + " px");

console.log("\nKneifen um einen Punkt abseits der Mitte");
t = newMap();
t.k.onPoint(48.0, 9.0, 10);
t.finger("pointerdown", 1, 40, 60);
t.finger("pointerdown", 2, 80, 60);
const corner = cityUnder(t.k, 60, 60);
for (let i = 1; i <= 8; i++) {
  t.finger("pointermove", 1, 40 - i * 2, 60);
  t.finger("pointermove", 2, 80 + i * 2, 60);
}
verify(spacingPx(t.k, corner, 60, 60) < 1,
       "auch in der Ecke bleibt der Inhalt unter den Fingern (frueher: um die "
       + "Kartenmitte gezoomt, der Inhalt wanderte weg)",
       spacingPx(t.k, corner, 60, 60).toFixed(2) + " px");

console.log("\nEin Finger bleibt nach dem Kneifen");
t = newMap();
t.k.onPoint(48.0, 9.0, 10);
t.finger("pointerdown", 1, 100, 150);
t.finger("pointerdown", 2, 300, 150);
for (let i = 1; i <= 5; i++) {
  t.finger("pointermove", 1, 100 + i * 10, 150);
  t.finger("pointermove", 2, 300 - i * 10, 150);
}
t.finger("pointerup", 2, 250, 150);        // the second finger lifts off
const earlier = t.k.view();
t.finger("pointermove", 1, 151, 150);      // the first moves one pixel
const after = t.k.view();
const jump = Math.hypot(
  (after.middle.lon - earlier.middle.lon) * 256 * Math.pow(2, after.zoom) / 360,
  (after.middle.lat - earlier.middle.lat) * 256 * Math.pow(2, after.zoom) / 360);
verify(jump < 3 && Math.abs(after.zoom - earlier.zoom) < 1e-9,
       "ein Pixel Bewegung schiebt die Karte um etwa einen Pixel - nicht um "
       + "die Strecke bis zur Position vor dem Kneifen (der Sprung)",
       jump.toFixed(1) + " px");
const under = cityUnder(t.k, 151, 150);
t.finger("pointermove", 1, 200, 180);
verify(spacingPx(t.k, under, 200, 180) < 1,
       "und der Finger zieht die Karte danach 1:1");
t.finger("pointerup", 1, 200, 180);

console.log("\nZeiger geht verloren");
t = newMap();
t.k.onPoint(48.0, 9.0, 10);
t.finger("pointerdown", 1, 100, 150);
t.finger("pointerdown", 2, 300, 150);
t.events["pointercancel"]({ pointerId: 2 });     // the system takes it away
t.events["pointercancel"]({ pointerId: 1 });
const z0 = t.k.view().zoom;
t.finger("pointerdown", 3, 200, 150);
t.finger("pointermove", 3, 250, 150);
verify(Math.abs(t.k.view().zoom - z0) < 1e-9,
       "nach verlorenen Zeigern bleibt die Karte nicht im Zoommodus: ein neuer "
       + "Finger zieht nur");
t.events["lostpointercapture"]({ pointerId: 3 });
t.finger("pointerdown", 4, 100, 100);
t.finger("pointermove", 4, 100, 100);
verify(true, "lostpointercapture raeumt auf, ohne zu werfen");

console.log("\nMausrad");
t = newMap();
t.k.onPoint(48.0, 9.0, 10);
const radCity = cityUnder(t.k, 330, 70);
t.events["wheel"]({ deltaY: -100, deltaMode: 0, ctrlKey: false,
                        clientX: 340, clientY: 90, preventDefault() {} });
verify(t.k.view().zoom > 10.3 && t.k.view().zoom < 10.5,
       "eine Raste vergroessert um knapp eine halbe Stufe", String(t.k.view().zoom));
verify(spacingPx(t.k, radCity, 330, 70) < 1,
       "und zoomt um den Mauszeiger, nicht um die Mitte",
       spacingPx(t.k, radCity, 330, 70).toFixed(2) + " px");
t.events["wheel"]({ deltaY: 4, deltaMode: 0, ctrlKey: false,
                        clientX: 340, clientY: 90, preventDefault() {} });
verify(Math.abs(t.k.view().zoom - 10.4 + 0.016) < 0.05,
       "ein kleiner Trackpad-Schritt aendert nur ein wenig (proportional)");

console.log("\nGrenzen");
t = newMap();
t.k.onPoint(48.0, 9.0, 17);
t.events["wheel"]({ deltaY: -100, deltaMode: 0, ctrlKey: false,
                        clientX: 200, clientY: 150, preventDefault() {} });
verify(t.k.view().zoom === 17, "nicht ueber die groesste Stufe hinaus");
t.k.onPoint(84.9, 9.0, 4);
t.finger("pointerdown", 1, 200, 150);
t.finger("pointermove", 1, 200, 5000);        // drag far down
const lat = t.k.view().middle.lat;
verify(Number.isFinite(lat) && lat <= 85.06,
       "ueber den Rand der Welt hinaus gibt es keine unsinnigen Breiten", String(lat));

console.log("\nAuto-Zoom auf die Strecke");
// [lon, lat], like the app's geometries.
const stuttgartBerlin = [];
for (let i = 0; i <= 50; i++) {
  stuttgartBerlin.push([9.18 + (13.4 - 9.18) * i / 50, 48.78 + (52.52 - 48.78) * i / 50]);
}
const onScreen = (k, lon, lat) => k.pastScreen(lat, lon);
const inImage = (k, lon, lat, edge) => {
  const p = onScreen(k, lon, lat);
  return p.x >= edge && p.x <= 400 - edge && p.y >= edge && p.y <= 300 - edge;
};

t = newMap();
t.k.setRoute(stuttgartBerlin);
verify(stuttgartBerlin.every((q) => inImage(t.k, q[0], q[1], 0)),
       "eine gesetzte Strecke liegt ganz im Bild, ohne dass jemand onRouteFit ruft");
const z1 = t.k.view().zoom;
verify(z1 > 4 && z1 < 8 && z1 * 4 === Math.floor(z1 * 4),
       "in Viertelstufen und nicht weiter herausgezoomt als noetig", String(z1));
let large = stuttgartBerlin.map((q) => onScreen(t.k, q[0], q[1]));
const xs = large.map((q) => q.x), ys = large.map((q) => q.y);
verify(Math.max(Math.max(...xs) - Math.min(...xs), (Math.max(...ys) - Math.min(...ys)) * 400 / 300) > 400 * 0.7,
       "und fuellt das Bild gut aus (mindestens 70 % der Breite oder Hoehe)");

// Charging stations at the edge must not widen the view.
const earlierZoom = t.k.view().zoom;
t.k.setMarker([{ lat: 40.0, lon: 0.0, kind: "saeule" },
                  { lat: stuttgartBerlin[0][1], lon: stuttgartBerlin[0][0], kind: "start" }]);
verify(t.k.view().zoom === earlierZoom,
       "eine Ladesaeule weit abseits zieht die Karte nicht auf (nur Strecke, Auto, Start, Ziel, Stopps)");

console.log("\nAufzeichnung: die Spur waechst");
t = newMap();
const track = [];
let change = 0, lastZoom = null, outside = 0;
// A trip from Stuttgart to the north, 120 reports, approx. 400 km.
for (let i = 0; i < 120; i++) {
  const lat = 48.78 + i * 0.03, lon = 9.18 + Math.sin(i / 20) * 0.3;
  track.push([lon, lat]);
  t.k.setRoute(track.slice());
  t.k.setMarker([{ lat, lon, kind: "auto", text: "hier" }]);
  const z = t.k.view().zoom;
  if (lastZoom !== null && z !== lastZoom) change++;
  lastZoom = z;
  if (!inImage(t.k, lon, lat, 0)) outside++;
}
verify(outside === 0, "das Auto ist bei jeder Meldung im Bild", String(outside));
verify(change > 0 && change < 25,
       "die Karte zoomt dabei nur gelegentlich, nicht bei jeder Meldung (120 Meldungen)",
       String(change) + " Wechsel");
verify(track.every((q) => inImage(t.k, q[0], q[1], 0)),
       "und am Ende liegt die ganze Spur im Bild");

console.log("\nWer die Karte anfasst, behaelt sie");
t = newMap();
t.k.setRoute(stuttgartBerlin.slice(0, 10));
verify(t.btn().hidden === true, "solange die Karte folgt, gibt es keinen Knopf");
t.finger("pointerdown", 1, 200, 150);
t.finger("pointermove", 1, 150, 120);
t.finger("pointerup", 1, 150, 120);
const shifted = t.k.view();
const long = stuttgartBerlin.slice();
t.k.setRoute(long);                      // the trace grows far beyond the frame
t.k.setMarker([{ lat: 52.5, lon: 13.4, kind: "auto" }]);
const afterwards = t.k.view();
verify(afterwards.zoom === shifted.zoom
       && afterwards.middle.lat === shifted.middle.lat && afterwards.middle.lon === shifted.middle.lon,
       "nach dem Anfassen aendert eine neue Meldung die Ansicht nicht mehr");
verify(t.btn().hidden === false, "stattdessen erscheint der Knopf 'Auf Strecke zoomen'");
t.btn().tap();
verify(long.every((q) => inImage(t.k, q[0], q[1], 0)) && t.btn().hidden === true,
       "ein Tipp darauf zeigt die ganze Strecke und versteckt den Knopf wieder");

t = newMap();
t.k.setRoute(stuttgartBerlin);
t.events["wheel"]({ deltaY: -100, deltaMode: 0, ctrlKey: false,
                        clientX: 200, clientY: 150, preventDefault() {} });
t.k.onRouteFit();
verify(stuttgartBerlin.every((q) => inImage(t.k, q[0], q[1], 0)) && t.btn().hidden === true,
       "onRouteFit (Planen) holt die Ansicht zurueck, auch nach dem Zoomen von Hand");

console.log("\nEine neue Fahrt beginnt von vorn");
t = newMap();
t.k.setRoute(stuttgartBerlin);
t.finger("pointerdown", 1, 200, 150);
t.finger("pointerup", 1, 200, 150);
t.k.setRoute([[9.18, 48.78]]);           // shorter route = different trip
t.k.setMarker([{ lat: 48.78, lon: 9.18, kind: "auto" }]);
verify(t.btn().hidden === true && t.k.view().zoom === 15,
       "nach einer von Hand verschobenen Karte folgt die naechste Aufzeichnung wieder: "
       + "ein einzelner Punkt wird gross gezeigt (Stufe 15)", String(t.k.view().zoom));

console.log("\nGroesse aendert sich");
t = newMap();
t.k.setRoute(stuttgartBerlin);
const zWide = t.k.view().zoom;
t.canvas.clientWidth = 200; t.canvas.clientHeight = 150;   // re-parenting / rotating
t.k.drawNew();
verify(t.k.view().zoom < zWide,
       "in einem kleineren Ausschnitt zoomt die Karte heraus, damit die Strecke passt");
t.canvas.clientWidth = 0;                                      // hidden section
const priorHidden = t.k.view().zoom;
t.k.drawNew();
verify(t.k.view().zoom === priorHidden,
       "ist der Abschnitt versteckt (Breite null), bleibt die Ansicht unberuehrt");

console.log("\nKacheln");
t = newMap();
t.k.onPoint(48.0, 9.0, 10);
verify(t.pictures.length === 0, "ohne geladene Kacheln wird nichts gezeichnet, nichts bricht");

console.log(failure ? `\n${failure} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
process.exit(failure ? 1 : 0);
