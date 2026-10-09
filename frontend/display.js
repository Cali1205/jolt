/* The display model: the state of the trip as a few numbers and texts.
 *
 * For everything that is not the UI itself - a Live Activity, a CarPlay
 * dashboard, a widget, a CarPlay template (see konzept-ios-app.md, section
 * "CarPlay"). These displays have room for five lines, not for a chart, and
 * they calculate nothing: they show what is stored here.
 *
 * Deliberately without DOM, without `window.jolt` and without network: a pure
 * function `state -> model` can be checked without Swift, without a Mac and
 * without Apple (tools/check_display.js). The Swift plugin comes later and
 * only calls `setTarget` (set target).
 *
 * The source is the state delivered by `GET /api/live/{id}` and the WebSocket -
 * nothing that has not already been calculated. Traffic is not part of it: it
 * only appears in the planning response and is not stored.
 */
window.joltDisplay = (function () {
  "use strict";

  // Apple allows a CarPlay app of the "Driving task" category to update the
  // display at most every ten seconds. Live Activities have their own limits;
  // ten seconds is more than enough for them anyway.
  const MIN_SPACING_MS = 15000;
  // Even if nothing changes, a message arrives regularly: the model carries
  // `stand` (timestamp), and a display that shows its age must not look "old"
  // just because the charge level happens to stay calm.
  const HEARTBEAT_MS = 60000;

  const MINUS = "−";

  function num(value, digits) {
    return Number(value).toLocaleString("de-DE", {
      minimumFractionDigits: digits || 0, maximumFractionDigits: digits || 0 });
  }

  const actual = (x) => typeof x === "number" && Number.isFinite(x);

  /* "+12 min", "nach Plan" (on schedule), "−5 min", "+1 h 05". */
  function shiftText(mins) {
    const amount = Math.round(Math.abs(mins));
    if (amount < 1) return "nach Plan";
    const sign = mins > 0 ? "+" : MINUS;
    if (amount < 60) return `${sign}${amount} min`;
    return `${sign}${Math.floor(amount / 60)} h ${String(amount % 60).padStart(2, "0")}`;
  }

  function kmText(km) {
    if (km < 1) return "gleich";
    return km < 10 ? `${num(km, 1)} km` : `${num(Math.round(km))} km`;
  }

  /* ---------- What the car does not show ----------
   *
   * The on-board computer shows consumption since start and since refuelling.
   * Here are things it does not show: the consumption of the last minute, the
   * last five, thirty, sixty - and what the auxiliary consumers draw.
   *
   * Everything comes from quantities the UI keeps anyway: the consumption
   * trace (time, energy counter, GPS distance) and the latest readings.
   * Energy from the counters (0.117 Wh resolution), distance from the GPS -
   * for the same reason as in the history chart: the odometer only resolves
   * in whole kilometres and is no good for a single minute. */
  const TIMEFRAME_MIN = [1, 5, 30, 60];
  // A window only counts if the trace covers it: anyone who shows a "30-minute
  // average" after twelve minutes of driving shows a twelve-minute average
  // under a false name.
  const TIMEFRAME_COVERAGE = 0.7;
  // The last point must not be older than this, otherwise the trace has
  // stalled (dongle gone) and the "now" is an old one.
  const TRACK_FRESH_MS = 45000;
  const MIN_KM = 0.3;
  const TRIP_MIN_KM = 1;
  // This is how old a read value may be. Rarely read values (climate) only
  // arrive every few minutes.
  const VALUE_OLD_MS = 15 * 60000;

  const MIN_MS = 60000;
  const BAR_NUMBER = 6;      // six bars of five minutes each = the last thirty

  function energyAndDistance(points, fromMs) {
    const p = points.filter((x) => x.timestamp >= fromMs && actual(x.gps) && actual(x.net));
    if (p.length < 2) return null;
    const at_first = p[0], final = p[p.length - 1];
    const duration = final.timestamp - at_first.timestamp;
    if (duration <= 0) return null;
    return { at_first, final, duration, km: final.gps - at_first.gps,
             kwh: final.net - at_first.net };
  }

  function historyModel(track, now) {
    if (!Array.isArray(track) || track.length < 2) return null;
    const last = track[track.length - 1];
    if (!last || !actual(last.timestamp) || now - last.timestamp > TRACK_FRESH_MS) return null;

    const windows = TIMEFRAME_MIN.map((min) => {
      const e = energyAndDistance(track, now - min * MIN_MS);
      const empty = { min, kwh100: null, kw: null, text: "–", kwText: "–" };
      if (!e || e.duration < TIMEFRAME_COVERAGE * min * MIN_MS) return empty;
      const kw = e.kwh / (e.duration / 3600000);
      const kwh100 = e.km >= MIN_KM ? e.kwh / e.km * 100 : null;
      return { min,
               kwh100: kwh100 === null ? null : Math.round(kwh100 * 10) / 10,
               kw: Math.round(kw * 10) / 10,
               text: kwh100 === null ? "–" : num(kwh100, 1),
               kwText: num(kw, 1) };
    });

    // The last thirty minutes in bars of five minutes, oldest first. A bar
    // without distance (standstill, traffic light) is a gap, not a zero.
    const bar = [];
    for (let i = BAR_NUMBER - 1; i >= 0; i--) {
      const bucketEnd = now - i * 5 * MIN_MS;
      const begin = bucketEnd - 5 * MIN_MS;
      const p = track.filter((x) => x.timestamp >= begin && x.timestamp <= bucketEnd && actual(x.gps) && actual(x.net));
      let barValue = null;
      if (p.length >= 2) {
        const km = p[p.length - 1].gps - p[0].gps;
        const duration = p[p.length - 1].timestamp - p[0].timestamp;
        if (km >= MIN_KM && duration >= 2 * MIN_MS) {
          barValue = Math.round((p[p.length - 1].net - p[0].net) / km * 1000) / 10;
        }
      }
      bar.push(barValue);
    }
    const hasBar = bar.some((b) => b !== null);

    // Regeneration: how much of the drawn energy came back, over the last
    // hour (or as long as the trace allows, at least five minutes).
    let regen = null;
    const r = track.filter((x) => x.timestamp >= now - 60 * MIN_MS && actual(x.disch) && actual(x.chg));
    if (r.length >= 2) {
      const duration = r[r.length - 1].timestamp - r[0].timestamp;
      const disch = r[r.length - 1].disch - r[0].disch;
      const chg = r[r.length - 1].chg - r[0].chg;
      if (duration >= 5 * MIN_MS && disch > 0.2 && chg >= 0) {
        regen = { percent: Math.min(100, Math.round(chg / disch * 100)),
                  mins: Math.round(duration / MIN_MS) };
      }
    }

    // The average of the whole trip so far (running mean): the number that
    // stands above the consumption bars. It uses the same energy and GPS
    // distance as the windows; below a kilometre it would still jump about.
    let trip = null;
    const t = energyAndDistance(track, 0);
    if (t && t.km >= TRIP_MIN_KM && t.kwh > 0) {
      const kwh100 = t.kwh / t.km * 100;
      trip = { kwh100: Math.round(kwh100 * 10) / 10,
               km: Math.round(t.km * 10) / 10, text: num(kwh100, 1) };
    }

    if (!windows.some((f) => f.kw !== null) && !hasBar && !regen && !trip) return null;
    return { timeframe: windows, bar: hasBar ? bar : null, regen, trip };
  }

  function valueFresh(vals, name, now) {
    const w = vals && vals[name];
    return w && actual(w.val) && now - w.timestamp <= VALUE_OLD_MS ? w.val : null;
  }

  /* The auxiliary consumers: what the car draws without driving. The measured
   * value (`aux_load_kw`) beats the approximation from standstill.
   * Heating (PTC) and A/C compressor are added if they were read: they are
   * the two big consumers one can influence oneself. The heater power is
   * current times pack voltage - an approximation, not a measurement. */
  function auxModel(vals, approximation, now) {
    let kw = valueFresh(vals, "aux_load_kw", now);
    let source = "gemessen";
    if (kw === null && approximation && actual(approximation.kw) && now - approximation.timestamp <= VALUE_OLD_MS) {
      kw = approximation.kw;
      source = "geschaetzt";
    }
    const ptcA = valueFresh(vals, "ptc_current_a", now);
    const voltage = valueFresh(vals, "voltage_v", now);
    const heating = (ptcA !== null && voltage !== null) ? ptcA * voltage / 1000 : null;
    const compressorW = valueFresh(vals, "compressor_w", now);
    const climate = compressorW !== null ? compressorW / 1000 : null;
    const batterie = valueFresh(vals, "batterie_c", now);

    if (kw === null && heating === null && climate === null && batterie === null) return null;
    const rounded = (x) => (x === null ? null : Math.round(x * 10) / 10);
    return {
      kw: rounded(kw), text: kw === null ? null : `${num(kw, 1)} kW`, source,
      heatingKw: rounded(heating),
      heatingText: heating === null ? null : `${num(heating, 1)} kW`,
      climateKw: rounded(climate),
      climateText: climate === null ? null : `${num(climate, 1)} kW`,
      batterieC: batterie === null ? null : Math.round(batterie),
      batterieText: batterie === null ? null : `${num(Math.round(batterie))} °C`,
    };
  }

  /* The charging stops of the trip that still lie ahead - for the CarPlay list.
   *
   * Distance from the current location, not from the start: whoever sits at
   * the wheel asks "how much further", not "at which kilometre". Without a
   * position on the route there is no distance, and an invented one would be
   * worse than no list. At most eight: more fits in no template. */
  const STOPS_MAX = 8;

  function stopListModel(plan, km) {
    if (km === null || !plan || !Array.isArray(plan.stops)) return null;
    const stops = [];
    for (const s of plan.stops) {
      if (!s || !actual(s.km_on_route)) continue;
      if (s.km_on_route < km - 0.5) continue;          // already passed
      const remaining = Math.max(0, s.km_on_route - km);
      const at = actual(s.arrival_soc) ? Math.round(s.arrival_soc) : null;
      const downhill = actual(s.departure_soc) ? Math.round(s.departure_soc) : null;
      const min = actual(s.charge_time_minutes) ? Math.round(s.charge_time_minutes) : null;
      stops.push({
        name: typeof s.name === "string" && s.name ? s.name : "Ladestopp",
        km: Math.round(remaining * 10) / 10, kmText: kmText(remaining),
        arrivalSoc: at, arrivalSocText: at === null ? null : `${num(at)} %`,
        departureSocText: downhill === null ? null : `${num(downhill)} %`,
        chargeTimeMin: min, chargeTimeText: min === null ? null : `${num(min)} min`,
        operator: typeof s.operator === "string" && s.operator ? s.operator : null,
        powerKw: actual(s.max_kw) ? Math.round(s.max_kw) : null,
      });
      if (stops.length >= STOPS_MAX) break;
    }
    return stops.length ? stops : null;
  }

  /* The model for a state - or null if there is nothing to show.
   *
   * If something is missing, it is missing in the model: no charging stop for
   * a recording, no arrival without a plan. Nothing is replaced or estimated -
   * a display in the car that invents a field is worse than one that leaves
   * it out. */
  function model(z, now, extras) {
    // In JavaScript an array is also an object - and not a state.
    if (!z || typeof z !== "object" || Array.isArray(z)) return null;
    const as_of = actual(now) ? now : Date.now();
    const m = { version: 1, as_of,
                soc: null, reserve: null, stop: null, arrival: null, rest: null };

    if (actual(z.actual_soc)) {
      // Where the number comes from belongs with it: measured, calculated or
      // the last measurement. Whoever reads it at the wheel should know what it is.
      const source = z.soc_source || (z.soc_reported === false ? "gerechnet" : "gemessen");
      m.soc = { percent: Math.round(z.actual_soc * 10) / 10,
                text: `${num(Math.round(z.actual_soc))} %`, source };
    }

    const km = actual(z.km_on_route) ? z.km_on_route : null;
    // `plan_soc` (planned SoC) only exists with a plan. Without it (recording),
    // remainder, reserve, stop and arrival are not numbers but gaps.
    const hasPlan = actual(z.plan_soc);

    if (hasPlan && km !== null && actual(z.reserve_at_km) && z.reserve_at_km >= km) {
      const remaining = z.reserve_at_km - km;
      m.reserve = { km: Math.round(remaining * 10) / 10, text: kmText(remaining) };
    }

    const s = z.next_stop;
    if (hasPlan && km !== null && s && actual(s.km_on_route) && s.km_on_route >= km) {
      const remaining = s.km_on_route - km;
      // Expected (extrapolated with the measured consumption), otherwise planned.
      const soc = actual(s.expected_soc) ? s.expected_soc
                : (actual(s.planned_soc) ? s.planned_soc : null);
      m.stop = { name: typeof s.name === "string" && s.name ? s.name : "Ladestopp",
                  km: Math.round(remaining * 10) / 10, kmText: kmText(remaining),
                  arrivalSoc: soc === null ? null : Math.round(soc),
                  arrivalSocText: soc === null ? null : `${num(Math.round(soc))} %`,
                  planned: !actual(s.expected_soc) };
    }

    if (hasPlan && actual(z.arrival_shift_min)) {
      m.arrival = { min: Math.round(z.arrival_shift_min),
                    text: shiftText(z.arrival_shift_min) };
    }

    if (hasPlan && actual(z.remaining_km)) {
      m.rest = { km: Math.round(z.remaining_km * 10) / 10,
                 text: `${num(Math.round(z.remaining_km))} km` };
    }

    // One line for the smallest display (Dynamic Island, Apple Watch format):
    // "72 % · Stopp in 41 km (18 %)".
    const parts = [];
    if (m.soc) parts.push(m.soc.text);
    if (m.stop) {
      parts.push(`Stopp in ${m.stop.kmText}` +
                 (m.stop.arrivalSocText ? ` (${m.stop.arrivalSocText})` : ""));
    } else if (m.reserve) {
      parts.push(`Reserve in ${m.reserve.text}`);
    }
    m.short = parts.length ? parts.join(" · ") : "Keine Werte";

    // History and auxiliary consumers: only if the UI supplies them.
    // If something is missing, the field is missing.
    const extra = extras || {};
    m.history = historyModel(extra.track, as_of);
    m.aux = auxModel(extra.vals, extra.aux, as_of);
    m.stopList = hasPlan ? stopListModel(extra.plan, km) : null;
    return m;
  }

  /* The sender: passes models on to a target (the Swift plugin), but not more
   * often than allowed and not without there being something new.
   *
   *  - at most every `spacingMs`; whatever arrives in between waits, and only
   *    the newest is sent,
   *  - unchanged (apart from `stand`) is not sent - except as a heartbeat
   *    after `heartbeatMs`,
   *  - `finish()` reports `null`: the trip is over, the display should
   *    disappear,
   *  - a target that is missing or fails aborts nothing: the UI keeps
   *    running, even if the car sees nothing of it. */
  function sender(options) {
    const opt = options || {};
    const spacingMs = actual(opt.spacingMs) ? opt.spacingMs : MIN_SPACING_MS;
    const heartbeatMs = actual(opt.heartbeatMs) ? opt.heartbeatMs : HEARTBEAT_MS;
    const schedule = opt.schedule || ((f, ms) => setTimeout(f, ms));
    const remove = opt.remove || ((t) => clearTimeout(t));
    const nowFn = opt.now_ts || (() => Date.now());

    let destination = typeof opt.destination === "function" ? opt.destination : null;
    let latestTime = null;
    let lastContent = null;
    let waiting = null;
    let clock = null;
    let ended_at = true;           // only the first message starts a display
    let errorReported = false;

    function contents(m) {
      const { as_of, ...rest } = m;
      return JSON.stringify(rest);
    }

    function send(m) {
      latestTime = m === null ? nowFn() : m.as_of;
      lastContent = m === null ? null : contents(m);
      if (!destination) return;
      try {
        const response = destination(m);
        if (response && typeof response.catch === "function") {
          response.catch((f) => errorRemember(f));
        }
      } catch (f) { errorRemember(f); }
    }

    function errorRemember(f) {
      // Say it once, not with every message: the same line every ten seconds
      // would fill the log of a long trip.
      if (errorReported) return;
      errorReported = true;
      console.log("[anzeige] Ziel meldet einen Fehler:", f && f.message ? f.message : f);
    }

    function sendWaiting() {
      clock = null;
      const m = waiting;
      waiting = null;
      if (m && !ended_at) send(m);
    }

    return {
      setTarget(f) { destination = typeof f === "function" ? f : null; },

      report(z, extras) {
        const now = nowFn();
        const m = model(z, now, extras);
        if (m === null) return;
        ended_at = false;
        const same = lastContent !== null && contents(m) === lastContent;
        const elapsed = latestTime === null ? Infinity : now - latestTime;

        if (same && elapsed < heartbeatMs) { waiting = null; return; }
        if (elapsed >= spacingMs) {
          if (clock !== null) { remove(clock); clock = null; }
          waiting = null;
          send(m);
          return;
        }
        // Too early: remember the newest and send at the permitted time.
        waiting = m;
        if (clock === null) clock = schedule(sendWaiting, spacingMs - elapsed);
      },

      finish() {
        if (clock !== null) { remove(clock); clock = null; }
        waiting = null;
        if (ended_at) return;
        ended_at = true;
        send(null);
      },
    };
  }

  /* ---------- History for the tiles ----------
   *
   * The tiles "Ladestand", "Nebenverbraucher" and "Rekuperation" show a
   * line: the last thirty minutes. The model only carries the value of now,
   * so this part remembers the samples. A gap of more than three minutes
   * starts the series anew - that is a different trip.
   * Pure function on a state that the caller holds: this way it can be
   * checked without a clock. */
  const SERIES_TIMEFRAME_MS = 30 * 60000;
  const SERIES_GAP_MS = 3 * 60000;

  function seriesListAppend(series_list, m, now) {
    const r = series_list || { points: [] };
    const last = r.points[r.points.length - 1];
    if (last && now - last.timestamp > SERIES_GAP_MS) r.points = [];
    r.points.push({
      timestamp: now,
      soc: m && m.soc && actual(m.soc.percent) ? m.soc.percent : null,
      aux: m && m.aux && actual(m.aux.kw) ? m.aux.kw : null,
      regen: m && m.history && m.history.regen && actual(m.history.regen.percent)
        ? m.history.regen.percent : null,
    });
    r.points = r.points.filter((p) => now - p.timestamp <= SERIES_TIMEFRAME_MS);
    return r;
  }

  /* The series per tile; fewer than two points are not a line. */
  function seriesListExcerpt(series_list) {
    const result = {};
    for (const name of ["soc", "aux", "regen"]) {
      const w = ((series_list && series_list.points) || []).map((p) => p[name]).filter(actual);
      result[name] = w.length >= 2 ? w : null;
    }
    return result;
  }

  /* ---------- Style of the CarPlay tiles ----------
   *
   * "klassisch": Swift draws as before. "a" and "b": the images come from
   * tiles.js and travel with the model. */
  const STYLE_KEY = "jolt-carplay-stil";
  const STYLES = ["klassisch", "a", "b"];

  function readStyle() {
    try {
      const s = window.localStorage.getItem(STYLE_KEY);
      return STYLES.includes(s) ? s : "klassisch";
    } catch (e) { return "klassisch"; }
  }

  let styleChoice = readStyle();
  let seriesListState = null;
  let lastModel = null;

  /* The model with style and tile images - what the plugin receives. The
   * images are not in the sender's model: the comparison "has something
   * changed" should not depend on pixels, and the Live Activity carries at
   * most 4 KB (the plugin leaves them out there). */
  function withImages(m, now) {
    seriesListState = seriesListAppend(seriesListState, m, now);
    const result = Object.assign({}, m, { look: styleChoice });
    if (styleChoice !== "klassisch" && window.joltTiles) {
      try {
        result.tileImages = window.joltTiles.pictures(styleChoice, m, seriesListExcerpt(seriesListState));
      } catch (failure) {
        // Without images Swift draws itself - a tile that fails must not
        // cost the display.
        console.log("[anzeige] Kacheln nicht gezeichnet:", failure && failure.message);
      }
    }
    return result;
  }

  /* The target in the iOS app: the Live Activity (plugins/jolt-display).
   *
   * In the browser and in Bluefy there is no such plugin; there the target
   * does nothing, and the sender reports nothing further. The plugin is only
   * looked up when sending, not when loading: ble-plugin.js comes before this
   * file, but what is not there at load time should not be missing later. */
  function nativeTarget(m) {
    const shell = window.joltBlePlugin;
    if (!shell || !shell.JoltDisplay || !shell.Capacitor
        || !shell.Capacitor.isNativePlatform()) return undefined;
    if (m === null) {
      lastModel = null;
      seriesListState = null;
      return shell.JoltDisplay.finish();
    }
    lastModel = m;
    return shell.JoltDisplay.refresh(
      { json: JSON.stringify(withImages(m, Date.now())) });
  }

  /* Choose the style - and resend the display right away, so the change
   * is visible in the car and not only after the next message. */
  function setStyle(look) {
    if (!STYLES.includes(look)) return false;
    styleChoice = look;
    try { window.localStorage.setItem(STYLE_KEY, look); } catch (e) { /* this session only */ }
    if (lastModel) {
      try { nativeTarget(lastModel); } catch (e) { /* no target */ }
    }
    return true;
  }

  /* The UI's sender: `live.js` reports every state here. */
  const defaultSender = sender({ destination: nativeTarget });

  return {
    model, sender, seriesListAppend, seriesListExcerpt, withImages,
    look: () => styleChoice, setStyle, STYLES,
    report: (z, extras) => defaultSender.report(z, extras),
    finish: () => defaultSender.finish(),
    setTarget: (f) => defaultSender.setTarget(f),
    MIN_SPACING_MS, HEARTBEAT_MS,
  };
})();
