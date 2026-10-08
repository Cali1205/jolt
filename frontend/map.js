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
window.joltMap = (function () {
  "use strict";

  const TILE = 256;
  const TILE_URL = (z, x, y) => `https://tile.openstreetmap.org/${z}/${x}/${y}.png`;
  const MAX_ZOOM = 17, MIN_ZOOM = 4;

  const COLORS = {
    route: "#ffc93c", routeEdge: "#00000066",
    start: "#57c98a", destination: "#7c9cc4", reserve: "#e2596a",
    charger: "#e6ebf0", chargerOccupied: "#e2596a", auto: "#ffffff",
    // Geplante Stopps heben sich von den übrigen Ladepunkten ab: Auf der
    // Karte ist die Frage nicht "wo gibt es Säulen", sondern "wo halte ich".
    // Eigener Farbton, nicht das Grün des Starts - sonst ist auf einer
    // herausgezoomten Strecke nicht zu sehen, wo die Fahrt beginnt und wo
    // der erste Halt liegt.
    stop: "#b48ef0",
  };

  let canvas = null, pen = null;
  let middle = { lat: 51.0, lon: 10.0 }, zoom = 6;
  let route = [], marker = [];
  const tiles = new Map();       // "z/x/y" -> Image
  let drawPlanned = false;
  // Folgt die Ansicht dem Inhalt (Strecke, Auto, Stopps)? Gilt, bis jemand die
  // Karte anfasst: Wer verschiebt, will sie nicht von der naechsten Meldung
  // zurueckgeholt bekommen.
  let follow = true;
  let fitButton = null;

  /* ---------- Projektion (Web Mercator) ---------- */

  function pastWorld(lat, lon, z) {
    const n = TILE * Math.pow(2, z);
    const x = (lon + 180) / 360 * n;
    const rad = Math.max(-85.05, Math.min(85.05, lat)) * Math.PI / 180;
    const y = (1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2 * n;
    return { x, y };
  }

  function pastGeo(x, y, z) {
    const n = TILE * Math.pow(2, z);
    const lon = x / n * 360 - 180;
    const k = Math.PI - 2 * Math.PI * y / n;
    const lat = 180 / Math.PI * Math.atan(0.5 * (Math.exp(k) - Math.exp(-k)));
    return { lat, lon };
  }

  function pastScreen(lat, lon) {
    const extent = canvas.clientWidth, elevation = canvas.clientHeight;
    const m = pastWorld(middle.lat, middle.lon, zoom);
    const p = pastWorld(lat, lon, zoom);
    return { x: p.x - m.x + extent / 2, y: p.y - m.y + elevation / 2 };
  }

  /* ---------- Kacheln ---------- */

  function fetchTile(z, x, y) {
    const keyname = `${z}/${x}/${y}`;
    if (tiles.has(keyname)) return tiles.get(keyname);

    const picture = new Image();
    picture.decoding = "async";
    picture.onload = () => drawLater();
    // Ein Fehlschlag darf nicht dazu führen, dass ewig nachgeladen wird.
    picture.onerror = () => { picture.failed = true; };
    picture.src = TILE_URL(z, x, y);
    tiles.set(keyname, picture);

    // Der Cache wächst sonst über eine lange Sitzung unbegrenzt.
    if (tiles.size > 400) {
      const oldest = tiles.keys().next().value;
      tiles.delete(oldest);
    }
    return picture;
  }

  /* Solange eine Kachel lädt, steht an ihrer Stelle ein Ausschnitt der
   * gröberen Kachel darüber - unscharf, aber da. Ohne das bleibt beim Zoomen
   * und Ziehen die Fläche leer, bis das Netz geantwortet hat, und die Karte
   * "springt" von weiss zu Inhalt. Nur was schon im Speicher liegt wird
   * genommen; geladen wird dafür nichts. */
  function drawFallback(z, x, y, sx, sy, dimension) {
    for (let dz = 1; dz <= 3 && z - dz >= 0; dz++) {
      const px = Math.floor(x / Math.pow(2, dz));
      const py = Math.floor(y / Math.pow(2, dz));
      const picture = tiles.get(`${z - dz}/${px}/${py}`);
      if (!picture || !picture.complete || picture.failed) continue;
      const part = TILE / Math.pow(2, dz);
      const qx = (x - px * Math.pow(2, dz)) * part;
      const qy = (y - py * Math.pow(2, dz)) * part;
      pen.drawImage(picture, qx, qy, part, part, sx, sy, dimension + 0.5, dimension + 0.5);
      return;
    }
  }

  function drawTiles() {
    const extent = canvas.clientWidth, elevation = canvas.clientHeight;
    const z = Math.round(zoom);
    const zoom_scale = Math.pow(2, zoom - z);
    const dimension = TILE * zoom_scale;

    const m = pastWorld(middle.lat, middle.lon, z);
    const leftTop = { x: m.x - extent / 2 / zoom_scale, y: m.y - elevation / 2 / zoom_scale };
    const fromX = Math.floor(leftTop.x / TILE);
    const fromY = Math.floor(leftTop.y / TILE);
    const untilX = Math.floor((leftTop.x + extent / zoom_scale) / TILE);
    const untilY = Math.floor((leftTop.y + elevation / zoom_scale) / TILE);
    const count = Math.pow(2, z);

    for (let x = fromX; x <= untilX; x++) {
      for (let y = fromY; y <= untilY; y++) {
        if (y < 0 || y >= count) continue;
        const xCirculation = ((x % count) + count) % count;   // Datumsgrenze
        const picture = fetchTile(z, xCirculation, y);
        const sx = (x * TILE - leftTop.x) * zoom_scale;
        const sy = (y * TILE - leftTop.y) * zoom_scale;
        if (!picture.complete || picture.failed) {
          drawFallback(z, xCirculation, y, sx, sy, dimension);
          continue;
        }
        // Ein halber Pixel Überlappung: sonst blitzen zwischen den Kacheln
        // haarfeine Linien durch, wenn der Massstab nicht ganzzahlig ist.
        pen.drawImage(picture, sx, sy, dimension + 0.5, dimension + 0.5);
      }
    }
  }

  /* ---------- Inhalte ---------- */

  function drawRoute() {
    if (route.length < 2) return;
    pen.lineJoin = "round";
    pen.lineCap = "round";

    for (const [colour, extent] of [[COLORS.routeEdge, 7], [COLORS.route, 4]]) {
      pen.beginPath();
      let scheduled = false;
      for (let i = 0; i < route.length; i++) {
        const p = pastScreen(route[i][1], route[i][0]);
        if (!scheduled) { pen.moveTo(p.x, p.y); scheduled = true; }
        else pen.lineTo(p.x, p.y);
      }
      pen.strokeStyle = colour;
      pen.lineWidth = extent;
      pen.stroke();
    }
  }

  function drawMarker() {
    for (const m of marker) {
      const p = pastScreen(m.lat, m.lon);
      const large = m.kind === "start" || m.kind === "ziel" || m.kind === "reserve"
        || m.kind === "auto" || m.kind === "stopp";
      const r = large ? 8 : 5;
      pen.beginPath();
      pen.arc(p.x, p.y, r, 0, Math.PI * 2);
      pen.fillStyle = COLORS[m.kind] || COLORS.charger;
      pen.fill();
      pen.strokeStyle = "#101418";
      pen.lineWidth = 2;
      pen.stroke();

      if (m.text && large) {
        pen.font = "600 12px system-ui, sans-serif";
        const extent = pen.measureText(m.text).width;
        pen.fillStyle = "#101418dd";
        pen.fillRect(p.x + 11, p.y - 9, extent + 10, 18);
        pen.fillStyle = "#e6ebf0";
        pen.fillText(m.text, p.x + 16, p.y + 4);
      }
    }
  }

  /* ---------- Zeichnen ---------- */

  function adjustSize() {
    const ratio = window.devicePixelRatio || 1;
    const extent = canvas.clientWidth, elevation = canvas.clientHeight;
    if (canvas.width !== extent * ratio
        || canvas.height !== elevation * ratio) {
      canvas.width = extent * ratio;
      canvas.height = elevation * ratio;
    }
    pen.setTransform(ratio, 0, 0, ratio, 0, 0);
  }

  function draw() {
    if (!canvas) return;
    adjustSize();
    pen.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    drawTiles();
    drawRoute();
    drawMarker();
  }

  function drawLater() {
    if (drawPlanned) return;
    drawPlanned = true;
    requestAnimationFrame(() => { drawPlanned = false; draw(); });
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
  function controlsSetUp() {
    const pointer = new Map();
    let reference = null;          // {x, y, abstand} beim letzten Schritt

    function local(e) {
      const r = canvas.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    }

    function placement() {
      const p = Array.from(pointer.values());
      if (!p.length) return null;
      const x = p.reduce((a, q) => a + q.x, 0) / p.length;
      const y = p.reduce((a, q) => a + q.y, 0) / p.length;
      const spacing = p.length >= 2 ? Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) : 0;
      return { x, y, spacing };
    }

    canvas.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      try { canvas.setPointerCapture(e.pointerId); } catch (f) { /* schon weg */ }
      pointer.set(e.pointerId, local(e));
      reference = placement();
      setFollow(false);
    });

    canvas.addEventListener("pointermove", (e) => {
      if (!pointer.has(e.pointerId)) return;
      pointer.set(e.pointerId, local(e));
      const now_ts = placement();
      if (!reference) { reference = now_ts; return; }
      if (now_ts.spacing > 0 && reference.spacing > 0) {
        zoomAround(Math.log2(now_ts.spacing / reference.spacing), reference.x, reference.y);
      }
      shift(reference.x - now_ts.x, reference.y - now_ts.y);
      reference = now_ts;
    });

    // Ein Zeiger, der verloren geht, ohne dass `pointerup` kommt (eine
    // Systemgeste von iOS, ein Anruf), bliebe sonst fuer immer in der Liste:
    // Die Karte hielte einen Finger fuer gedrueckt und bliebe im Zoommodus.
    const release = (e) => {
      if (!pointer.delete(e.pointerId)) return;
      reference = placement();
    };
    canvas.addEventListener("pointerup", release);
    canvas.addEventListener("pointercancel", release);
    canvas.addEventListener("lostpointercapture", release);
    window.addEventListener("blur", () => { pointer.clear(); reference = null; });

    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      // Proportional zum Rad: Eine Maus rastet in 100er-Schritten (0,4 Stufen),
      // ein Trackpad liefert viele kleine, und Kneifen darauf kommt mit ctrlKey.
      const factor = e.deltaMode === 1 ? 0.05 : (e.ctrlKey ? 0.01 : 0.004);
      const delta = Math.max(-1, Math.min(1, -e.deltaY * factor));
      setFollow(false);
      const r = canvas.getBoundingClientRect();
      zoomAround(delta, e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
  }

  function shift(dx, dy) {
    if (!dx && !dy) return;
    const n = TILE * Math.pow(2, zoom);
    const m = pastWorld(middle.lat, middle.lon, zoom);
    // Nicht ueber den Rand der Welt hinaus: Dort gibt es keine Kacheln, und
    // die Projektion liefert fuer y ausserhalb von [0, n] unsinnige Breiten.
    const y = Math.max(0, Math.min(n, m.y + dy));
    middle = pastGeo(m.x + dx, y, zoom);
    drawLater();
  }

  /* Zoomen um einen Punkt auf der Leinwand (Pixel): Der Ort unter diesem
   * Punkt liegt danach wieder darunter. */
  function zoomAround(delta, sx, sy) {
    const fresh = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom + delta));
    if (fresh === zoom) return;
    const dx = sx - canvas.clientWidth / 2;
    const dy = sy - canvas.clientHeight / 2;
    const m = pastWorld(middle.lat, middle.lon, zoom);
    const under = pastGeo(m.x + dx, m.y + dy, zoom);
    zoom = fresh;
    const u = pastWorld(under.lat, under.lon, zoom);
    const n = TILE * Math.pow(2, zoom);
    middle = pastGeo(u.x - dx, Math.max(0, Math.min(n, u.y - dy)), zoom);
    drawLater();
  }

  /* ---------- Auf den Inhalt passen ---------- */

  // Was die Ansicht zeigen soll: die Strecke, und von den Markern alles, was
  // zur Fahrt gehoert. Ladesaeulen am Rand nicht - sie wuerden die Karte auf
  // Land und Leute herauszoomen.
  const MARKER_FOR_VIEW = ["auto", "start", "ziel", "stopp", "reserve"];
  const EDGE_PX = 36;
  // Unter diesem Anteil der Ansicht ist der Inhalt zu klein, um ihn dort zu lassen.
  const MIN_SHARE = 0.35;

  function contentPoints() {
    const points = route.map((p) => [p[1], p[0]]);      // [lat, lon]
    for (const m of marker) {
      if (MARKER_FOR_VIEW.indexOf(m.kind) !== -1) points.push([m.lat, m.lon]);
    }
    return points.filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]));
  }

  /* Die Ansicht so waehlen, dass der Inhalt hineinpasst.
   *
   * `erzwingen`: immer neu waehlen. Sonst nur, wenn es noetig ist - der
   * Inhalt ragt aus der Ansicht, oder er fuellt weniger als ein Drittel davon.
   * Dazwischen bleibt die Ansicht stehen: Waechst die Spur einer Aufzeichnung,
   * soll die Karte nicht bei jeder Meldung ein wenig zoomen und wandern. */
  function contentFit(force) {
    if (!canvas || !follow) return;
    const extent = canvas.clientWidth, elevation = canvas.clientHeight;
    if (extent < 50 || elevation < 50) return;      // versteckt: spaeter noch einmal
    const points = contentPoints();
    if (!points.length) return;

    // Umrandung in Weltkoordinaten der Stufe 0: dort ist Mercator linear.
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const [lat, lon] of points) {
      const w = pastWorld(lat, lon, 0);
      minX = Math.min(minX, w.x); maxX = Math.max(maxX, w.x);
      minY = Math.min(minY, w.y); maxY = Math.max(maxY, w.y);
    }
    const w0 = maxX - minX, h0 = maxY - minY;

    if (!force) {
      const f = Math.pow(2, zoom);
      const visibleB = extent - 2 * EDGE_PX, visibleH = elevation - 2 * EDGE_PX;
      const m = pastWorld(middle.lat, middle.lon, 0);
      const left_side = (minX - m.x) * f + extent / 2, right = (maxX - m.x) * f + extent / 2;
      const upper = (minY - m.y) * f + elevation / 2, bottom = (maxY - m.y) * f + elevation / 2;
      const inside = left_side >= EDGE_PX / 2 && right <= extent - EDGE_PX / 2
        && upper >= EDGE_PX / 2 && bottom <= elevation - EDGE_PX / 2;
      const share = Math.max(w0 * f / visibleB, h0 * f / visibleH);
      // Ein einzelner Punkt (Beginn einer Aufzeichnung) hat keine Ausdehnung:
      // da genuegt es, wenn er im Bild ist.
      if (inside && (share >= MIN_SHARE || (w0 === 0 && h0 === 0))) return;
    }

    let fresh;
    if (w0 < 1e-9 && h0 < 1e-9) {
      fresh = 15;                                  // ein Punkt: Strassenebene
    } else {
      const factor = Math.min((extent - 2 * EDGE_PX) / Math.max(w0, 1e-9),
                              (elevation - 2 * EDGE_PX) / Math.max(h0, 1e-9));
      // In Viertelstufen abrunden: stabil gegen das Hin und Her bei jeder
      // Kleinigkeit, und die Strecke passt sicher hinein.
      fresh = Math.floor(Math.log2(factor) * 4) / 4;
    }
    zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, fresh));
    middle = pastGeo((minX + maxX) / 2 * Math.pow(2, zoom),
                    (minY + maxY) / 2 * Math.pow(2, zoom), zoom);
    drawLater();
  }

  function showButton() {
    if (!fitButton) return;
    // Nur anbieten, wenn es etwas zu zeigen gibt und die Ansicht nicht schon folgt.
    fitButton.hidden = follow || contentPoints().length === 0;
  }

  function setFollow(at) {
    follow = at;
    showButton();
  }

  function buttonSetUp() {
    const holder = canvas.parentElement;
    if (!holder || !document.createElement) return;
    fitButton = document.createElement("button");
    fitButton.type = "button";
    fitButton.textContent = "Auf Strecke zoomen";
    fitButton.hidden = true;
    fitButton.style.cssText = "position:absolute;top:8px;right:8px;z-index:2;"
      + "padding:6px 10px;border-radius:8px;border:1px solid #ffffff33;"
      + "background:#101418cc;color:#e6ebf0;font:600 12px system-ui,sans-serif;"
      + "cursor:pointer";
    fitButton.addEventListener("click", () => {
      setFollow(true);
      contentFit(true);
    });
    if (holder.style && !holder.style.position) holder.style.position = "relative";
    holder.appendChild(fitButton);
  }

  /* ---------- Öffentlich ---------- */

  function create(id) {
    canvas = document.getElementById(id);
    if (!canvas) return false;
    pen = canvas.getContext("2d");
    controlsSetUp();
    buttonSetUp();
    // Eine andere Breite (Drehen, Umhaengen in eine andere Ansicht) ist ein
    // anderer Ausschnitt: Folgt die Karte dem Inhalt, passt sie neu.
    window.addEventListener("resize", drawNew);
    drawLater();
    return true;
  }

  function drawNew() {
    contentFit(true);
    drawLater();
  }

  function setRoute(geometry) {
    const fresh = geometry || [];
    // Eine kuerzere Strecke als zuvor ist eine neue (andere Fahrt, andere
    // Planung): Dort beginnt das Folgen von vorn, auch wenn die vorige
    // Ansicht von Hand verschoben wurde. Eine wachsende Spur bleibt, wie sie ist.
    const newTrip = fresh.length < route.length || route.length === 0;
    if (newTrip) setFollow(true);
    route = fresh;
    // Eine neue Strecke wird immer neu eingepasst; sonst bliebe ihr erster
    // Punkt in der weiten Ansicht der vorigen stehen.
    contentFit(newTrip);
    showButton();
    drawLater();
  }

  function setMarker(lst) {
    marker = lst || [];
    contentFit(false);
    showButton();
    drawLater();
  }

  /** Zoom und Mitte so waehlen, dass die ganze Route hineinpasst - und die
   *  Ansicht folgt dem Inhalt wieder. */
  function onRouteFit() {
    if (!canvas || route.length < 2) return;
    setFollow(true);
    contentFit(true);
  }

  function onPoint(lat, lon, z) {
    setFollow(false);
    middle = { lat, lon };
    if (z) zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, z));
    drawLater();
  }

  return { create, setRoute, setMarker, onRouteFit, onPoint,
           drawNew,
           // Fuer die Pruefung (tools/check_map.js): Ansicht lesen und einen
           // Punkt in Pixel umrechnen.
           view: () => ({ middle: { ...middle }, zoom }),
           pastScreen };
})();
