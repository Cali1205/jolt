/* A small pannable map on a canvas.
 *
 * Why built in-house instead of a map library: what is needed is
 * tiles, a line, a few markers and zooming by dragging. Including a
 * library for that would mean putting it into the repo (no CDN, the
 * Content-Security-Policy forbids it) and maintaining it for good - for
 * a fraction of its feature set. This is two hundred lines
 * that you understand.
 *
 * Tiles come from the OSM tile server. That is fine for a self-hosted
 * instance with a handful of requests; the attribution is shown
 * below the map because it is required.
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
    // Planned stops stand out from the other charge points: on the
    // map the question is not "where are chargers" but "where do I stop".
    // Own hue, not the green of the start - otherwise on a zoomed-out
    // route you cannot see where the trip begins and where the first
    // stop is.
    stop: "#b48ef0",
  };

  let canvas = null, pen = null;
  let middle = { lat: 51.0, lon: 10.0 }, zoom = 6;
  let route = [], marker = [];
  const tiles = new Map();       // "z/x/y" -> Image
  let drawPlanned = false;
  // Does the view follow the content (route, car, stops)? Holds until someone
  // touches the map: anyone who pans does not want it pulled back by the
  // next update.
  let follow = true;
  let fitButton = null;

  /* ---------- Projection (Web Mercator) ---------- */

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
    const width = canvas.clientWidth, height = canvas.clientHeight;
    const m = pastWorld(middle.lat, middle.lon, zoom);
    const p = pastWorld(lat, lon, zoom);
    return { x: p.x - m.x + width / 2, y: p.y - m.y + height / 2 };
  }

  /* ---------- Tiles ---------- */

  function fetchTile(z, x, y) {
    const key = `${z}/${x}/${y}`;
    if (tiles.has(key)) return tiles.get(key);

    const picture = new Image();
    picture.decoding = "async";
    picture.onload = () => drawLater();
    // A failure must not lead to endless reloading.
    picture.onerror = () => { picture.failed = true; };
    picture.src = TILE_URL(z, x, y);
    tiles.set(key, picture);

    // Otherwise the cache grows without limit over a long session.
    if (tiles.size > 400) {
      const oldest = tiles.keys().next().value;
      tiles.delete(oldest);
    }
    return picture;
  }

  /* While a tile is loading, a section of the coarser tile above it takes its
   * place - blurry, but there. Without it the area stays empty while zooming
   * and dragging until the network has answered, and the map
   * "jumps" from white to content. Only what is already in memory is
   * used; nothing is loaded for this. */
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
    const width = canvas.clientWidth, height = canvas.clientHeight;
    const z = Math.round(zoom);
    const zoom_scale = Math.pow(2, zoom - z);
    const dimension = TILE * zoom_scale;

    const m = pastWorld(middle.lat, middle.lon, z);
    const leftTop = { x: m.x - width / 2 / zoom_scale, y: m.y - height / 2 / zoom_scale };
    const fromX = Math.floor(leftTop.x / TILE);
    const fromY = Math.floor(leftTop.y / TILE);
    const untilX = Math.floor((leftTop.x + width / zoom_scale) / TILE);
    const untilY = Math.floor((leftTop.y + height / zoom_scale) / TILE);
    const count = Math.pow(2, z);

    for (let x = fromX; x <= untilX; x++) {
      for (let y = fromY; y <= untilY; y++) {
        if (y < 0 || y >= count) continue;
        const xCirculation = ((x % count) + count) % count;   // date line
        const picture = fetchTile(z, xCirculation, y);
        const sx = (x * TILE - leftTop.x) * zoom_scale;
        const sy = (y * TILE - leftTop.y) * zoom_scale;
        if (!picture.complete || picture.failed) {
          drawFallback(z, xCirculation, y, sx, sy, dimension);
          continue;
        }
        // Half a pixel of overlap: otherwise hairline gaps flash between the
        // tiles when the scale is not an integer.
        pen.drawImage(picture, sx, sy, dimension + 0.5, dimension + 0.5);
      }
    }
  }

  /* ---------- Content ---------- */

  function drawRoute() {
    if (route.length < 2) return;
    pen.lineJoin = "round";
    pen.lineCap = "round";

    for (const [colour, lineWidth] of [[COLORS.routeEdge, 7], [COLORS.route, 4]]) {
      pen.beginPath();
      let scheduled = false;
      for (let i = 0; i < route.length; i++) {
        const p = pastScreen(route[i][1], route[i][0]);
        if (!scheduled) { pen.moveTo(p.x, p.y); scheduled = true; }
        else pen.lineTo(p.x, p.y);
      }
      pen.strokeStyle = colour;
      pen.lineWidth = lineWidth;
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
        const textWidth = pen.measureText(m.text).width;
        pen.fillStyle = "#101418dd";
        pen.fillRect(p.x + 11, p.y - 9, textWidth + 10, 18);
        pen.fillStyle = "#e6ebf0";
        pen.fillText(m.text, p.x + 16, p.y + 4);
      }
    }
  }

  /* ---------- Drawing ---------- */

  function adjustSize() {
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (canvas.width !== width * ratio
        || canvas.height !== height * ratio) {
      canvas.width = width * ratio;
      canvas.height = height * ratio;
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

  /* ---------- Interaction ---------- */

  /* One gesture, whether one, two or three fingers: centroid and distance
   * of the fingers are compared from step to step.
   *
   *  - The centroid moves: the map moves with it (dragging, and dragging with
   *    two fingers).
   *  - The distance changes: zoom happens **around the centroid**, so that
   *    the point under the fingers stays under the fingers.
   *
   * Earlier there was a branch here for one finger and one for two. When
   * switching between them - one finger lifts after pinching - the finger
   * position from before the pinch still applied, and the next step shoved
   * the map by the whole distance in between: the jump. Zooming was also
   * around the map centre, not around the fingers, so the content under
   * them drifted away. Here there is no branch: every finger added or
   * removed restarts the gesture (`reference`), and a step computes only
   * against the step before. */
  function controlsSetUp() {
    const pointer = new Map();
    let reference = null;          // {x, y, spacing} at the last step

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
      try { canvas.setPointerCapture(e.pointerId); } catch (f) { /* already gone */ }
      pointer.set(e.pointerId, local(e));
      reference = placement();
      setFollow(false);
    });

    canvas.addEventListener("pointermove", (e) => {
      if (!pointer.has(e.pointerId)) return;
      pointer.set(e.pointerId, local(e));
      const current = placement();
      if (!reference) { reference = current; return; }
      if (current.spacing > 0 && reference.spacing > 0) {
        zoomAround(Math.log2(current.spacing / reference.spacing), reference.x, reference.y);
      }
      shift(reference.x - current.x, reference.y - current.y);
      reference = current;
    });

    // A pointer that is lost without `pointerup` arriving (a system gesture of
    // iOS, a phone call) would otherwise stay in the list forever: the map would
    // consider a finger pressed and stay in zoom mode.
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
      // Proportional to the wheel: a mouse clicks in steps of 100 (0.4 levels),
      // a trackpad delivers many small ones, and pinching on it comes with ctrlKey.
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
    // Not beyond the edge of the world: there are no tiles there, and
    // the projection gives nonsensical latitudes for y outside [0, n].
    const y = Math.max(0, Math.min(n, m.y + dy));
    middle = pastGeo(m.x + dx, y, zoom);
    drawLater();
  }

  /* Zoom around a point on the canvas (pixels): the place under this
   * point lies under it again afterwards. */
  function zoomAround(delta, sx, sy) {
    const newZoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom + delta));
    if (newZoom === zoom) return;
    const dx = sx - canvas.clientWidth / 2;
    const dy = sy - canvas.clientHeight / 2;
    const m = pastWorld(middle.lat, middle.lon, zoom);
    const under = pastGeo(m.x + dx, m.y + dy, zoom);
    zoom = newZoom;
    const u = pastWorld(under.lat, under.lon, zoom);
    const n = TILE * Math.pow(2, zoom);
    middle = pastGeo(u.x - dx, Math.max(0, Math.min(n, u.y - dy)), zoom);
    drawLater();
  }

  /* ---------- Fit to content ---------- */

  // What the view should show: the route, and of the markers everything that
  // belongs to the trip. Charging stations at the edge do not - they would zoom
  // the map out to the whole countryside.
  const MARKER_FOR_VIEW = ["auto", "start", "ziel", "stopp", "reserve"];
  const EDGE_PX = 36;
  // Below this share of the view the content is too small to leave it there.
  const MIN_SHARE = 0.35;

  function contentPoints() {
    const points = route.map((p) => [p[1], p[0]]);      // [lat, lon]
    for (const m of marker) {
      if (MARKER_FOR_VIEW.indexOf(m.kind) !== -1) points.push([m.lat, m.lon]);
    }
    return points.filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]));
  }

  /* Choose the view so that the content fits.
   *
   * `force`: always choose anew. Otherwise only when needed - the
   * content sticks out of the view, or fills less than a third of it.
   * In between the view stays put: when the trace of a recording grows,
   * the map should not zoom and drift a little with every update. */
  function contentFit(force) {
    if (!canvas || !follow) return;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (width < 50 || height < 50) return;      // hidden: try again later
    const points = contentPoints();
    if (!points.length) return;

    // Bounding box in world coordinates of level 0: Mercator is linear there.
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const [lat, lon] of points) {
      const w = pastWorld(lat, lon, 0);
      minX = Math.min(minX, w.x); maxX = Math.max(maxX, w.x);
      minY = Math.min(minY, w.y); maxY = Math.max(maxY, w.y);
    }
    const w0 = maxX - minX, h0 = maxY - minY;

    if (!force) {
      const f = Math.pow(2, zoom);
      const visibleB = width - 2 * EDGE_PX, visibleH = height - 2 * EDGE_PX;
      const m = pastWorld(middle.lat, middle.lon, 0);
      const left = (minX - m.x) * f + width / 2, right = (maxX - m.x) * f + width / 2;
      const upper = (minY - m.y) * f + height / 2, bottom = (maxY - m.y) * f + height / 2;
      const inside = left >= EDGE_PX / 2 && right <= width - EDGE_PX / 2
        && upper >= EDGE_PX / 2 && bottom <= height - EDGE_PX / 2;
      const share = Math.max(w0 * f / visibleB, h0 * f / visibleH);
      // A single point (start of a recording) has no width:
      // it suffices for it to be in the picture.
      if (inside && (share >= MIN_SHARE || (w0 === 0 && h0 === 0))) return;
    }

    let targetZoom;
    if (w0 < 1e-9 && h0 < 1e-9) {
      targetZoom = 15;                                  // one point: street level
    } else {
      const factor = Math.min((width - 2 * EDGE_PX) / Math.max(w0, 1e-9),
                              (height - 2 * EDGE_PX) / Math.max(h0, 1e-9));
      // Round down to quarter levels: stable against the back and forth on every
      // little thing, and the route surely fits in.
      targetZoom = Math.floor(Math.log2(factor) * 4) / 4;
    }
    zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, targetZoom));
    middle = pastGeo((minX + maxX) / 2 * Math.pow(2, zoom),
                    (minY + maxY) / 2 * Math.pow(2, zoom), zoom);
    drawLater();
  }

  function showButton() {
    if (!fitButton) return;
    // Only offer it if there is something to show and the view is not already following.
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

  /* ---------- Public ---------- */

  function create(id) {
    canvas = document.getElementById(id);
    if (!canvas) return false;
    pen = canvas.getContext("2d");
    controlsSetUp();
    buttonSetUp();
    // A different width (rotating, moving to another view) is a different
    // section: if the map follows the content, it fits anew.
    window.addEventListener("resize", drawNew);
    drawLater();
    return true;
  }

  function drawNew() {
    contentFit(true);
    drawLater();
  }

  function setRoute(geometry) {
    const newRoute = geometry || [];
    // A shorter route than before is a new one (different trip, different
    // planning): following begins afresh there, even if the previous view
    // was panned by hand. A growing trace stays as it is.
    const newTrip = newRoute.length < route.length || route.length === 0;
    if (newTrip) setFollow(true);
    route = newRoute;
    // A new route is always fitted anew; otherwise its first point would
    // stay put in the wide view of the previous one.
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

  /** Choose zoom and centre so that the whole route fits - and the
   *  view follows the content again. */
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
           // For the check (tools/check_map.js): read the view and convert a
           // point to pixels.
           view: () => ({ middle: { ...middle }, zoom }),
           pastScreen };
})();
