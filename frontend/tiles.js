/* The tiles of the CarPlay overview, drawn in the UI.
 *
 * For a tile CarPlay draws only an image and a title - everything
 * else has to go into the image. Until now Swift drew the images
 * (JoltCarPlaySceneDelegate.tileImage). Here they are created in the
 * canvas instead and travel as PNG in the display model:
 *
 *  - You can see them before getting in the car: the settings show each
 *    tile in each style with sample values, in the browser as in the app.
 *  - A change to the look is a change to this file. The UI comes from the
 *    server (server.url), so a new app build is not
 *    needed.
 *  - If the phone lacks the image (older app, other style), Swift draws
 *    it itself as before.
 *
 * Two styles, both dark and with their own background, so they look the
 * same by day and night (CarPlay recolours only the title under the tile):
 *
 *   "a"  Instrument   Round gauges with a scale: charge level and recuperation
 *                     as a pointer arc, consumption as a bar above a
 *                     centre line, arrival with an arrow, reserve with a range
 *                     bar.
 *   "b"  Telemetrie   Numbers on the left, segment bars as in a race car:
 *                     charge level as a row of LEDs, arrival as deviation from
 *                     zero, stops as a route band, trends as an area.
 *
 * Drawing happens in a square of 120 units; the pixel count comes from
 * `zoom_scale` (2 = 240 pixels). Nothing here touches the DOM except the
 * canvas supplied by the caller - the drawing functions can be checked with
 * a recorder instead of a canvas (tools/check_tiles.js).
 */
