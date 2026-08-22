/* Eine kleine Schiebekarte auf einem Canvas.
 *
 * Warum selbst gebaut statt einer Kartenbibliothek: Gebraucht werden
 * Kacheln, eine Linie, ein paar Marker und Zoomen mit Ziehen. Dafür eine
 * Bibliothek einzubinden hiesse, sie mit ins Repo zu legen (kein CDN, das
 * verbietet die Content-Security-Policy) und dauerhaft zu pflegen - für
 * einen Bruchteil ihres Funktionsumfangs. Das hier sind zweihundert Zeilen,
 * die man versteht.
 *
 * Kacheln kommen vom OSM-Tileserver. Das ist für eine selbstgehostete
 * Instanz mit einer Handvoll Aufrufe in Ordnung; die Namensnennung steht
 * unter der Karte, weil sie verlangt ist.
 */
window.joltKarte = (function () {
  "use strict";

  const KACHEL = 256;
  const KACHEL_URL = (z, x, y) => `https://tile.openstreetmap.org/${z}/${x}/${y}.png`;
  const MAX_ZOOM = 17, MIN_ZOOM = 4;

  const FARBEN = {
    route: "#ffc93c", routeRand: "#00000066",
    start: "#57c98a", ziel: "#7c9cc4", reserve: "#e2596a",
    saeule: "#e6ebf0", saeuleBelegt: "#e2596a", auto: "#ffffff",
    // Geplante Stopps heben sich von den übrigen Ladepunkten ab: Auf der
    // Karte ist die Frage nicht "wo gibt es Säulen", sondern "wo halte ich".
    // Eigener Farbton, nicht das Grün des Starts - sonst ist auf einer
    // herausgezoomten Strecke nicht zu sehen, wo die Fahrt beginnt und wo
    // der erste Halt liegt.
    stopp: "#b48ef0",
  };

  let leinwand = null, stift = null;
  let mitte = { lat: 51.0, lon: 10.0 }, zoom = 6;
  let route = [], marker = [];
  const kacheln = new Map();       // "z/x/y" -> Image
  let zeichnenGeplant = false;

  /* ---------- Projektion (Web Mercator) ---------- */

  function nachWelt(lat, lon, z) {
    const n = KACHEL * Math.pow(2, z);
    const x = (lon + 180) / 360 * n;
    const rad = Math.max(-85.05, Math.min(85.05, lat)) * Math.PI / 180;
    const y = (1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2 * n;
    return { x, y };
  }

  function nachGeo(x, y, z) {
    const n = KACHEL * Math.pow(2, z);
    const lon = x / n * 360 - 180;
    const k = Math.PI - 2 * Math.PI * y / n;
    const lat = 180 / Math.PI * Math.atan(0.5 * (Math.exp(k) - Math.exp(-k)));
    return { lat, lon };
  }

  function nachSchirm(lat, lon) {
    const breite = leinwand.clientWidth, hoehe = leinwand.clientHeight;
    const m = nachWelt(mitte.lat, mitte.lon, zoom);
    const p = nachWelt(lat, lon, zoom);
    return { x: p.x - m.x + breite / 2, y: p.y - m.y + hoehe / 2 };
  }

  /* ---------- Kacheln ---------- */

  function kachelHolen(z, x, y) {
    const schluessel = `${z}/${x}/${y}`;
    if (kacheln.has(schluessel)) return kacheln.get(schluessel);

    const bild = new Image();
    bild.decoding = "async";
    bild.onload = () => zeichnenSpaeter();
    // Ein Fehlschlag darf nicht dazu führen, dass ewig nachgeladen wird.
    bild.onerror = () => { bild.fehlgeschlagen = true; };
    bild.src = KACHEL_URL(z, x, y);
    kacheln.set(schluessel, bild);

    // Der Cache wächst sonst über eine lange Sitzung unbegrenzt.
    if (kacheln.size > 400) {
      const aeltester = kacheln.keys().next().value;
      kacheln.delete(aeltester);
    }
    return bild;
  }

  function kachelnZeichnen() {
    const breite = leinwand.clientWidth, hoehe = leinwand.clientHeight;
    const z = Math.round(zoom);
    const massstab = Math.pow(2, zoom - z);
    const groesse = KACHEL * massstab;

    const m = nachWelt(mitte.lat, mitte.lon, z);
    const linksOben = { x: m.x - breite / 2 / massstab, y: m.y - hoehe / 2 / massstab };
    const vonX = Math.floor(linksOben.x / KACHEL);
    const vonY = Math.floor(linksOben.y / KACHEL);
    const bisX = Math.floor((linksOben.x + breite / massstab) / KACHEL);
    const bisY = Math.floor((linksOben.y + hoehe / massstab) / KACHEL);
    const anzahl = Math.pow(2, z);

    for (let x = vonX; x <= bisX; x++) {
      for (let y = vonY; y <= bisY; y++) {
        if (y < 0 || y >= anzahl) continue;
        const xUmlauf = ((x % anzahl) + anzahl) % anzahl;   // Datumsgrenze
        const bild = kachelHolen(z, xUmlauf, y);
        if (!bild.complete || bild.fehlgeschlagen) continue;
        const sx = (x * KACHEL - linksOben.x) * massstab;
        const sy = (y * KACHEL - linksOben.y) * massstab;
        // Ein halber Pixel Überlappung: sonst blitzen zwischen den Kacheln
        // haarfeine Linien durch, wenn der Massstab nicht ganzzahlig ist.
        stift.drawImage(bild, sx, sy, groesse + 0.5, groesse + 0.5);
      }
    }
  }

  /* ---------- Inhalte ---------- */

  function routeZeichnen() {
    if (route.length < 2) return;
    stift.lineJoin = "round";
    stift.lineCap = "round";

    for (const [farbe, breite] of [[FARBEN.routeRand, 7], [FARBEN.route, 4]]) {
      stift.beginPath();
      let angesetzt = false;
      for (let i = 0; i < route.length; i++) {
        const p = nachSchirm(route[i][1], route[i][0]);
        if (!angesetzt) { stift.moveTo(p.x, p.y); angesetzt = true; }
        else stift.lineTo(p.x, p.y);
      }
      stift.strokeStyle = farbe;
      stift.lineWidth = breite;
      stift.stroke();
    }
  }

  function markerZeichnen() {
    for (const m of marker) {
      const p = nachSchirm(m.lat, m.lon);
      const gross = m.typ === "start" || m.typ === "ziel" || m.typ === "reserve"
        || m.typ === "auto" || m.typ === "stopp";
      const r = gross ? 8 : 5;
      stift.beginPath();
      stift.arc(p.x, p.y, r, 0, Math.PI * 2);
      stift.fillStyle = FARBEN[m.typ] || FARBEN.saeule;
      stift.fill();
      stift.strokeStyle = "#101418";
      stift.lineWidth = 2;
      stift.stroke();

      if (m.text && gross) {
        stift.font = "600 12px system-ui, sans-serif";
        const breite = stift.measureText(m.text).width;
        stift.fillStyle = "#101418dd";
        stift.fillRect(p.x + 11, p.y - 9, breite + 10, 18);
        stift.fillStyle = "#e6ebf0";
        stift.fillText(m.text, p.x + 16, p.y + 4);
      }
    }
  }

  /* ---------- Zeichnen ---------- */

  function groesseAnpassen() {
    const verhaeltnis = window.devicePixelRatio || 1;
    const breite = leinwand.clientWidth, hoehe = leinwand.clientHeight;
    if (leinwand.width !== breite * verhaeltnis
        || leinwand.height !== hoehe * verhaeltnis) {
      leinwand.width = breite * verhaeltnis;
      leinwand.height = hoehe * verhaeltnis;
    }
    stift.setTransform(verhaeltnis, 0, 0, verhaeltnis, 0, 0);
  }

  function zeichnen() {
    if (!leinwand) return;
    groesseAnpassen();
    stift.clearRect(0, 0, leinwand.clientWidth, leinwand.clientHeight);
    kachelnZeichnen();
    routeZeichnen();
    markerZeichnen();
  }

  function zeichnenSpaeter() {
    if (zeichnenGeplant) return;
    zeichnenGeplant = true;
    requestAnimationFrame(() => { zeichnenGeplant = false; zeichnen(); });
  }

  /* ---------- Bedienung ---------- */

  function bedienungEinrichten() {
    let zieht = false, letzte = null;
    const zeiger = new Map();
    let letzterAbstand = 0;

    leinwand.addEventListener("pointerdown", (e) => {
      leinwand.setPointerCapture(e.pointerId);
      zeiger.set(e.pointerId, { x: e.clientX, y: e.clientY });
      zieht = true;
      letzte = { x: e.clientX, y: e.clientY };
    });

    leinwand.addEventListener("pointermove", (e) => {
      if (!zeiger.has(e.pointerId)) return;
      zeiger.set(e.pointerId, { x: e.clientX, y: e.clientY });

      if (zeiger.size >= 2) {
        // Kneifen: der Abstand der beiden Finger steuert den Zoom.
        const [a, b] = Array.from(zeiger.values());
        const abstand = Math.hypot(a.x - b.x, a.y - b.y);
        if (letzterAbstand > 0 && abstand > 0) {
          zoomAendern(Math.log2(abstand / letzterAbstand));
        }
        letzterAbstand = abstand;
        return;
      }

      if (!zieht || !letzte) return;
      verschieben(letzte.x - e.clientX, letzte.y - e.clientY);
      letzte = { x: e.clientX, y: e.clientY };
    });

    const loslassen = (e) => {
      zeiger.delete(e.pointerId);
      if (zeiger.size < 2) letzterAbstand = 0;
      if (zeiger.size === 0) { zieht = false; letzte = null; }
    };
    leinwand.addEventListener("pointerup", loslassen);
    leinwand.addEventListener("pointercancel", loslassen);

    leinwand.addEventListener("wheel", (e) => {
      e.preventDefault();
      zoomAendern(e.deltaY < 0 ? 0.5 : -0.5);
    }, { passive: false });
  }

  function verschieben(dx, dy) {
    const m = nachWelt(mitte.lat, mitte.lon, zoom);
    mitte = nachGeo(m.x + dx, m.y + dy, zoom);
    zeichnenSpaeter();
  }

  function zoomAendern(delta) {
    zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom + delta));
    zeichnenSpaeter();
  }

  /* ---------- Öffentlich ---------- */

  function erstellen(id) {
    leinwand = document.getElementById(id);
    if (!leinwand) return false;
    stift = leinwand.getContext("2d");
    bedienungEinrichten();
    window.addEventListener("resize", zeichnenSpaeter);
    zeichnenSpaeter();
    return true;
  }

  function routeSetzen(geometrie) {
    route = geometrie || [];
    zeichnenSpaeter();
  }

  function markerSetzen(liste) {
    marker = liste || [];
    zeichnenSpaeter();
  }

  /** Zoom und Mitte so wählen, dass die ganze Route hineinpasst. */
  function aufRoutePassen(rand) {
    if (!leinwand || route.length < 2) return;
    let minLat = 90, maxLat = -90, minLon = 180, maxLon = -180;
    for (const p of route) {
      minLat = Math.min(minLat, p[1]); maxLat = Math.max(maxLat, p[1]);
      minLon = Math.min(minLon, p[0]); maxLon = Math.max(maxLon, p[0]);
    }
    mitte = { lat: (minLat + maxLat) / 2, lon: (minLon + maxLon) / 2 };

    const breite = leinwand.clientWidth - (rand || 40);
    const hoehe = leinwand.clientHeight - (rand || 40);
    for (let z = MAX_ZOOM; z >= MIN_ZOOM; z--) {
      const a = nachWelt(maxLat, minLon, z);
      const b = nachWelt(minLat, maxLon, z);
      if (Math.abs(b.x - a.x) <= breite && Math.abs(b.y - a.y) <= hoehe) {
        zoom = z;
        break;
      }
      zoom = MIN_ZOOM;
    }
    zeichnenSpaeter();
  }

  function aufPunkt(lat, lon, z) {
    mitte = { lat, lon };
    if (z) zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, z));
    zeichnenSpaeter();
  }

  return { erstellen, routeSetzen, markerSetzen, aufRoutePassen, aufPunkt,
           neuZeichnen: zeichnenSpaeter };
})();
