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

  /* Solange eine Kachel lädt, steht an ihrer Stelle ein Ausschnitt der
   * gröberen Kachel darüber - unscharf, aber da. Ohne das bleibt beim Zoomen
   * und Ziehen die Fläche leer, bis das Netz geantwortet hat, und die Karte
   * "springt" von weiss zu Inhalt. Nur was schon im Speicher liegt wird
   * genommen; geladen wird dafür nichts. */
  function ersatzZeichnen(z, x, y, sx, sy, groesse) {
    for (let dz = 1; dz <= 3 && z - dz >= 0; dz++) {
      const px = Math.floor(x / Math.pow(2, dz));
      const py = Math.floor(y / Math.pow(2, dz));
      const bild = kacheln.get(`${z - dz}/${px}/${py}`);
      if (!bild || !bild.complete || bild.fehlgeschlagen) continue;
      const teil = KACHEL / Math.pow(2, dz);
      const qx = (x - px * Math.pow(2, dz)) * teil;
      const qy = (y - py * Math.pow(2, dz)) * teil;
      stift.drawImage(bild, qx, qy, teil, teil, sx, sy, groesse + 0.5, groesse + 0.5);
      return;
    }
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
        const sx = (x * KACHEL - linksOben.x) * massstab;
        const sy = (y * KACHEL - linksOben.y) * massstab;
        if (!bild.complete || bild.fehlgeschlagen) {
          ersatzZeichnen(z, xUmlauf, y, sx, sy, groesse);
          continue;
        }
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

  /* Eine Geste, egal ob ein, zwei oder drei Finger: Schwerpunkt und Abstand
   * der Finger werden von Schritt zu Schritt verglichen.
   *
   *  - Der Schwerpunkt wandert: die Karte wandert mit (Ziehen, und Ziehen mit
   *    zwei Fingern).
   *  - Der Abstand ändert sich: gezoomt wird **um den Schwerpunkt**, so dass
   *    der Punkt unter den Fingern unter den Fingern bleibt.
   *
   * Frueher stand hier ein Zweig fuer einen Finger und einer fuer zwei. Beim
   * Wechsel dazwischen - ein Finger hebt sich nach dem Kneifen - galt noch die
   * Fingerposition von vor dem Kneifen, und der naechste Schritt schob die
   * Karte um die ganze Strecke dazwischen: der Sprung. Gezoomt wurde ausserdem
   * um die Kartenmitte, nicht um die Finger, so dass der Inhalt unter ihnen
   * wegwanderte. Hier gibt es keinen Zweig: Jedes Hinzukommen und Wegfallen
   * eines Fingers beginnt die Geste neu (`bezug`), und ein Schritt rechnet nur
   * gegen den Schritt davor. */
  function bedienungEinrichten() {
    const zeiger = new Map();
    let bezug = null;          // {x, y, abstand} beim letzten Schritt

    function lokal(e) {
      const r = leinwand.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    }

    function lage() {
      const p = Array.from(zeiger.values());
      if (!p.length) return null;
      const x = p.reduce((a, q) => a + q.x, 0) / p.length;
      const y = p.reduce((a, q) => a + q.y, 0) / p.length;
      const abstand = p.length >= 2 ? Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) : 0;
      return { x, y, abstand };
    }

    leinwand.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      try { leinwand.setPointerCapture(e.pointerId); } catch (f) { /* schon weg */ }
      zeiger.set(e.pointerId, lokal(e));
      bezug = lage();
    });

    leinwand.addEventListener("pointermove", (e) => {
      if (!zeiger.has(e.pointerId)) return;
      zeiger.set(e.pointerId, lokal(e));
      const jetzt = lage();
      if (!bezug) { bezug = jetzt; return; }
      if (jetzt.abstand > 0 && bezug.abstand > 0) {
        zoomUm(Math.log2(jetzt.abstand / bezug.abstand), bezug.x, bezug.y);
      }
      verschieben(bezug.x - jetzt.x, bezug.y - jetzt.y);
      bezug = jetzt;
    });

    // Ein Zeiger, der verloren geht, ohne dass `pointerup` kommt (eine
    // Systemgeste von iOS, ein Anruf), bliebe sonst fuer immer in der Liste:
    // Die Karte hielte einen Finger fuer gedrueckt und bliebe im Zoommodus.
    const loslassen = (e) => {
      if (!zeiger.delete(e.pointerId)) return;
      bezug = lage();
    };
    leinwand.addEventListener("pointerup", loslassen);
    leinwand.addEventListener("pointercancel", loslassen);
    leinwand.addEventListener("lostpointercapture", loslassen);
    window.addEventListener("blur", () => { zeiger.clear(); bezug = null; });

    leinwand.addEventListener("wheel", (e) => {
      e.preventDefault();
      // Proportional zum Rad: Eine Maus rastet in 100er-Schritten (0,4 Stufen),
      // ein Trackpad liefert viele kleine, und Kneifen darauf kommt mit ctrlKey.
      const faktor = e.deltaMode === 1 ? 0.05 : (e.ctrlKey ? 0.01 : 0.004);
      const delta = Math.max(-1, Math.min(1, -e.deltaY * faktor));
      const r = leinwand.getBoundingClientRect();
      zoomUm(delta, e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
  }

  function verschieben(dx, dy) {
    if (!dx && !dy) return;
    const n = KACHEL * Math.pow(2, zoom);
    const m = nachWelt(mitte.lat, mitte.lon, zoom);
    // Nicht ueber den Rand der Welt hinaus: Dort gibt es keine Kacheln, und
    // die Projektion liefert fuer y ausserhalb von [0, n] unsinnige Breiten.
    const y = Math.max(0, Math.min(n, m.y + dy));
    mitte = nachGeo(m.x + dx, y, zoom);
    zeichnenSpaeter();
  }

  /* Zoomen um einen Punkt auf der Leinwand (Pixel): Der Ort unter diesem
   * Punkt liegt danach wieder darunter. */
  function zoomUm(delta, sx, sy) {
    const neu = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom + delta));
    if (neu === zoom) return;
    const dx = sx - leinwand.clientWidth / 2;
    const dy = sy - leinwand.clientHeight / 2;
    const m = nachWelt(mitte.lat, mitte.lon, zoom);
    const unter = nachGeo(m.x + dx, m.y + dy, zoom);
    zoom = neu;
    const u = nachWelt(unter.lat, unter.lon, zoom);
    const n = KACHEL * Math.pow(2, zoom);
    mitte = nachGeo(u.x - dx, Math.max(0, Math.min(n, u.y - dy)), zoom);
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
           neuZeichnen: zeichnenSpaeter,
           // Fuer die Pruefung (tools/check_karte.js): Ansicht lesen und einen
           // Punkt in Pixel umrechnen.
           ansicht: () => ({ mitte: { ...mitte }, zoom }),
           nachSchirm };
})();