window.joltTiles = (function () {
  "use strict";

  const PAGE = 120;
  const actual = (x) => typeof x === "number" && Number.isFinite(x);

  const COLOR = {
    grund1: "#141f2b", grund2: "#0a1119",
    edge: "#27384a", track: "#1c2937", cells: "#233343",
    text: "#f2f7fb", muted: "#8696a6",
    accent: "#37d4ff", good: "#36e08b", warn: "#ffb52e", bad: "#ff4f5f",
  };
  const NUMBER = '"SF Mono", ui-monospace, Menlo, Consolas, "DejaVu Sans Mono", monospace';
  const TEXT = '-apple-system, "SF Pro Text", system-ui, "Segoe UI", Roboto, sans-serif';

  /* ---------- What each colour means ---------- */

  /* Charge level: below 20 % it is time to act, below 10 % urgent. */
  function colorSoc(p) {
    return p >= 35 ? COLOR.good : (p >= 20 ? COLOR.warn : COLOR.bad);
  }
  /* Arrival: minutes relative to the plan. */
  function colorArrival(min) {
    return min <= 2 ? COLOR.good : (min <= 15 ? COLOR.warn : COLOR.bad);
  }
  /* Reserve: kilometres to the charge level the plan does not want to fall below. */
  function colorReserve(km) {
    return km >= 100 ? COLOR.good : (km >= 40 ? COLOR.warn : COLOR.bad);
  }

  /* ---------- Drawing helpers ---------- */

  function rounded(c, x, y, w, h, r) {
    c.beginPath();
    c.moveTo(x + r, y);
    c.lineTo(x + w - r, y); c.arcTo(x + w, y, x + w, y + r, r);
    c.lineTo(x + w, y + h - r); c.arcTo(x + w, y + h, x + w - r, y + h, r);
    c.lineTo(x + r, y + h); c.arcTo(x, y + h, x, y + h - r, r);
    c.lineTo(x, y + r); c.arcTo(x, y, x + r, y, r);
    c.closePath();
  }

  function reason(c) {
    const cycle = c.createLinearGradient(0, 0, 0, PAGE);
    cycle.addColorStop(0, COLOR.grund1);
    cycle.addColorStop(1, COLOR.grund2);
    rounded(c, 0.5, 0.5, PAGE - 1, PAGE - 1, 17);
    c.fillStyle = cycle;
    c.fill();
    c.lineWidth = 1;
    c.strokeStyle = COLOR.edge;
    c.stroke();
  }

  function write_out(c, s, x, y, dimension, colour, opt) {
    const o = opt || {};
    c.font = `${o.weight || 600} ${dimension}px ${o.typeface || TEXT}`;
    c.fillStyle = colour;
    c.textAlign = o.orientation || "left";
    c.textBaseline = o.baseline || "alphabetic";
    c.fillText(s, x, y);
  }

  /* A number as large as possible without exceeding `extent`. */
  function numberFitting(c, s, x, y, startSize, extent, colour, orientation) {
    let g = startSize;
    do {
      c.font = `700 ${g}px ${NUMBER}`;
      g -= 1;
    } while (c.measureText(s).width > extent && g > 10);
    write_out(c, s, x, y, g + 1, colour,
              { typeface: NUMBER, weight: 700, orientation });
  }

  function glow(c, colour, strength) {
    c.shadowColor = colour;
    c.shadowBlur = strength;
  }
  function withoutGlow(c) { c.shadowBlur = 0; c.shadowColor = "transparent"; }

  /* An arc from `begin` to `upto` (radians, clockwise). */
  function arc_len(c, mx, my, r, begin, upto, extent, colour, cap) {
    c.beginPath();
    c.arc(mx, my, r, begin, upto);
    c.lineWidth = extent;
    c.lineCap = cap || "round";
    c.strokeStyle = colour;
    c.stroke();
  }

  function empty(c, title) {
    reason(c);
    c.setLineDash([3, 4]);
    c.strokeStyle = COLOR.cells;
    c.lineWidth = 1.5;
    rounded(c, 12, 12, PAGE - 24, PAGE - 24, 10);
    c.stroke();
    c.setLineDash([]);
    write_out(c, "–", PAGE / 2, PAGE / 2 + 12, 36, COLOR.muted,
              { orientation: "center", typeface: NUMBER });
  }

  /* Trend as a line with an area below. `series` in any unit. */
  function history(c, series, x, y, b, h, colour, options) {
    const o = options || {};
    const low = o.low !== undefined ? o.low : Math.min(...series);
    let high = o.high !== undefined ? o.high : Math.max(...series);
    if (high - low < 1e-6) high = low + 1;
    const px = (i) => x + (i / (series.length - 1)) * b;
    const py = (w) => y + h - ((w - low) / (high - low)) * h;

    if (o.cells) {
      c.strokeStyle = COLOR.cells; c.lineWidth = 1;
      for (let k = 0; k <= 2; k++) {
        const yy = Math.round(y + (h * k) / 2) + 0.5;
        c.beginPath(); c.moveTo(x, yy); c.lineTo(x + b, yy); c.stroke();
      }
    }
    const area = c.createLinearGradient(0, y, 0, y + h);
    area.addColorStop(0, colour + "66");
    area.addColorStop(1, colour + "00");
    c.beginPath();
    c.moveTo(px(0), y + h);
    series.forEach((w, i) => c.lineTo(px(i), py(w)));
    c.lineTo(px(series.length - 1), y + h);
    c.closePath();
    c.fillStyle = area;
    c.fill();

    c.beginPath();
    series.forEach((w, i) => (i ? c.lineTo(px(i), py(w)) : c.moveTo(px(i), py(w))));
    c.lineWidth = 2.5; c.lineJoin = "round"; c.lineCap = "round";
    c.strokeStyle = colour;
    glow(c, colour, 6);
    c.stroke();
    withoutGlow(c);

    const ex = px(series.length - 1), ey = py(series[series.length - 1]);
    c.beginPath(); c.arc(ex, ey, 3.4, 0, Math.PI * 2);
    c.fillStyle = COLOR.text; c.fill();
  }

  /* ---------- Style A: instrument ---------- */

  const START = Math.PI * 0.75;          // 135 degrees: bottom left
  const SPAN = Math.PI * 1.5;          // 270 degrees

  /* Pointer arc with scale: charge level and recuperation. */
  function pointerA(c, val, colour, unit, aux_line) {
    reason(c);
    const mx = PAGE / 2, my = 61, r = 44;
    // Scale: eleven ticks, every 10 %.
    for (let i = 0; i <= 10; i++) {
      const w = START + (SPAN * i) / 10;
      const large = i % 5 === 0;
      c.beginPath();
      c.moveTo(mx + Math.cos(w) * (r - (large ? 13 : 11)), my + Math.sin(w) * (r - (large ? 13 : 11)));
      c.lineTo(mx + Math.cos(w) * (r - 8), my + Math.sin(w) * (r - 8));
      c.lineWidth = large ? 2 : 1;
      c.strokeStyle = large ? COLOR.muted : COLOR.cells;
      c.stroke();
    }
    arc_len(c, mx, my, r, START, START + SPAN, 9, COLOR.track);
    const share = Math.max(0, Math.min(1, val / 100));
    if (share > 0.004) {
      glow(c, colour, 9);
      arc_len(c, mx, my, r, START, START + SPAN * share, 9, colour);
      withoutGlow(c);
      // Pointer head: bright dot at the end of the arc.
      const w = START + SPAN * share;
      c.beginPath(); c.arc(mx + Math.cos(w) * r, my + Math.sin(w) * r, 2.6, 0, Math.PI * 2);
      c.fillStyle = COLOR.text; c.fill();
    }
    numberFitting(c, String(Math.round(val)), mx, my + 11, 36, 58, COLOR.text, "center");
    write_out(c, unit, mx, my + 28, 15, COLOR.muted,
              { orientation: "center", weight: 600 });
    if (aux_line) {
      write_out(c, aux_line, mx, PAGE - 5, 12, COLOR.muted,
                { orientation: "center", weight: 500 });
    }
  }

  function barA(c, d) {
    reason(c);
    const colour = COLOR.accent;
    numberFitting(c, d.text, PAGE / 2, 34, 30, 92, COLOR.text, "center");
    write_out(c, "kWh/100 km", PAGE / 2, 50, 12.5, COLOR.muted,
              { orientation: "center", weight: 600 });
    const x0 = 13, y0 = 58, b = PAGE - 26, h = 40;
    write_out(c, "−30 min", x0, 114, 11.5, COLOR.muted, { weight: 500 });
    write_out(c, "jetzt", x0 + b, 114, 11.5, COLOR.muted,
              { orientation: "right", weight: 500 });
    const vals = (d.bar || []).map((w) => (actual(w) ? w : null));
    const real = vals.filter((w) => w !== null);
    if (!real.length) return;
    const avg = real.reduce((a, w) => a + w, 0) / real.length;
    const peak = Math.max(...real, avg * 1.15, 1);
    const n = vals.length, gap = 4;
    const bb = (b - gap * (n - 1)) / n;
    // Baseline and centre line.
    c.strokeStyle = COLOR.muted; c.lineWidth = 1;
    c.beginPath(); c.moveTo(x0, y0 + h + 0.5); c.lineTo(x0 + b, y0 + h + 0.5); c.stroke();
    vals.forEach((w, i) => {
      const x = x0 + i * (bb + gap);
      if (w === null) {
        c.fillStyle = COLOR.cells; c.fillRect(x, y0 + h - 2, bb, 2);
        return;
      }
      const hh = Math.max(3, (w / peak) * h);
      const f = w > avg * 1.12 ? COLOR.warn : colour;
      const cycle = c.createLinearGradient(0, y0 + h - hh, 0, y0 + h);
      cycle.addColorStop(0, f); cycle.addColorStop(1, f + "55");
      rounded(c, x, y0 + h - hh, bb, hh, 2.2);
      c.fillStyle = cycle; c.fill();
    });
    const ym = y0 + h - (avg / peak) * h;
    c.setLineDash([3, 3]); c.strokeStyle = COLOR.text + "aa"; c.lineWidth = 1;
    c.beginPath(); c.moveTo(x0 - 3, ym); c.lineTo(x0 + b + 3, ym); c.stroke();
    c.setLineDash([]);
  }

  function historyA(c, d, unit, colour) {
    reason(c);
    numberFitting(c, d.text, PAGE / 2, 38, 32, 90, COLOR.text, "center");
    write_out(c, unit, PAGE / 2, 54, 14, COLOR.muted,
              { orientation: "center", weight: 600 });
    if (d.series && d.series.length >= 2) {
      history(c, d.series, 12, 66, PAGE - 24, 38, colour, { cells: false });
    }
  }

  function arrivalA(c, d) {
    reason(c);
    const colour = colorArrival(d.min);
    const pastPlan = Math.abs(d.min) < 1;
    const later = d.min > 0;
    // Symbol: arrow up (later) or down (earlier), check mark for "nach Plan".
    const mx = PAGE / 2;
    c.lineWidth = 4.5; c.lineCap = "round"; c.lineJoin = "round"; c.strokeStyle = colour;
    glow(c, colour, 9);
    c.beginPath();
    if (pastPlan) {
      c.moveTo(mx - 12, 28); c.lineTo(mx - 3, 37); c.lineTo(mx + 13, 18);
    } else if (later) {
      c.moveTo(mx - 12, 32); c.lineTo(mx, 19); c.lineTo(mx + 12, 32);
    } else {
      c.moveTo(mx - 12, 19); c.lineTo(mx, 32); c.lineTo(mx + 12, 19);
    }
    c.stroke();
    withoutGlow(c);
    const long = Math.abs(d.min) >= 60;
    const large = pastPlan ? "im Plan"
      : (long ? d.text.replace("−", "-") : `${later ? "+" : "−"}${Math.abs(d.min)}`);
    numberFitting(c, large, mx, 76, 38, 96, COLOR.text, "center");
    write_out(c, pastPlan ? "Ankunft" : (long ? (later ? "später" : "früher")
              : (later ? "min später" : "min früher")),
              mx, 100, 15, colour, { orientation: "center", weight: 600 });
  }

  function reserveA(c, d) {
    reason(c);
    const colour = colorReserve(d.km);
    numberFitting(c, String(Math.round(d.km)), PAGE / 2, 54, 38, 90, COLOR.text, "center");
    write_out(c, "km bis Reserve", PAGE / 2, 72, 12, COLOR.muted,
              { orientation: "center", weight: 600 });
    // Range bar: 0 to 300 km.
    const x = 14, y = 87, b = PAGE - 28, h = 10;
    rounded(c, x, y, b, h, 4.5); c.fillStyle = COLOR.track; c.fill();
    const share = Math.max(0.02, Math.min(1, d.km / 300));
    glow(c, colour, 7);
    rounded(c, x, y, b * share, h, 4.5); c.fillStyle = colour; c.fill();
    withoutGlow(c);
    c.strokeStyle = COLOR.muted; c.lineWidth = 1;
    [0.33, 0.66].forEach((t) => {
      c.beginPath(); c.moveTo(x + b * t, y + h + 2); c.lineTo(x + b * t, y + h + 6); c.stroke();
    });
  }

  function stopsA(c, d) {
    reason(c);
    numberFitting(c, String(d.count), PAGE / 2, 56, 46, 60, COLOR.text, "center");
    // Dots: one dot per stop, the next one bright.
    const n = Math.min(d.count, 8), spacing = 14;
    const x0 = PAGE / 2 - ((n - 1) * spacing) / 2;
    for (let i = 0; i < n; i++) {
      c.beginPath(); c.arc(x0 + i * spacing, 78, i === 0 ? 4.4 : 3.2, 0, Math.PI * 2);
      c.fillStyle = i === 0 ? COLOR.accent : COLOR.muted;
      if (i === 0) glow(c, COLOR.accent, 7);
      c.fill(); withoutGlow(c);
    }
    if (d.upcoming) {
      write_out(c, "in " + d.upcoming, PAGE / 2, 103, 15, COLOR.accent,
                { orientation: "center", weight: 600 });
    }
  }

  /* ---------- Style B: telemetry ---------- */

  /* The vertical status bar on the left: colour says how things stand. */
  function stripe(c, colour) {
    glow(c, colour, 6);
    rounded(c, 7, 14, 4, PAGE - 28, 2);
    c.fillStyle = colour; c.fill();
    withoutGlow(c);
  }

  /* LED bar: `n` segments, `share` of them lit. */
  function segmente(c, x, y, b, h, n, share, colorFn) {
    const gap = 1.8, sb = (b - gap * (n - 1)) / n;
    const active = Math.round(Math.max(0, Math.min(1, share)) * n);
    for (let i = 0; i < n; i++) {
      const sx = x + i * (sb + gap);
      const at = i < active;
      const f = colorFn(i / (n - 1));
      rounded(c, sx, y, sb, h, 1.2);
      if (at) { glow(c, f, 4); c.fillStyle = f; } else { c.fillStyle = COLOR.track; }
      c.fill();
      withoutGlow(c);
    }
  }

  /* Number on the left, unit small beside it on the same baseline. */
  function numberLeft(c, num, unit, y, startSize, extent) {
    c.font = `700 ${startSize}px ${NUMBER}`;
    let g = startSize;
    const unitWidth = unit ? unit.length * 6.2 + 4 : 0;
    while (c.measureText(num).width + unitWidth > extent && g > 12) {
      g -= 1; c.font = `700 ${g}px ${NUMBER}`;
    }
    const w = c.measureText(num).width;
    write_out(c, num, 17, y, g, COLOR.text, { typeface: NUMBER, weight: 700 });
    if (unit) {
      write_out(c, unit, 17 + w + 4, y, 12, COLOR.muted, { weight: 600 });
    }
  }

  function toolbarB(c, val, colour, unit, gauge) {
    reason(c);
    stripe(c, colour);
    numberLeft(c, String(Math.round(val)), unit, 56, 44, 92);
    segmente(c, 17, 70, PAGE - 31, 17, 16, val / 100, () => colour);
    // Scale below the bar.
    const brands = gauge || ["0", "50", "100"];
    brands.forEach((m, i) => {
      const x = 17 + ((PAGE - 31) * i) / (brands.length - 1);
      write_out(c, m, x, 108, 11.5, COLOR.muted,
                { orientation: i === 0 ? "left" : (i === brands.length - 1 ? "right" : "center"),
                  weight: 500, typeface: NUMBER });
    });
  }

  function socB(c, d) {
    const colour = colorSoc(d.val);
    reason(c);
    stripe(c, colour);
    numberLeft(c, String(Math.round(d.val)), "%", 56, 44, 92);
    // Trend: arrow for the trend relative to the series.
    if (d.series && d.series.length >= 2) {
      const diff = d.series[d.series.length - 1] - d.series[0];
      if (Math.abs(diff) >= 0.5) {
        const uphill = diff > 0;
        c.beginPath();
        // Tip up for rising, down for falling charge level.
        c.moveTo(PAGE - 20, uphill ? 21 : 31); c.lineTo(PAGE - 13, uphill ? 31 : 21); c.lineTo(PAGE - 27, uphill ? 31 : 21);
        c.closePath();
        c.fillStyle = uphill ? COLOR.good : COLOR.warn; c.fill();
      }
    }
    // Segment bar with gradient by fill level: red bottom, yellow middle, green top.
    segmente(c, 17, 70, PAGE - 31, 17, 20, d.val / 100, (t) => colorSoc(t * 100));
    ["0", "50", "100"].forEach((m, i) => {
      write_out(c, m, 17 + ((PAGE - 31) * i) / 2, 108, 11.5, COLOR.muted,
                { orientation: i === 0 ? "left" : (i === 2 ? "right" : "center"),
                  weight: 500, typeface: NUMBER });
    });
  }

  function barB(c, d) {
    reason(c);
    stripe(c, COLOR.accent);
    numberLeft(c, d.text, "", 40, 34, 92);
    write_out(c, "kWh/100 km", 17, 54, 12.5, COLOR.muted, { weight: 600 });
    const x0 = 17, y0 = 62, b = PAGE - 31, h = 38;
    write_out(c, "−30", x0, 114, 11.5, COLOR.muted, { weight: 500 });
    write_out(c, "jetzt", x0 + b, 114, 11.5, COLOR.muted,
              { orientation: "right", weight: 500 });
    const vals = (d.bar || []).map((w) => (actual(w) ? w : null));
    const real = vals.filter((w) => w !== null);
    if (!real.length) return;
    const peak = Math.max(...real, 1);
    c.strokeStyle = COLOR.cells; c.lineWidth = 1;
    for (let k = 0; k <= 2; k++) {
      const yy = Math.round(y0 + (h * k) / 2) + 0.5;
      c.beginPath(); c.moveTo(x0, yy); c.lineTo(x0 + b, yy); c.stroke();
    }
    const n = vals.length, gap = 3.5, bb = (b - gap * (n - 1)) / n;
    vals.forEach((w, i) => {
      const x = x0 + i * (bb + gap);
      if (w === null) { c.fillStyle = COLOR.cells; c.fillRect(x, y0 + h - 2, bb, 2); return; }
      const hh = Math.max(3, (w / peak) * h);
      const last = i === vals.length - 1;
      c.fillStyle = last ? COLOR.text : COLOR.accent + "cc";
      c.fillRect(x, y0 + h - hh, bb, hh);
      c.fillStyle = last ? COLOR.text : COLOR.accent;
      c.fillRect(x, y0 + h - hh, bb, 2.2);
    });
  }

  function historyB(c, d, unit, colour) {
    reason(c);
    stripe(c, colour);
    numberLeft(c, d.text, unit, 44, 34, 92);
    if (d.series && d.series.length >= 2) {
      history(c, d.series, 17, 62, PAGE - 31, 38, colour, { cells: true });
      const high = Math.max(...d.series), low = Math.min(...d.series);
      const fmt = (w) => (Math.abs(w) < 10 ? w.toFixed(1) : String(Math.round(w))).replace(".", ",");
      write_out(c, fmt(high), PAGE - 14, 59, 11.5, COLOR.muted,
                { orientation: "right", weight: 500, typeface: NUMBER });
      write_out(c, fmt(low), PAGE - 14, 114, 11.5, COLOR.muted,
                { orientation: "right", weight: 500, typeface: NUMBER });
    }
  }

  function arrivalB(c, d) {
    const colour = colorArrival(d.min);
    reason(c);
    stripe(c, colour);
    const pastPlan = Math.abs(d.min) < 1;
    const sign = d.min > 0 ? "+" : (d.min < 0 ? "−" : "");
    numberLeft(c, pastPlan ? "Plan" : `${sign}${Math.abs(d.min) < 60 ? Math.abs(d.min) : d.text}`,
              pastPlan || Math.abs(d.min) >= 60 ? "" : "min", 56, 42, 92);
    // Deviation from zero: centre = plan, right = later, left = earlier.
    const x = 17, y = 70, b = PAGE - 31, h = 14, middle = x + b / 2;
    rounded(c, x, y, b, h, 3); c.fillStyle = COLOR.track; c.fill();
    const zone = 30;
    const share = Math.max(-1, Math.min(1, d.min / zone));
    if (!pastPlan) {
      glow(c, colour, 6);
      c.fillStyle = colour;
      if (share > 0) c.fillRect(middle, y, (b / 2) * share, h);
      else c.fillRect(middle + (b / 2) * share, y, (b / 2) * -share, h);
      withoutGlow(c);
    }
    c.fillStyle = COLOR.text; c.fillRect(middle - 1, y - 4, 2, h + 8);
    write_out(c, "früher", x, 108, 12, COLOR.muted, { weight: 500 });
    write_out(c, "später", x + b, 108, 12, COLOR.muted, { orientation: "right", weight: 500 });
  }

  function reserveB(c, d) {
    const colour = colorReserve(d.km);
    reason(c);
    stripe(c, colour);
    numberLeft(c, String(Math.round(d.km)), "km", 56, 44, 92);
    // Ruler: ticks every 50 km up to 300, a mark at the range.
    const x = 17, y = 68, b = PAGE - 31;
    c.strokeStyle = COLOR.cells; c.lineWidth = 1;
    c.beginPath(); c.moveTo(x, y + 12.5); c.lineTo(x + b, y + 12.5); c.stroke();
    for (let k = 0; k <= 6; k++) {
      const xx = Math.round(x + (b * k) / 6) + 0.5;
      c.beginPath(); c.moveTo(xx, y + 7); c.lineTo(xx, y + (k % 2 === 0 ? 20 : 16)); c.stroke();
    }
    const t = Math.max(0, Math.min(1, d.km / 300));
    glow(c, colour, 7);
    c.beginPath();
    c.moveTo(x + b * t, y + 12); c.lineTo(x + b * t - 6, y - 2); c.lineTo(x + b * t + 6, y - 2);
    c.closePath(); c.fillStyle = colour; c.fill();
    withoutGlow(c);
    write_out(c, "bis Reserve", x, 108, 12.5, COLOR.muted, { weight: 500 });
  }

  function stopsB(c, d) {
    reason(c);
    stripe(c, COLOR.accent);
    numberLeft(c, String(d.count), d.count === 1 ? "Stopp" : "Stopps", 56, 44, 92);
    // Route band: start left, destination right, one dot per stop.
    const x = 17, y = 82, b = PAGE - 31;
    c.strokeStyle = COLOR.cells; c.lineWidth = 3; c.lineCap = "round";
    c.beginPath(); c.moveTo(x, y); c.lineTo(x + b, y); c.stroke();
    const kms = d.kms || [];
    const end = Math.max(...kms, 1);
    kms.forEach((km, i) => {
      const xx = x + Math.min(1, km / (end * 1.12)) * b;
      c.beginPath(); c.arc(xx, y, i === 0 ? 5 : 3.6, 0, Math.PI * 2);
      c.fillStyle = i === 0 ? COLOR.accent : COLOR.muted;
      if (i === 0) glow(c, COLOR.accent, 8);
      c.fill(); withoutGlow(c);
    });
    c.fillStyle = COLOR.text; c.fillRect(x + b - 2, y - 7, 3, 14);
    if (d.upcoming) {
      write_out(c, "in " + d.upcoming, x, 108, 14, COLOR.accent, { weight: 600 });
    }
  }

  /* ---------- Mapping ---------- */

  const SLOTS = ["soc", "arrival", "reserve", "consumption", "aux", "regen", "stops"];

  const DRAWER = {
    a: {
      soc: (c, d) => pointerA(c, d.val, colorSoc(d.val), "%", d.source === "gerechnet" ? "gerechnet" : null),
      arrival: arrivalA,
      reserve: reserveA,
      consumption: barA,
      aux: (c, d) => historyA(c, d, "kW", COLOR.accent),
      regen: (c, d) => pointerA(c, d.val, COLOR.accent, "%", null),
      stops: stopsA,
    },
    b: {
      soc: socB,
      arrival: arrivalB,
      reserve: reserveB,
      consumption: barB,
      aux: (c, d) => historyB(c, d, "kW", COLOR.accent),
      regen: (c, d) => toolbarB(c, d.val, COLOR.accent, "%", ["0", "50", "100"]),
      stops: stopsB,
    },
  };

  const STYLES = [
    { id: "klassisch", name: "Klassisch (Swift zeichnet)" },
    { id: "a", name: "A – Instrument" },
    { id: "b", name: "B – Telemetrie" },
  ];

  /* From the display model the values per tile - or null if something is
   * missing there. Nothing is substituted: a tile without a value stays empty. */
  function records(m, series_list) {
    const r = series_list || {};
    const origin_of = {};
    if (!m) return origin_of;
    origin_of.soc = m.soc && actual(m.soc.percent)
      ? { val: m.soc.percent, source: m.soc.source, series: r.soc || null } : null;
    origin_of.arrival = m.arrival && actual(m.arrival.min)
      ? { min: m.arrival.min, text: m.arrival.text } : null;
    origin_of.reserve = m.reserve && actual(m.reserve.km) ? { km: m.reserve.km, text: m.reserve.text } : null;

    const v = m.history;
    const timeframe = v && Array.isArray(v.timeframe) ? v.timeframe.filter((f) => f && actual(f.kwh100)) : [];
    const hasBar = !!(v && Array.isArray(v.bar) && v.bar.some(actual));
    origin_of.consumption = timeframe.length || hasBar
      ? { text: timeframe.length ? timeframe[0].text : "–", bar: hasBar ? v.bar : null } : null;

    const aux = m.aux;
    const auxKw = aux && actual(aux.kw) ? aux.kw : null;
    origin_of.aux = auxKw !== null
      ? { val: auxKw, text: String(auxKw.toFixed(1)).replace(".", ","), series: r.aux || null } : null;

    origin_of.regen = v && v.regen && actual(v.regen.percent)
      ? { val: v.regen.percent, series: r.regen || null } : null;

    const lst = Array.isArray(m.stopList) ? m.stopList : [];
    origin_of.stops = lst.length ? {
      count: lst.length,
      upcoming: lst[0] && lst[0].kmText ? lst[0].kmText : null,
      kms: lst.map((s) => s.km).filter(actual),
    } : null;
    return origin_of;
  }

  /* Draw a tile onto a prepared 2D context. */
  function draw(look, slot, d, c) {
    const table = DRAWER[look];
    c.save();
    try {
      if (!table || !table[slot] || !d) empty(c, slot);
      else table[slot](c, d);
    } finally { c.restore(); }
  }

  /* A tile as PNG (Base64 without header).
   * `generator(pixel)` returns a canvas with edge length `pixel`. */
  function tilePng(look, slot, d, generator, zoom_scale) {
    const ms = zoom_scale || 2;
    const canvas = generator(PAGE * ms);
    const c = canvas.getContext("2d");
    c.scale(ms, ms);
    draw(look, slot, d, c);
    const url = canvas.toDataURL("image/png");
    return String(url).replace(/^data:image\/png;base64,/, "");
  }

  function browserCanvas(pixel) {
    const k = document.createElement("canvas");
    k.width = pixel; k.height = pixel;
    return k;
  }

  /* All seven tiles of a style. A tile without a value is drawn as an empty
   * one (dashed frame, dash): it should match the style and not
   * look like a foreign body from Swift. Fixed places - a missing value
   * does not make the others move up. */
  function pictures(look, m, series_list, generator) {
    if (!DRAWER[look]) return null;
    const every = records(m, series_list);
    const origin_of = {};
    for (const slot of SLOTS) {
      origin_of[slot] = tilePng(look, slot, every[slot] || null, generator || browserCanvas, 2);
    }
    return origin_of;
  }

  /* Sample values for the preview in the settings and for checks. */
  function probe() {
    return {
      m: {
        soc: { percent: 68, text: "68 %", source: "gemessen" },
        arrival: { min: 12, text: "+12 min" },
        reserve: { km: 134, text: "134 km" },
        history: {
          timeframe: [{ min: 5, kwh100: 17.4, kw: 14.1, text: "17,4", kwText: "14,1" }],
          bar: [15.2, 18.9, 16.4, 21.7, 17.8, 17.4],
          regen: { percent: 23, mins: 60 },
        },
        aux: { kw: 1.8, text: "1,8 kW" },
        stopList: [
          { name: "Fastned A7", km: 41, kmText: "41 km" },
          { name: "Ionity Hildesheim", km: 188, kmText: "188 km" },
        ],
      },
      series_list: {
        soc: [74, 73.6, 73.1, 72.4, 71.9, 71.2, 70.6, 70.1, 69.2, 68.6, 68],
        aux: [1.1, 1.3, 1.2, 1.9, 2.4, 2.1, 1.8, 1.6, 1.9, 1.8],
        regen: [18, 19, 21, 20, 22, 23],
      },
    };
  }

  return { PAGE, SLOTS, STYLES, records, draw, tilePng, pictures, probe, browserCanvas,
           colorSoc, colorArrival, colorReserve };
})();
