/* The live view: actual versus plan, while driving.
 *
 * Two ways in: the phone's own location (GPS), or the simulator in the
 * server. At this stage the state of charge cannot yet be had from the
 * car - it comes from the simulation or is extrapolated.
 * Once the OBD2 logger connects, nothing about this view changes,
 * only the source of the measurement points.
 */
window.joltLive = (function () {
  "use strict";

  const K = window.jolt;
  let socket = null;       // WebSocket
  let plan = null;            // the currently valid charging plan
  let awake = null;           // watchPosition id, or "nativ" (native)
  let nativeAwakeId = null;   // id of the app's background location watcher
  let nativeRun = 0;         // counts starts, so a late callback knows whether it still applies
  let locationErrorReported = false;
  let latestReport = 0;      // time of the last position report
  let dongle = false;         // is the OBD2 dongle reading along?
  let readRound = 0;
  // The driven track of a recording, [[lon, lat], ...].
  let track = [];
  // The distance covered along this track. It is carried **continuously**
  // and pinned to each history point, instead of being recomputed from the
  // track at draw time: track and history grow under different conditions
  // (the track with every new position, the history with every new state of
  // charge), so `spur[i]` does not belong to `verlauf[i]`. When recording
  // with a dongle the difference is huge - the state of charge changes every
  // few minutes, the position every second.
  let drivenKm = 0;
  // The measured history: [{km, soc, reported}, ...] for the curve.
  let history = [];
  // The values most recently read from the car. The server does not send
  // them back - it only stores them -, so the display keeps them itself.
  let latestRawValues = null;
  let latestRawValuesTime = 0;   // when the last complete set arrived
  /* The last known value per measured quantity, with its timestamp.
   *
   * The table only showed what arrived in **this** round - and so became
   * patchy: the DC/DC current is only read every tenth round and was empty
   * nine rounds out of ten, and a value that dropped out once
   * vanished together with its row.
   *
   * An old value is almost always more useful than none at all. The
   * odometer reading from thirty seconds ago is still right; the interior
   * temperature from two minutes ago too. What is missing is not the value
   * but the indication of how old it is - and that now sits next to it. */
  let valuesAsOf = {};          // name -> {value, time}
  // Start of the trip for the running consumption: {soc, km} from the first
  // round in which both were available at the same time.
  let consumptionStart = null;
  // Whether a warning about the silence has already been given. Once is
  // enough: a message that comes every twelve seconds gets switched off.
  let quietReported = false;
  /* Measurement points for the consumption plot: [{time, kw, km, soc}, ...].
   *
   * Collected raw and only combined into sections at draw time - this way
   * the section width can be changed without losing the measurement. */
  let consumption_track = [];
  let unanswered = new Set();  // ids this car does not answer
  /* The power of the auxiliary consumers, when the control unit does not
   * report it.
   *
   * It exists as a ready-made number (DID 0364, "HV auxiliary consumer
   * power"), and that beats any calculation. If this control unit does not
   * answer, the approximation remains: while driving, the pack power
   * contains propulsion **and** auxiliary consumers, and modelling the
   * propulsion would need the gradient, which nobody knows during a
   * recording. But if the car is standing and not charging, the pack power
   * is that of the auxiliary consumers.
   *
   * Because this fallback only holds as long as nothing changes about the
   * heating, it is shown with its age - unlike the measured value,
   * which is always from right now. */
  let aux_load = null;   // {kw, time}

  /* How often the position is reported.
   *
   * This used to be thirty seconds, on the grounds that the tracking
   * averages over kilometres anyway. That holds for the **tracking**; for
   * the **recording** it does not, and the recording had not been built
   * back then. There every measurement point is a support point of the
   * route that is later built from them - at country-road speed there were
   * four hundred metres between them, and the straight line cuts off every
   * bend. The first real test drive showed exactly that.
   *
   * Twelve seconds is roughly a hundred and sixty metres and brings the
   * bends back, without battery and mobile data costing noticeably more:
   * a report is a small JSON, and the GPS is running anyway.
   *
   * For the length of the route the vehicle's odometer is the better
   * source (see `live/recording.odometer_factor`) - denser points are
   * still needed, because they carry the **history**: elevation profile,
   * speed per segment, and the map. */
  const REPORT_INTERVAL_MS = 12000;

  function showConnection(text, colour) {
    const el = document.getElementById("live-connection");
    if (!el) return;
    el.textContent = text;
    el.style.color = colour || "";
  }

  async function launch() {
    const trip = K.state.trip;
    if (!trip) { K.report("Erst eine Route rechnen.", "fehler"); return; }
    // First the dongle, then the session - see dongleOffer(). Whoever
    // picks none drives without: the trip starts in any case.
    const withDongle = await dongleOffer();
    try {
      // With the same filters as in the planning view: a charging plan that
      // suddenly allows other chargers on the road than when planning would
      // no longer be comprehensible.
      // The effort per stop goes along too: a plan that is suddenly
      // re-planned on the road by a different yardstick than when setting
      // off would no longer be comprehensible.
      const holding_cost = document.getElementById("holding-cost");
      const response = await K.api(`/api/live/start/${trip.trip_id}`
        + `?min_kw=${document.getElementById("min-kw").value}`
        + `&radius_km=${document.getElementById("radius").value}`
        + (holding_cost ? `&stop_fixed_cost_min=${holding_cost.value}` : "")
        + (function () {
            const p = document.getElementById("charge-park");
            return p ? `&charge_park_bonus_min=${p.value}` : "";
          })(),
        { method: "POST" });
      K.state.sessionId = response.session_id;
      K.sessionRemember(response.session_id);
      // Whoever starts the trip is sitting in the car: reading is allowed
      // until the phone says the car is standing.
      drivingStateStart("faehrt");
      document.getElementById("live-empty").hidden = true;
      document.getElementById("live-content").hidden = false;
      plan = response.plan || null;
      drawPlan();
      // At departure the starting state of charge is the best known value -
      // better at any rate than a fixed number that has nothing to do with this car.
      socFieldPrefill(trip.start_soc);
      link(response.session_id);
      positionTrace();
      window.joltApp.showView("live");
      // Ask once at the start, where the question means something - and not
      // at the first changed plan, where it gets in the way.
      notificationsSetUp();
      showDongle();
      K.report(withDongle
        ? "Live-Fahrt läuft – Ladestand und Zähler kommen aus dem Auto."
        : "Live-Fahrt läuft ohne Dongle – der Ladestand kommt von Hand. "
          + "Über „Dongle verbinden“ geht es jederzeit nachträglich.",
        "hinweis");
    } catch (failure) {
      K.report("Live: " + failure.message, "fehler");
    }
  }

  /* Re-establish the connection after a drop.
   *
   * A WebSocket does not survive a tunnel or a switch from Wi-Fi to
   * mobile data. Without reconnecting, the live view stayed frozen for the
   * rest of the trip: the measurement points still went out (the POST is a
   * separate path), but nothing came back - so no deviation, no arrival
   * forecast and above all no message about a changed plan. Exactly the
   * situation in which you need them.
   *
   * Growing intervals as with the dongle: a tunnel lasts seconds, a
   * dead spot in the countryside minutes. When the trip ends, it stops -
   * `K.zustand.sessionId` is the condition, and `beenden()` clears it. */
  let reconnectClock = null;

  function reconnectAgain(sessionId, attempt) {
    if (reconnectClock) clearTimeout(reconnectClock);
    if (attempt > 8 || K.state.sessionId !== sessionId) return;
    const wait = Math.min(30000, 2000 * Math.pow(2, attempt - 1));
    showConnection(`getrennt – neuer Versuch in ${wait / 1000} s`,
                       "#e8804f");
    reconnectClock = setTimeout(() => {
      reconnectClock = null;
      if (K.state.sessionId === sessionId) link(sessionId, attempt);
    }, wait);
  }

  function link(sessionId, attempt = 1) {
    if (socket) {
      // Detach the old listener before closing: otherwise this very close
      // triggers a reconnect itself.
      try { socket.onclose = null; socket.close(); } catch (e) {}
    }
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/api/live/${sessionId}/ws`);

    // A browser cannot set headers on a WebSocket; the token therefore goes
    // as the first message. Only the reply "bereit" ("ready") means the
    // server has accepted it - before that the connection does not count as
    // established, and the wait time stays as it is.
    socket.onopen = () => {
      try { socket.send(JSON.stringify({ token: K.token() })); }
      catch (e) { /* onclose reconnects */ }
    };
    socket.onclose = () => {
      if (K.state.sessionId === sessionId) reconnectAgain(sessionId, attempt + 1);
      else showConnection("getrennt", "#8a97a5");
    };
    socket.onerror = () => showConnection("gestört", "#e2596a");
    socket.onmessage = (msg) => {
      let message;
      try { message = JSON.parse(msg.data); } catch (e) { return; }
      if (message.kind === "bereit") {
        showConnection("verbunden", "#57c98a");
        attempt = 0;   // an established connection resets the wait time
        return;
      }
      if (message.kind === "ende") {
        showConnection("Fahrt beendet", "#8a97a5");
        return;
      }
      showState(message);
    };
  }

  function showState(z) {
    // The display model for everything outside this UI (CarPlay,
    // widget): a few numbers, throttled. An error there must never drag
    // the display here down with it.
    try {
      if (window.joltDisplay) {
        window.joltDisplay.report(z, { track: consumption_track, vals: valuesAsOf,
                                       aux: aux_load, plan: z.plan || plan });
      }
    }
    catch (e) { console.log("[anzeige]", e && e.message); }
    const trip = K.state.trip;
    const reserve = trip ? trip.vehicle.reserve_soc : 10;

    // A newly computed plan arrives with the state. Only if it really
    // differs is it pointed out - a plan that announces itself
    // every thirty seconds is no plan.
    if (z.plan) {
      plan = z.plan;
      drawPlan();
      if (z.plan_changed) reportChange(z.change);
    }

    // The last known state of charge as a suggestion for the next report: at
    // a charge point the new value is higher, on the road lower - in both
    // cases the last value is a shorter way than a fixed number.
    socFieldPrefill(z.actual_soc);

    const deviationVariety = z.deviation_pp === null ? ""
      : (z.deviation_pp <= -5 ? "schlecht"
        : (z.deviation_pp <= -2 ? "warnung" : "gut"));
    const forecastVariety = z.forecast_soc_at_target === null ? ""
      : (z.forecast_soc_at_target < reserve ? "schlecht"
        : (z.forecast_soc_at_target < reserve + 10 ? "warnung" : "gut"));

    /* The answer first, and the answer is not the state of charge.
     *
     * The state of charge is an input - the question in the car is "will
     * it be enough?", and the arrival value answers it. As long as a
     * charging plan exists, the next stop is the nearer and therefore more
     * urgent answer; without a plan the destination counts. */
    showResponse(z, reserve);

    /* Below it only what changes a decision. State of charge and
     * deviation deliberately sit here and not above: they are evidence, not
     * the answer - you read them when you want to follow up the big number. */
    document.getElementById("live-values").innerHTML = [
      // One decimal place: the dongle delivers the state of charge in steps
      // of 0.4 percentage points (one byte divided by 2.5). Rounded to whole
      // percent the number stands still for minutes although it is moving -
      // and the movement is exactly what you want to see.
      K.valueTile(z.soc_source === "zuletzt" ? "Ladestand (zuletzt gemessen)"
                   : (z.soc_reported === false ? "Ladestand (gerechnet)" : "Ladestand"),
        K.num(z.actual_soc, 1) + " %"),
      K.valueTile("Abweichung",
        (z.deviation_pp === null ? "–"
          : (z.deviation_pp > 0 ? "+" : "") + K.num(z.deviation_pp, 1) + " pp"),
        deviationVariety),
      // The arrival time is the second quantity that shifts on the road -
      // and the only one that a traffic jam moves without touching
      // the consumption.
      K.valueTile("Ankunft",
        (z.arrival_shift_min === null ? "–"
          : (Math.abs(z.arrival_shift_min) < 1 ? "nach Plan"
            : (z.arrival_shift_min > 0 ? "+" : "–")
              + K.duration(Math.abs(z.arrival_shift_min)))),
        (z.arrival_shift_min || 0) >= 10 ? "warnung" : ""),
      K.valueTile("Noch", K.num(z.remaining_km) + " km"),
    ].join("");

    /* Every measurement point arrives here **twice**: once as the reply to
     * our own POST, once via the WebSocket, which mirrors it back to all
     * viewers - and our own browser is one of them. Without this
     * check every point would appear twice in the history and in the track;
     * over a long distance that would be a thousand entries too many. */
    // Position and distance **before** the history: the history point should
    // know how far had been driven when it came into being.
    const place = measurement_site(z);
    if (place) {
      const most_recent = track[track.length - 1];
      if (!most_recent || most_recent[0] !== place[0] || most_recent[1] !== place[1]) {
        if (most_recent) drivenKm += spacingKm(most_recent, place);
        track.push(place);
      }
    }

    // Give the last consumption point the GPS distance that is now known.
    // It came into being when the dongle was read, i.e. before the
    // position was through.
    const lastV = consumption_track[consumption_track.length - 1];
    if (lastV && lastV.gps === null) lastV.gps = drivenKm;

    const previous = history[history.length - 1];
    // The driven distance counts in the comparison too: in a
    // recording `km_on_route` is null for every point (there is no
    // route yet), and without this part every point with an unchanged
    // state of charge counted as a duplicate. When recording with a
    // dongle that is almost all of them - the state of charge changes
    // every few minutes.
    const actualNew = z.actual_soc !== null && z.actual_soc !== undefined
      && !(previous && previous.km === (z.km_on_route || 0)
           && previous.driven_km === drivenKm
           && previous.soc === z.actual_soc);
    if (actualNew) {
      history.push({ km: z.km_on_route || 0, driven_km: drivenKm,
                     soc: z.actual_soc, reported: z.soc_reported !== false });
    }
    drawHistory();
    drawConsumption();
    autoRow(z);
    showDongle();

    const bar = document.getElementById("live-bars");
    bar.style.width = Math.max(0, Math.min(100, z.actual_soc)) + "%";
    bar.style.background = z.actual_soc <= reserve ? "#e2596a"
      : (z.actual_soc <= reserve + 10 ? "#e8804f" : "#57c98a");

    const hint = document.getElementById("live-hint");
    hint.textContent = z.reason || "im Plan";
    hint.style.color = z.replanning_required ? "#e8804f" : "";

    // The reserve marker moves along: that is the actual message of the
    // live function - not "you are using more", but "it is now only enough
    // as far as there".
    /* Serve the map **without** a planned route too.
     *
     * This used to be `if (fahrt && ...)`, and `K.zustand.fahrt` is only set
     * if a route was computed beforehand. A recording has
     * none - so the whole block was skipped and the map stayed
     * empty although the position had long been coming in. The own position
     * has nothing to do with whether a route exists.
     */
    if (window.joltMap) {
      const here = place || [z_lon(z), z_lat(z)];
      const marker = [{ lat: here[1], lon: here[0], kind: "auto", text: "hier" }];
      // In a recording the driven track is what there is to see:
      // it grows along and shows that data is really being written.
      // It is filled further up, together with the distance.
      if (!trip) {
        window.joltMap.setRoute(track);
        // The map follows the track by itself (map.js) until someone
        // touches it - then it stays where it is. Earlier only the first
        // point was centred, and the growing route had to be followed
        // by hand.
      }
      if (trip && z.reserve_at_km !== null && trip.profile) {
        const hit = trip.profile.find((p) => p.km >= z.reserve_at_km);
        if (hit) {
          marker.push({ lat: hit.lat, lon: hit.lon, kind: "reserve",
                        text: "Reserve " + K.num(z.reserve_at_km) + " km" });
        }
      }
      for (const stop of (plan && plan.stops) || []) {
        marker.push({ lat: stop.lat, lon: stop.lon, kind: "stopp",
                      text: K.duration(stop.charge_time_minutes) });
      }
      window.joltMap.setMarker(marker);
    }
  }


  /* ---------- The answer ---------- */

  function showResponse(z, reserve) {
    const box = document.getElementById("live-response");
    const numberEl = document.getElementById("live-response-number");
    const text = document.getElementById("live-response-text");
    if (!box) return;

    const stop = z.next_stop;
    let expectedSoc = null, whereText = "", level = "";
    if (stop && stop.expected_soc !== null && stop.expected_soc !== undefined) {
      expectedSoc = stop.expected_soc;
      whereText = `an ${stop.name || "nächster Stopp"} · km ${K.num(stop.km_on_route)}`;
    } else if (z.forecast_soc_at_target !== null) {
      expectedSoc = z.forecast_soc_at_target;
      whereText = "am Ziel, ohne Nachladen";
    }

    if (expectedSoc === null) {
      numberEl.textContent = K.num(z.actual_soc) + " %";
      text.textContent = "Ladestand – noch keine Prognose";
      box.className = "";
      return;
    }
    // A negative value is not a statement about the battery but about the
    // fact that it will not be enough. That is exactly what belongs there.
    if (expectedSoc < 0) {
      numberEl.textContent = "reicht nicht";
      level = "schlecht";
    } else {
      numberEl.textContent = K.num(expectedSoc) + " %";
      level = expectedSoc < reserve ? "schlecht" : (expectedSoc < reserve + 8 ? "warnung" : "gut");
    }
    text.textContent = whereText;
    box.className = level;
  }

  /* Straight-line distance between two [lon, lat] in kilometres. For a driven
   * track with points every thirty seconds the difference from the
   * road length is negligible - and for the axis of a curve all that
   * matters is that it grows monotonically. */
  function spacingKm(a, b) {
    if (!a || !b) return 0;
    const R = 6371, r = Math.PI / 180;
    const dLat = (b[1] - a[1]) * r, dLon = (b[0] - a[0]) * r;
    const h = Math.sin(dLat / 2) ** 2
      + Math.cos(a[1] * r) * Math.cos(b[1] * r) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  /* ---------- The history ---------- */

  /* Plan and actual over the distance, in one picture.
   *
   * This is jolt's thesis as a drawing: a plan computed at departure
   * is wrong after eighty kilometres - and two curves that
   * diverge say so at a glance, whereas a tile showing
   * "-6 pp" first has to be read and put in context. Above all the
   * curve says whether things are getting better or worse; a snapshot
   * fundamentally cannot.
   *
   * It is drawn without a plan too: in a recording there is no
   * planned curve, but the measured one is then all the more what you
   * want to see.
   */
  function drawHistory() {
    const canvas = document.getElementById("live-history");
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const trip = K.state.trip;
    const profile = (trip && trip.profile) || [];
    const reserve = trip ? trip.vehicle.reserve_soc : 10;

    // The scale depends on what there is: with a plan on the whole
    // route, without a plan on what has been driven so far.
    /* Without a plan there is no `km_on_route` - it comes from the projection
     * onto the route, and a recording has none. It is therefore null
     * for **every** point, and the curve collapsed into a vertical
     * line at the left edge. Of all places exactly where it is the
     * only thing there is to see.
     *
     * Measuring then happens along the driven track: for point i the
     * sum of the distances up to there. That is the distance really
     * covered, and therefore the right axis. */
    /* There used to be a loop here that recomputed the distance at draw time
     * from the track - `spacingKm(spur[i-1], spur[i])` for every history
     * point i. That presupposed that `spur[i]` belongs to `verlauf[i]`,
     * and it does not: the track grows with every new position, the
     * history with every new state of charge. When recording with a dongle
     * the history was many times shorter, and the curve moved
     * to the left edge as a result - exactly the error this loop
     * was meant to fix. Now each history point carries its own distance. */
    const ownKm = !profile.length;
    const distanceFrom = (v) => ownKm ? (v.driven_km || 0) : v.km;
    const maxKm = profile.length
      ? (profile[profile.length - 1].km || 1)
      : Math.max(1, ...history.map(distanceFrom));
    const left = 4, right = width - 4, upper = 8, bottom = height - 16;
    const x = (km) => left + (km / maxKm) * (right - left);
    const y = (soc) => bottom - (Math.max(0, Math.min(100, soc)) / 100)
      * (bottom - upper);

    // Elevation profile in the background. It explains the kinks in both curves -
    // without this explanation they look like measurement errors.
    if (profile.length > 1) {
      let maxElevation = 1;
      for (const p of profile) maxElevation = Math.max(maxElevation, p.elevation || 0);
      ctx.beginPath();
      ctx.moveTo(x(0), bottom);
      for (const p of profile) {
        ctx.lineTo(x(p.km), bottom - ((p.elevation || 0) / maxElevation) * (bottom - upper) * 0.3);
      }
      ctx.lineTo(x(maxKm), bottom);
      ctx.closePath();
      ctx.fillStyle = "rgba(138,151,165,.12)";
      ctx.fill();
    }

    // The reserve as a line, not a number: you see at once where the
    // measured curve is heading towards it.
    ctx.beginPath();
    ctx.setLineDash([4, 4]);
    ctx.moveTo(left, y(reserve));
    ctx.lineTo(right, y(reserve));
    ctx.strokeStyle = "rgba(226,89,106,.6)";
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(226,89,106,.75)";
    ctx.font = "10px system-ui, sans-serif";
    ctx.fillText("Reserve", left + 2, y(reserve) - 3);

    // Planned curve: muted, it is the reference and not the message.
    if (profile.length > 1) {
      ctx.beginPath();
      profile.forEach((p, i) => {
        const px = x(p.km), py = y(p.soc);
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      });
      ctx.strokeStyle = "rgba(138,151,165,.55)";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }

    // The charging stops as markers - they explain the jumps that are
    // about to come, and show how far away the next one still is.
    for (const stop of (plan && plan.stops) || []) {
      const px = x(stop.km_on_route);
      ctx.beginPath();
      ctx.moveTo(px, upper);
      ctx.lineTo(px, bottom);
      ctx.strokeStyle = "rgba(255,201,60,.35)";
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    // The measured curve. It is the message, so make it strong.
    if (history.length > 1) {
      ctx.beginPath();
      history.forEach((v, i) => {
        const px = x(distanceFrom(v)), py = y(v.soc);
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      });
      ctx.strokeStyle = "#ffc93c";
      ctx.lineWidth = 2.5;
      ctx.lineJoin = "round";
      ctx.stroke();
    }

    // Where the car is right now. A computed state of charge gets a
    // hollow dot - you should be able to see that it is not measured.
    const latest = history[history.length - 1];
    if (latest) {
      ctx.beginPath();
      ctx.arc(x(distanceFrom(latest)), y(latest.soc), 4.5, 0, Math.PI * 2);
      if (latest.reported) { ctx.fillStyle = "#ffc93c"; ctx.fill(); }
      else { ctx.strokeStyle = "#ffc93c"; ctx.lineWidth = 2; ctx.stroke(); }
    }

    ctx.fillStyle = "rgba(138,151,165,.8)";
    ctx.fillText("0", left, height - 4);
    const label = K.num(maxKm) + " km";
    ctx.fillText(label, right - ctx.measureText(label).width,
                   height - 4);
  }

  /* ---------- The charging plan on the road ---------- */

  function drawPlan() {
    const listEl = document.getElementById("live-plan");
    const as_of = document.getElementById("live-plan-status");
    if (!listEl || !as_of) return;

    if (!plan) {
      listEl.innerHTML = '<li class="leer">Noch kein Ladeplan.</li>';
      as_of.textContent = "";
      return;
    }
    as_of.textContent = plan.reading_km ? "gerechnet ab km " + K.num(plan.reading_km)
                                      : "beim Losfahren gerechnet";

    if (!plan.feasible) {
      listEl.innerHTML = `<li class="leer" style="color:#e2596a">${
        sanitize(plan.reason || "Kein Ladeplan möglich.")}</li>`;
      return;
    }
    if (!plan.stops || !plan.stops.length) {
      listEl.innerHTML = '<li class="leer">Kein Ladestopp mehr nötig.</li>';
      return;
    }

    listEl.innerHTML = "";
    plan.stops.forEach((s, i) => {
      const entry = document.createElement("li");
      entry.innerHTML = `
        <div class="haupt">
          <div class="titel">${i + 1}. ${sanitize(s.name || s.operator
            || "Ladepunkt")}</div>
          <div class="unter">km ${K.num(s.km_on_route)} ·
            ${K.num(s.arrival_soc)} % → ${K.num(s.departure_soc)} % ·
            ${K.num(s.max_kw)} kW · ${s.point_count} Ladepunkte</div>
        </div>
        <div class="kw">${K.duration(s.charge_time_minutes)}</div>`;
      listEl.appendChild(entry);
    });
  }

  /* A change to the plan is the only reason to disturb someone at the
   * wheel - hence a notification here and nowhere else. */
  function reportChange(text) {
    const box = document.getElementById("live-change");
    if (box) {
      box.textContent = text || "Der Ladeplan hat sich geändert.";
      box.hidden = false;
    }
    notify(text || "Der Ladeplan hat sich geändert.");
  }

  function notify(text) {
    // Without granted permission we neither ask nor notify:
    // whoever has the view open sees the message anyway. We ask
    // once at the start of the trip, where the question also means something.
    try {
      if (!("Notification" in window) || Notification.permission !== "granted") {
        return;
      }
      new Notification("jolt – Ladeplan geändert", { body: text, day: "jolt-plan" });
    } catch (e) { /* depending on browser and context not allowed - then so be it */ }
  }

  /* ---------- Notifications to the phone ---------- */

  /* Ask once at the start of the trip and register the device.
   *
   * The route via the push service is the only one that reaches a phone with
   * a dark screen: the WebSocket connection goes to sleep with it. Hence
   * a real subscription here and not just the permission for the Notification API.
   *
   * If any step fails, the trip runs anyway - then just with
   * the message in the open view. */
  async function notificationsSetUp() {
    try {
      if (!("Notification" in window) || !("PushManager" in window)) return;

      const key = await K.api("/api/push/key");
      if (!key.configured) return;   // no VAPID key on the server

      if (Notification.permission === "default") {
        await Notification.requestPermission();
      }
      if (Notification.permission !== "granted") return;

      const registration = K.state.serviceWorker
        || (navigator.serviceWorker && await navigator.serviceWorker.ready);
      if (!registration || !registration.pushManager) return;

      // Reuse an existing subscription. Creating a new one would return
      // the same endpoint, but costs a detour.
      let subscription = await registration.pushManager.getSubscription();
      if (!subscription) {
        subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: keyAsBytes(key.public_key),
        });
      }

      const subscriptionJson = subscription.toJSON();
      await K.api("/api/push/subscription", { method: "POST", body: {
        endpoint: subscriptionJson.endpoint,
        p256dh: subscriptionJson.keys.p256dh,
        auth: subscriptionJson.keys.auth,
        device: navigator.userAgent.slice(0, 120),
      }});
    } catch (failure) {
      // Deliberately only to the log: whoever is just setting off does not want to read an error
      // message about a side function.
      if (window.console) console.warn("Benachrichtigungen:", failure.message);
    }
  }

  /* The public key arrives as base64url and must be passed as a
   * Uint8Array - the browser does not accept the string. */
  function keyAsBytes(text) {
    const filled = (text + "=".repeat((4 - text.length % 4) % 4))
      .replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(filled);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return bytes;
  }

  /* The names come from third-party data sources and end up in innerHTML. */
  function sanitize(text) {
    const helper = document.createElement("div");
    helper.textContent = text || "";
    return helper.innerHTML;
  }

  /* Where the measurement point was - [lon, lat], or null.
   *
   * The server sends the coordinate along. Before, it did not, and the
   * view computed it back from the *planned* profile (`z_lat`/`z_lon`
   * below). In a recording there is no such profile - it only
   * comes into being on completion -, and the back-computation silently
   * returned (0, 0): map in the Gulf of Guinea, track of a single point.
   *
   * The fallback remains for sessions that are still served by an older
   * version - there it is correct, because then there is a route. */
  function measurement_site(z) {
    if (typeof z.lat === "number" && typeof z.lon === "number"
        && (z.lat !== 0 || z.lon !== 0)) {
      return [z.lon, z.lat];
    }
    const lat = z_lat(z), lon = z_lon(z);
    return (lat === 0 && lon === 0) ? null : [lon, lat];
  }

  /* The fallback: via the profile, every odometer reading has a
   * position attached. Only applies to planned trips. */
  function z_lat(z) { return pointAtKm(z.km_on_route).lat; }
  function z_lon(z) { return pointAtKm(z.km_on_route).lon; }

  function pointAtKm(km) {
    const profile = (K.state.trip || {}).profile || [];
    return profile.find((p) => p.km >= km) || profile[profile.length - 1]
      || { lat: 0, lon: 0 };
  }

  async function simulate() {
    if (!K.state.sessionId) { K.report("Keine Live-Fahrt.", "fehler"); return; }
    const more = Number(document.getElementById("extra-consumption").value) / 100;
    const jam = Number(document.getElementById("jam").value) / 100;
    try {
      await K.api(`/api/live/${K.state.sessionId}/simulate`
        + `?extra_consumption=${more}&tick_s=0.3&time_factor=${jam}`,
        { method: "POST" });
      K.report(`Simulation läuft mit ${Math.round(more * 100)} % Verbrauch `
        + `und ${Math.round(jam * 100)} % Fahrzeit.`, "hinweis");
    } catch (failure) {
      K.report("Simulation: " + failure.message, "fehler");
    }
  }

  /* ---------- State of charge by hand ---------- */

  /* ---------- Reporting the position continuously ---------- */

  /* Without these reports jolt gets absolutely nothing between two
   * typed-in states of charge - no position, no time. Then the time factor
   * stays at 1.0 for the whole trip, the arrival forecast at its state at departure,
   * and a detour is only noticed when someone types something in of their own accord.
   *
   * The state of charge is deliberately *not* sent along: it is unknown, and
   * re-sending the last known value would mean inventing a measurement -
   * the consumption factor would read from it that the car has used nothing
   * since. What holds between two reports, the server extrapolates from the
   * energy profile. */
  function locationInput(coords, timeMs) {
    // Before the throttling: the state machine wants to see every fix, not every
    // twelfth.
    examineDrivingState(coords, timeMs);
    const now = Date.now();
    // Do not report every GPS update: the device delivers every
    // second, and the tracking averages over kilometres anyway.
    // Sending more often costs battery and mobile data without saying anything.
    if (now - latestReport < REPORT_INTERVAL_MS) return;
    latestReport = now;
    reportPosition(coords, timeMs);
  }

  function positionTrace() {
    if (awake !== null) return;
    const native = nativeLocation();
    if (native) { nativeTrace(native); return; }
    webTrace();
  }

  function webTrace() {
    if (!navigator.geolocation) return;
    screenAwakeHold();
    awake = navigator.geolocation.watchPosition(
      (pos) => locationInput(pos.coords, pos.timestamp),
      // A GPS error on the road is no reason to bother the user -
      // in a tunnel it is the normal case, and the next measurement comes.
      () => {},
      { enableHighAccuracy: true, maximumAge: 15000, timeout: 30000 });
  }

  /* The location with a locked phone - only in the iOS app.
   *
   * `watchPosition` in the WebView delivers nothing any more once the screen
   * is locked: iOS freezes the page, and the reports with it. The
   * plugin, by contrast, runs via `CLLocationManager` with
   * `allowsBackgroundLocationUpdates`; as long as it delivers positions, iOS
   * keeps the app alive, and the reports - including dongle reading and
   * queue - carry on as in the foreground.
   *
   * What matters is `backgroundMessage`: if it is set, the watcher stays
   * active in the background too, otherwise only in the foreground. On iOS you then
   * see the blue location indicator in the status bar - intended.
   *
   * In the browser the plugin does not exist; there `watchPosition` applies.
   *
   * **The plugin must really be built in.** The app loads its
   * UI from the server at runtime; this code is therefore there immediately, the
   * native class only after a new app build. On an older
   * build `registerPlugin` returns a stand-in whose calls fail
   * with "not implemented" - and the trip would have no location at all
   * any more. `isPluginAvailable` checks, and otherwise the browser path applies. */
  function nativeLocation() {
    const h = window.joltBlePlugin;
    if (!h || !h.Capacitor || !h.Capacitor.isNativePlatform()
        || !h.BackgroundGeolocation) return null;
    if (typeof h.Capacitor.isPluginAvailable === "function"
        && !h.Capacitor.isPluginAvailable("BackgroundGeolocation")) return null;
    return h.BackgroundGeolocation;
  }

  async function nativeTrace(plugin) {
    // Claim it immediately: `addWatcher` only answers after the
    // permission prompt, and until then no second start may slip in.
    awake = "nativ";
    const cycle = ++nativeRun;
    screenAwakeHold();
    let id;
    try {
      id = await plugin.addWatcher({
        backgroundTitle: "jolt zeichnet die Fahrt auf",
        backgroundMessage: "Position und Ladestand werden weiter erfasst.",
        requestPermissions: true,
        distanceFilter: 0,
      }, (place, failure) => {
        if (failure) { locationError(failure); return; }
        if (!place || typeof place.latitude !== "number") return;
        locationInput({
          latitude: place.latitude, longitude: place.longitude,
          // The plugin delivers null instead of -1 when the speed is missing.
          speed: typeof place.speed === "number" ? place.speed : null,
          altitude: typeof place.altitude === "number" ? place.altitude : null,
        }, place.time);
      });
    } catch (failure) {
      // The plugin did not work - then at least the location in the foreground,
      // instead of none at all for the rest of the trip.
      if (cycle === nativeRun) { awake = null; webTrace(); }
      locationError(failure);
      return;
    }
    // Ended while iOS was still asking: remove the watcher just created
    // again straight away, otherwise it keeps running without a trip and costs battery.
    if (cycle !== nativeRun || awake !== "nativ") {
      dropWatcher(plugin, id);
      return;
    }
    nativeAwakeId = id;
  }

  function dropWatcher(plugin, id) {
    // A promise: a rejection is not caught by try/catch.
    try {
      Promise.resolve(plugin.removeWatcher({ id })).catch(() => {});
    } catch (e) { /* already gone */ }
  }

  function locationError(failure) {
    if (failure && failure.code === "NOT_AUTHORIZED") {
      // Say it once, not on every callback. Without permission there are no
      // measurement points with a locked phone - you need to know that before
      // setting off.
      if (locationErrorReported) return;
      locationErrorReported = true;
      K.report("Standort nicht erlaubt. In den iOS-Einstellungen für jolt "
        + "Standort auf „Beim Verwenden“ oder „Immer“ stellen - sonst "
        + "kommen bei gesperrtem Telefon keine Messpunkte an.", "warnung");
    }
    // Everything else is as in the browser: a tunnel, the next measurement comes.
  }

  function positionGiveUp() {
    if (awake === null) return;
    if (awake === "nativ") {
      const id = nativeAwakeId;
      nativeAwakeId = null;
      nativeRun++;
      const plugin = nativeLocation();
      if (id && plugin) {
        dropWatcher(plugin, id);
      }
    } else {
      try { navigator.geolocation.clearWatch(awake); } catch (e) {}
    }
    awake = null;
    locationErrorReported = false;
    screenRelease();
  }

  /* ---------- The screen must stay on ---------- */

  /* Without this iOS turns the screen off after a minute, and with the
   * screen the page sleeps: `watchPosition` delivers nothing any more, the
   * Bluetooth loop stands still, and on waking up the piece in between is missing.
   *
   * The diagnostics page under /obd had this from the start, this view
   * did not - and recording happens here. In the first real test drive
   * there is exactly for that reason a gap of eight minutes with a single
   * measurement point in it.
   *
   * The lock is lost as soon as the page goes to the background, and
   * does not come back by itself; hence it is acquired again on
   * returning. */
  let wake_lock = null;
  let keepAwakeVideo = null;

  /* On iOS the Wake Lock API remains unreliable in a page launched as an
   * app from the home screen ("standalone", see manifest.json) -
   * sometimes the lock is not granted at all, sometimes it drops after a short
   * time by itself without triggering a "release" event. A
   * silent, invisible video running in an endless loop on the other hand
   * keeps the screen awake reliably - the same technique NoSleep.js
   * uses. Both paths run in parallel, neither harms the other.
   *
   * An empty canvas as the source (`captureStream()`) was not enough: iOS
   * apparently did not consistently count that as real playback, the
   * screen went off anyway. An actual, if tiny,
   * video (1 second, 2x2 pixels, black, no sound - 1.5 kB) works more
   * reliably. Embedded instead of as a separate file, so nothing has to be
   * loaded from the network before the lock takes effect. */
  const KEEP_AWAKE_MP4 = "data:video/mp4;base64,AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAMXbW9vdgAAAGxtdmhkAAAA"
    + "AAAAAAAAAAAAAAAD6AAAA+gAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAB"
    + "AAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAkF0"
    + "cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAA+gAAAAAAAAAAAAAAAAAAAAA"
    + "AAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAIAAAACAAAAAAAkZWR0"
    + "cwAAABxlbHN0AAAAAAAAAAEAAAPoAAAAAAABAAAAAAG5bWRpYQAAACBtZGhkAAAAAAAA"
    + "AAAAAAAAAABAAAAAQABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABW"
    + "aWRlb0hhbmRsZXIAAAABZG1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAA"
    + "HGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAASRzdGJsAAAAwHN0c2QAAAAAAAAAAQAA"
    + "ALBhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAIAAgBIAAAASAAAAAAAAAABFUxh"
    + "dmM1OS4zNy4xMDAgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAANmF2Y0MBZAAK/+EAGWdk"
    + "AAqs2V+IiMBEAAADAAQAAAMACDxIllgBAAZo6+PLIsD9+PgAAAAAEHBhc3AAAAABAAAA"
    + "AQAAABRidHJ0AAAAAAAAFigAABYoAAAAGHN0dHMAAAAAAAAAAQAAAAEAAEAAAAAAHHN0"
    + "c2MAAAAAAAAAAQAAAAEAAAABAAAAAQAAABRzdHN6AAAAAAAAAsUAAAABAAAAFHN0Y28A"
    + "AAAAAAAAAQAAA0cAAABidWR0YQAAAFptZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGly"
    + "YXBwbAAAAAAAAAAAAAAAAC1pbHN0AAAAJal0b28AAAAdZGF0YQAAAAEAAAAATGF2ZjU5"
    + "LjI3LjEwMAAAAAhmcmVlAAACzW1kYXQAAAKtBgX//6ncRem95tlIt5Ys2CDZI+7veDI2"
    + "NCAtIGNvcmUgMTY0IHIzMDk1IGJhZWU0MDAgLSBILjI2NC9NUEVHLTQgQVZDIGNvZGVj"
    + "IC0gQ29weWxlZnQgMjAwMy0yMDIyIC0gaHR0cDovL3d3dy52aWRlb2xhbi5vcmcveDI2"
    + "NC5odG1sIC0gb3B0aW9uczogY2FiYWM9MSByZWY9MyBkZWJsb2NrPTE6MDowIGFuYWx5"
    + "c2U9MHgzOjB4MTEzIG1lPWhleCBzdWJtZT03IHBzeT0xIHBzeV9yZD0xLjAwOjAuMDAg"
    + "bWl4ZWRfcmVmPTEgbWVfcmFuZ2U9MTYgY2hyb21hX21lPTEgdHJlbGxpcz0xIDh4OGRj"
    + "dD0xIGNxbT0wIGRlYWR6b25lPTIxLDExIGZhc3RfcHNraXA9MSBjaHJvbWFfcXBfb2Zm"
    + "c2V0PS0yIHRocmVhZHM9MSBsb29rYWhlYWRfdGhyZWFkcz0xIHNsaWNlZF90aHJlYWRz"
    + "PTAgbnI9MCBkZWNpbWF0ZT0xIGludGVybGFjZWQ9MCBibHVyYXlfY29tcGF0PTAgY29u"
    + "c3RyYWluZWRfaW50cmE9MCBiZnJhbWVzPTMgYl9weXJhbWlkPTIgYl9hZGFwdD0xIGJf"
    + "Ymlhcz0wIGRpcmVjdD0xIHdlaWdodGI9MSBvcGVuX2dvcD0wIHdlaWdodHA9MiBrZXlp"
    + "bnQ9MjUwIGtleWludF9taW49MSBzY2VuZWN1dD00MCBpbnRyYV9yZWZyZXNoPTAgcmNf"
    + "bG9va2FoZWFkPTQwIHJjPWNyZiBtYnRyZWU9MSBjcmY9MjMuMCBxY29tcD0wLjYwIHFw"
    + "bWluPTAgcXBtYXg9NjkgcXBzdGVwPTQgaXBfcmF0aW89MS40MCBhcT0xOjEuMDAAgAAA"
    + "ABBliIQAFf/+98nvwKbr29+B";

  /* Positioned outside the visible area (negative coordinates)
   * it did not hold - presumably a video that is not in the
   * visible area at all does not count as real playback for iOS. Now
   * it actually sits in the top left corner, just a single, almost
   * transparent pixel in size - that goes unnoticed, but counts. */
  function fetchVideoKeepAwake() {
    if (keepAwakeVideo) return keepAwakeVideo;
    const video = document.createElement("video");
    video.muted = true;
    video.loop = true;
    video.playsInline = true;
    video.setAttribute("playsinline", "");
    video.setAttribute("webkit-playsinline", "");
    video.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;"
      + "opacity:0.01;pointer-events:none;z-index:-1;";
    video.src = KEEP_AWAKE_MP4;
    document.body.appendChild(video);
    keepAwakeVideo = video;
    return video;
  }

  /* Both paths report back, instead of only writing to the invisible console log -
   * at the wheel nobody can get at the console, and without a
   * visible response a failure could only be guessed at rather than seen. */
  /* In the iOS app this is one line, and it holds.
   *
   * `isIdleTimerDisabled` simply tells the system not to let the lock
   * timer run - no Wake Lock that gets revoked, no video that has to
   * count as playback. The path below stays in place anyway:
   * the UI also keeps running in the browser, and there is nothing
   * better there than Wake Lock and the video workaround. */
  async function nativeAwakeHold() {
    const h = window.joltBlePlugin;
    if (!h || !h.Capacitor || !h.Capacitor.isNativePlatform()) return false;
    try {
      await h.KeepAwake.keepAwake();
      return true;
    } catch (failure) {
      console.log("[live] KeepAwake ging nicht:", failure.message);
      return false;
    }
  }

  async function screenAwakeHold() {
    if (await nativeAwakeHold()) return;
    let hint = "";
    if (!wake_lock && "wakeLock" in navigator) {
      try {
        wake_lock = await navigator.wakeLock.request("screen");
        wake_lock.addEventListener("release", () => { wake_lock = null; });
      } catch (failure) {
        hint = "Wake Lock: " + failure.message;
      }
    } else if (!("wakeLock" in navigator)) {
      hint = "Wake Lock: vom Browser nicht unterstützt";
    }
    try {
      await fetchVideoKeepAwake().play();
    } catch (failure) {
      hint += (hint ? " / " : "") + "Video: " + failure.message;
    }
    if (hint) {
      console.log("[live] Bildschirm wachhalten:", hint);
      K.report("Bildschirm bleibt evtl. nicht an (" + hint + ") - "
        + "sicherheitshalber Automatische Sperre in den iOS-Einstellungen "
        + "auf „Nie” stellen.", "warnung");
    }
  }

  function screenRelease() {
    // After the trip the phone should lock normally again. The
    // native path is withdrawn first; the two below do no
    // harm if they never took hold in the first place.
    const h = window.joltBlePlugin;
    if (h && h.Capacitor && h.Capacitor.isNativePlatform()) {
      h.KeepAwake.allowSleep().catch(() => {});
    }
    if (wake_lock) {
      try { wake_lock.release(); } catch (e) {}
      wake_lock = null;
    }
    if (keepAwakeVideo) {
      try { keepAwakeVideo.pause(); } catch (e) {}
    }
  }

  /* Back in the foreground: catch up on what was left lying in the background.
   *
   * iOS freezes a page in the background. The drop of the
   * Bluetooth connection is then reported, but the reconnect
   * hangs on a timer, and that only continues once the page is
   * visible again. Without this follow-up the dongle would stay disconnected
   * until the next drop comes - and it never does. */
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible" || !K.state.sessionId) return;
    screenAwakeHold();
    // `donglePause` belongs in both conditions: at the charger the
    // car is locked, and a connection that jolt brings back on its own
    // here sets off the alarm. Today `dongle` already covers the case
    // - but this condition must not depend on a
    // second variable having been set correctly elsewhere.
    reconnectDongle();
    // Report a point immediately instead of waiting for the next beat:
    // after a pause in the background the first point afterwards is
    // the important one - it closes the gap.
    latestReport = 0;
  });

  /* When a dongle reads along, the state of charge travels from here.
   *
   * Deliberately attached to the same report and not as a second loop: this way
   * position and state of charge belong to **one** measurement point and the same
   * second. Two loops would produce points that alternate - one with
   * position, one with state of charge -, and the tracking would have to put
   * both back together. */
  /* Is the dongle paused right now?
   *
   * At the charger the car is locked, and a locked car that
   * keeps being queried over CAN sets off the alarm. A mere
   * "stop reading" is not enough here - the reconnect would bring the
   * connection back by itself. Pause therefore means: disconnect and
   * do not reconnect until someone says so. */
  let donglePause = false;

  /* ---------- When may the dongle query the car? ---------- */

  /* Whether the car is locked cannot be found out **without** asking
   * it - and the asking itself sets off the alarm of a locked car.
   * There is no signal that the dongle or the car sends out on its own.
   * So it is worked out the other way round: reading happens
   * only if the phone knows something that cannot be the case with a locked
   * car - it is moving at driving speed.
   *
   *   fährt    from 15 km/h (two measurements in a row). On foot you
   *            do not get there, and whoever is that fast is in a car.
   *            Reading happens, and if the dongle is gone it is fetched back.
   *   steht    under 3 km/h for ten seconds. Nothing is asked any more;
   *            the connection stays, an idling dongle sends nothing onto
   *            the bus. Traffic lights, jams and charging pumps thus cost no rebuild.
   *   geparkt  the phone is on foot more than 25 m from the stopping place, or
   *            the car has been standing for three minutes: disconnect, do not
   *            reconnect. Only a drive brings it back.
   *
   * The counters in the car run over the lifetime: what was used during a pause
   * is contained in the difference of the next measurement. A gap while
   * standing therefore costs no consumption, only individual values.
   *
   * Not detectable: whoever locks before ten seconds have passed
   * can still trigger a query in the first moment. Only
   * a signal from the car itself would be certain. */
  const FAST_KMH = 15;
  const STAND_KMH = 3;
  const STANDING_TIME_MS = 10000;
  const PARK_TIME_MS = 180000;
  const PATH_M = 25;
  // If someone tapped "Dongle verbinden" ("connect dongle") by hand, they want to read while standing.
  const MANUAL_MS = 600000;
  // No dongle in range (bicycle, bus): after this many failed attempts
  // it stops until the vehicle has stopped again.
  const AGAIN_ATTEMPTS_MAX = 8;
  const AGAIN_ATTEMPTS_DRIVING = 40;

  let autoMode = true;
  try { autoMode = localStorage.getItem("jolt-dongle-auto") !== "0"; }
  catch (e) { /* without storage the default applies */ }

  let driveState = "steht";     // "faehrt" | "steht" | "geparkt"
  let standSince = null;
  let standCity = null;
  let fastSequence = 0;
  let asOfSeen = true;
  let manualUntil = 0;
  let latestPosition = null;
  let latestSpeed = null;

  function readAllowed() {
    if (donglePause) return false;
    // While listening in (settings) nothing may be queried.
    if (window.joltObd && window.joltObd.listens && window.joltObd.listens()) return false;
    if (!autoMode) return true;
    return driveState === "faehrt" || Date.now() < manualUntil;
  }

  function drivingStateStart(state) {
    driveState = state;
    standSince = null; standCity = null; fastSequence = 0;
    asOfSeen = true; manualUntil = 0; latestPosition = null;
    voltageBasis = []; voltageSequence = 0;
  }

  function distanceM(a, b) {
    const R = 6371000, rad = Math.PI / 180;
    const dLat = (b.lat - a.lat) * rad, dLon = (b.lon - a.lon) * rad;
    const h = Math.sin(dLat / 2) ** 2
      + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(h));
  }

  /* The speed from the fix - or from two fixes if the device
   * provides none (regularly the case in the browser on iOS). */
  function speedKmh(coords, place, timeMs) {
    const prior = latestPosition;
    latestPosition = { lat: place.lat, lon: place.lon, timestamp: timeMs };
    if (typeof coords.speed === "number" && coords.speed >= 0) {
      return coords.speed * 3.6;
    }
    if (!prior) return null;
    const dt = (timeMs - prior.timestamp) / 1000;
    if (dt < 1 || dt > 30) return null;
    return distanceM(prior, place) / dt * 3.6;
  }

  function examineDrivingState(coords, timeMs) {
    if (!autoMode || !K.state.sessionId) return;
    const now = Date.now();
    const place = { lat: coords.latitude, lon: coords.longitude };
    const v = speedKmh(coords, place, timeMs || now);
    if (v === null) return;
    latestSpeed = v;

    if (v >= FAST_KMH) {
      standSince = null; standCity = null;
      fastSequence++;
      if (fastSequence >= 2 && driveState !== "faehrt") driveBegins();
      return;
    }
    fastSequence = 0;
    if (v < STAND_KMH) asOfSeen = true;
    if (now < manualUntil || driveState === "geparkt") return;

    if (v < STAND_KMH) {
      if (standSince === null) { standSince = now; standCity = place; }
      const as_of = now - standSince;
      if (as_of >= PARK_TIME_MS) statePark("Das Auto steht seit drei Minuten");
      else if (as_of >= STANDING_TIME_MS && driveState === "faehrt") driveState = "steht";
    } else if (standCity && distanceM(standCity, place) > PATH_M) {
      // Slow and far from the stopping place: you got out and are walking.
      statePark("Du bist vom Auto weggegangen");
    }
  }

  /* The car is off - you can tell from the 12 V voltage, without asking.
   *
   * `ATRV` is measured by the ELM chip itself, nothing goes onto the CAN bus (see
   * `obd-core.js: spannung`). As long as the car is on or charging, the
   * DC/DC converter keeps the voltage up; when it goes off it falls within
   * seconds. That happens **before** locking - you switch off,
   * get out and lock - and it is thus the signal that the
   * phone's movement cannot provide.
   *
   * There is deliberately no fixed threshold: how high the voltage is
   * with the converter running differs from car to car. The comparison
   * is against the average of the last drive, and only while standing - a
   * drop during the drive is not a parking position. Whoever has never driven
   * has no basis; then the rules about standing and distance apply.
   *
   * If the car charges locked, the voltage stays up, and the
   * rules stay as they are. That is fine too: the query is then precisely
   * *not* the problem, as long as nothing is being asked any more. */
  const VOLTAGE_TICK_MS = 2000;
  const VOLTAGE_CASE_V = 0.7;
  const VOLTAGE_SEQUENCE = 2;
  const VOLTAGE_BASIS = 5;
  let voltageBasis = [];
  let voltageSequence = 0;
  let voltageRunning = false;
  let latestVoltage = null;
  let latestVoltageTime = 0;

  function avg(values) {
    const sorted = [...values].sort((a, b) => a - b);
    const m = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[m] : (sorted[m - 1] + sorted[m]) / 2;
  }

  async function examineVoltage() {
    if (voltageRunning || !K.state.sessionId || !dongle || !autoMode
        || driveState === "geparkt" || donglePause || !window.joltObd
        || !window.joltObd.linked() || !window.joltObd.voltage) return;
    voltageRunning = true;
    try {
      const v = await window.joltObd.voltage();
      if (typeof v !== "number" || !(v > 5 && v < 30)) return;
      latestVoltage = v;
      latestVoltageTime = Date.now();
      if (latestSpeed !== null && latestSpeed >= FAST_KMH) {
        voltageBasis.push(v);
        if (voltageBasis.length > 40) voltageBasis.shift();
        voltageSequence = 0;
        return;
      }
      if (Date.now() < manualUntil || voltageBasis.length < VOLTAGE_BASIS) return;
      if (v < avg(voltageBasis) - VOLTAGE_CASE_V) {
        if (++voltageSequence >= VOLTAGE_SEQUENCE) {
          statePark("Die 12-V-Spannung ist gefallen, das Auto ist aus");
        }
      } else {
        voltageSequence = 0;
      }
    } finally {
      voltageRunning = false;
    }
  }

  function statePark(reason) {
    if (driveState === "geparkt") return;
    // Set the state first, then disconnect: `trennen()` triggers the
    // connection drop, and its handling asks `readAllowed()`.
    driveState = "geparkt";
    standSince = null; standCity = null;
    asOfSeen = false;
    voltageBasis = []; voltageSequence = 0;
    if (dongle && window.joltObd) {
      try { window.joltObd.detach(); } catch (e) { /* already disconnected */ }
      K.report(reason + " – jolt fragt das Auto nicht mehr, bis du losfährst. "
        + "So löst ein abgeschlossenes Auto keinen Alarm aus.", "hinweis");
    }
    showDongle();
  }

  function driveBegins() {
    // After a failed attempt without a dongle only again once the vehicle has stopped -
    // otherwise a bicycle ride would try to connect all the way.
    if (driveState === "geparkt" && !asOfSeen) return;
    const wasParked = driveState === "geparkt";
    driveState = "faehrt";
    standSince = null; standCity = null;
    if (wasParked && dongle) {
      K.report("Fahrt erkannt – jolt verbindet den Dongle wieder.", "hinweis");
    }
    reconnectDongle();
    showDongle();
  }

  /* Fetch the dongle when it is missing - as long as the car is moving, but not
   * endlessly: if none is in range, the connection stays off, and every
   * further attempt only costs battery. */
  function reconnectDongle() {
    if (!dongle || !readAllowed() || !window.joltObd
        || window.joltObd.linked()) return;
    let attempts = 0;
    window.joltObd.reconnect(1, () => {
      if (!K.state.sessionId || !readAllowed()) return false;
      // Whoever is driving right now (GPS: at least 15 km/h) has not left
      // the car: giving up after two minutes would mean driving until the next
      // stop without vehicle values and reconnecting by hand. So it keeps
      // knocking for a good twelve minutes (every 20 s, costs little).
      // When standing - and on the bus without a dongle - it stays at eight attempts.
      const driving = latestSpeed !== null
                      && latestSpeed >= FAST_KMH;
      const bound = driving ? AGAIN_ATTEMPTS_DRIVING : AGAIN_ATTEMPTS_MAX;
      if (autoMode && ++attempts > bound) {
        driveState = "geparkt";
        asOfSeen = false;
        K.report("Kein Dongle in Reichweite – jolt versucht es erst nach dem "
          + "nächsten Halt wieder.", "hinweis");
        showDongle();
        return false;
      }
      return true;
    });
  }

  function showDongle() {
    const connectBtn = document.getElementById("dongle-on");
    const pause = document.getElementById("dongle-pause");
    if (!connectBtn || !pause) return;
    const linked = dongle && window.joltObd && window.joltObd.linked();
    connectBtn.hidden = linked && !donglePause;
    connectBtn.textContent = donglePause ? "Dongle wieder verbinden"
                                 : "Dongle verbinden";
    pause.hidden = !linked || donglePause;
  }

  async function donglePausieren() {
    donglePause = true;
    dongle = false;
    try { window.joltObd.detach(); } catch (e) {}
    showDongle();
    K.report("Dongle getrennt. Das Auto kann jetzt abgeschlossen werden – "
      + "jolt fragt nichts mehr über CAN. Die Fahrt läuft weiter, die "
      + "Position kommt vom Telefon.", "hinweis");
  }

  /* The dongle that is connected and still stays silent.
   *
   * Both rescue paths - the event `gattserverdisconnected` and the
   * follow-up on returning to the foreground - ask `verbunden()`
   * and only act if the connection is **gone**. The more frequent
   * case looks different: GATT keeps reporting "connected" but the dongle
   * no longer answers anything. Then every read round silently falls into
   * its `catch`, the measurement point goes out without vehicle values - and
   * nothing is ever triggered because formally everything is fine.
   *
   * On 2 September that cut two trips in half: in session 28, after
   * 16:45, 51 of 110 minutes arrived without a single value, in
   * session 26 the last 27 minutes. Both times the connection
   * stayed up, and both times nobody noticed.
   *
   * The remedy is to bring about the drop ourselves: `trennen()`
   * triggers `gattserverdisconnected`, and the reconnect that has long
   * existed hangs on that. If the event does not come, we follow up.
   *
   * None of this applies at the charger: there silence is wanted, and
   * a locked car that is queried over CAN again sets off the
   * alarm. Hence the condition on `donglePause`. */
  const QUIET_RESTART_MS = 120000;

  function quietWatch() {
    if (!readAllowed() || !window.joltObd || !window.joltObd.linked()) return;
    // If nothing ever came, the clock runs from now - otherwise the
    // monitoring waits for a value that never comes, and never steps in.
    if (!latestRawValuesTime) { latestRawValuesTime = Date.now(); return; }
    if (Date.now() - latestRawValuesTime < QUIET_RESTART_MS) return;
    // Advance the clock immediately, otherwise the next round triggers
    // the same rebuild again while the first is still running.
    latestRawValuesTime = Date.now();
    K.report("Der Dongle antwortet seit zwei Minuten nicht mehr – jolt baut "
      + "die Verbindung neu auf.", "hinweis");
    try { window.joltObd.detach(); } catch (e) { /* already disconnected */ }
    setTimeout(reconnectDongle, 3000);
  }

  /* Offer the dongle before anything else runs.
   *
   * The order is not arbitrary: `requestDevice` may only run in
   * immediate response to a user gesture. Whoever waits for GPS or
   * an API response beforehand has used up the gesture and gets a
   * `SecurityError`. That is why the dongle comes **first** - even before
   * creating the session.
   *
   * If none comes about - no Bluetooth in the browser, no dongle in the
   * car, dialog tapped away -, that is no reason to abort but
   * one to carry on without: a trip with a state of charge reported by hand
   * is better than no trip. Nothing is decided here,
   * the return value only says what became of it. */
  async function dongleOffer() {
    if (!window.joltObd || !window.joltObd.obtainable()) return false;
    try {
      dongleUse();
      // First without a dialog: if the dongle has been allowed once,
      // it connects without a tap.
      await window.joltObd.attach();
      if (await handshakeSafe()) return true;
    } catch (failure) {
      console.log("[obd] Verbindung nicht zustande gekommen:", failure);
    }
    dongle = false;
    return false;
  }

  /* Send the handshake, twice if in doubt.
   *
   * The ELM often answers the first ATZ after connecting only after
   * seconds, or a reply arrives too late and hits the next command.
   * The sequence keeps running anyway and configures the dongle - only the
   * verdict "incomplete" used to be final, and with it the start gave up
   * the dongle although it was connected and delivering values. Hence: a
   * second pass, and whoever is connected afterwards gets used.
   * Returns whether the dongle is usable; the reasons are in the log. */
  async function handshakeSafe() {
    const O = window.joltObd;
    for (let attempt = 1; attempt <= 2; attempt++) {
      if (!O.linked()) return false;
      if (await O.handshake()) return true;
      console.log("[obd] Handshake unvollständig (Versuch " + attempt + "):",
                  (O.seriesError ? O.seriesError() : []).join("; "));
    }
    return O.linked();
  }

  async function connectDongle() {
    const btn = document.getElementById("dongle-on");
    if (btn) btn.disabled = true;
    try {
      if (!window.joltObd || !window.joltObd.obtainable()) {
        K.report("Dieser Browser kann kein Bluetooth. Mit Dongle: dieselbe "
          + "Adresse in Bluefy öffnen.", "fehler");
        return;
      }
      donglePause = false;
      // Whoever connects by hand wants to read - even while standing, for ten minutes.
      manualUntil = Date.now() + MANUAL_MS;
      if (driveState === "geparkt") driveState = "steht";
      dongleUse();
      await window.joltObd.attach();
      if (!window.joltObd.linked()) throw new Error("keine Verbindung");
      if (!(await handshakeSafe())) throw new Error("keine Verbindung");
      K.report("Dongle verbunden – ab jetzt kommen die Werte aus dem Auto.",
               "hinweis");
    } catch (failure) {
      dongle = false;
      K.report("Dongle: " + failure.message, "fehler");
    } finally {
      if (btn) btn.disabled = false;
      showDongle();
    }
  }

  function dongleUse() {
    dongle = true;
    donglePause = false;
    if (window.joltObd) {
      window.joltObd.set_up(
        (t) => console.log("[obd]", t),
        // A drop in a tunnel is no reason to stop as long as the trip
        // is running: the component rebuilds the connection itself.
        () => { if (K.state.sessionId) reconnectDongle(); });
    }
  }

  /* Power from voltage times current. The sign is **not** confirmed:
   * the raw value of the current is offset by 150000, so positive means
   * either discharging or charging - which of the two, the first measurement
   * on the vehicle will show. Hence the magnitude is displayed and the direction
   * named, instead of making an assumption you cannot see. */
  function powerKw(raw) {
    if (!raw || typeof raw.voltage_v !== "number"
        || typeof raw.current_a !== "number") return null;
    return raw.voltage_v * raw.current_a / 1000;
  }

  function auxLoadRemember(raw) {
    // If the measured value is available, the approximation is not needed.
    if (typeof raw.aux_load_kw === "number") return;
    const kw = powerKw(raw);
    if (kw === null) return;
    const speed = typeof raw.speed_kmh === "number" ? raw.speed_kmh : null;
    // Only while standing, and only when energy is drawn - while charging
    // you measure the charger, not the heater.
    if (speed !== null && speed < 5 && kw > 0) {
      aux_load = { kw, timestamp: Date.now() };
    }
  }

  /* What the car measures - as a **row**, not a row of tiles.
   *
   * Six more tiles would have been the same size as the four
   * above, and everything would have looked equally important. But these values
   * are only needed occasionally: you look when you want to know
   * *why* the consumption is high - not to learn that it is.
   */
  function autoRow(z) {
    const block = document.getElementById("live-car");
    const raw = latestRawValues;
    if (!block) return;
    if (!raw) {
      block.hidden = true;
      const to = document.getElementById("live-raw");
      if (to) to.hidden = true;
      return;
    }
    block.hidden = false;

    const kw = powerKw(raw);
    const speed = typeof raw.speed_kmh === "number" ? raw.speed_kmh : null;
    const current = (kw !== null && speed !== null && speed >= 5)
      ? Math.abs(kw) / speed * 100 : null;

    const parts = [];
    /* The consumption of the trip first: that is the number you
     * record for. The instantaneous power below it is an extra - and on the
     * ID.Buzz it is not available anyway, because the battery current does not
     * answer. */
    const so_far = runningConsumption(raw, z && z.actual_soc);
    if (so_far) {
      parts.push(`<b>${K.num(so_far.kwh100, 1)}</b> kWh/100 auf `
                 + `${K.num(so_far.km, 0)} km`);
    }
    if (current !== null) {
      parts.push(`<b>${K.num(current, 1)}</b> kWh/100 gerade`);
    }
    if (kw !== null) {
      parts.push(`<b>${K.num(Math.abs(kw), 0)}</b> kW${kw < 0 ? " zurück" : ""}`);
    }
    if (typeof raw.aux_load_kw === "number") {
      let n = `<b>${K.num(raw.aux_load_kw, 1)}</b> kW Nebenverbraucher`;
      if (typeof raw.ptc_current_a === "number"
          && typeof raw.voltage_v === "number") {
        n += `, davon <b>${K.num(raw.ptc_current_a * raw.voltage_v / 1000, 1)}</b> Heizung`;
      }
      parts.push(n);
    } else if (aux_load) {
      const age = Math.round((Date.now() - aux_load.timestamp) / 60000);
      parts.push(`<b>${K.num(aux_load.kw, 1)}</b> kW Nebenverbraucher`
                 + ` (im Stand${age > 0 ? `, vor ${age} min` : ""})`);
    }
    /* Outside temperature and odometer were here too. They are
     * right and interesting, but not **while driving** - and a row
     * with six items is not read at all any more. Both are in the
     * table under the flap, where you look for them if you look for them. */

    // Row and table now sit in different places: the row
    // up by the tiles, the table below behind the flap.
    document.getElementById("live-car-row").innerHTML = parts.join(" · ");
    const flap = document.getElementById("live-raw");
    if (flap) {
      flap.hidden = false;
      document.getElementById("live-car-values").innerHTML =
        rawValuesTable(raw);
    }

    /* How old the last set is - the question you really have at the
     * wheel. "3 s" means the dongle is answering; "4 min" means it is gone,
     * and the numbers below are memories. Without this indication a frozen
     * display looks exactly like a running one. */
    const age = latestRawValuesTime
      ? Math.round((Date.now() - latestRawValuesTime) / 1000) : null;
    const as_of = document.getElementById("live-car-status");
    if (age === null) {
      as_of.textContent = "";
    } else if (age < 90) {
      as_of.textContent = `vor ${age} s`;
      as_of.style.color = "";
    } else {
      as_of.textContent = `seit ${K.duration(age / 60)} keine Antwort`;
      as_of.style.color = "#e8804f";
      /* Say clearly, once, that nothing comes from the car any more.
       *
       * The pale line behind the flap is not enough for that. On a
       * real trip some twenty kilometres were recorded without a single
       * vehicle value - GPS kept running, the dongle
       * was gone, and nobody noticed. A recording without
       * state of charge is worthless for learning, and otherwise you only find
       * that out afterwards.
       *
       * Three minutes, not ninety seconds: a tunnel or a brief
       * lock should not report, a dropped dongle should. */
      if (!quietReported && age > 180) {
        quietReported = true;
        K.report("Seit drei Minuten kommt nichts mehr aus dem Auto. jolt "
          + "zeichnet die Strecke weiter auf, aber ohne Ladestand – zum "
          + "Lernen taugt sie dann nicht. jolt in den Vordergrund holen, "
          + "dann verbindet sich der Dongle von selbst wieder.", "fehler");
      }
    }
  }

  /* Everything the car delivers - as a table, not as a sentence.
   *
   * The row above answers "how is it going right now". This table
   * answers the other question: "does what should arrive actually arrive".
   * For that it must also show what has **not** answered - a missing
   * value is the more interesting information when setting up than a
   * present one, and a counter ("3 values do not answer") does not say
   * which three.
   *
   * The list comes from `joltObd.FELDER`, so that a new data identifier shows up
   * here by itself and does not have to be maintained in two places. */
  function valuesRemember(raw) {
    const now = Date.now();
    // For the consumption plot: odometer and state of charge with a timestamp.
    // The power is deliberately not included - see verbrauchsabschnitte().
    if (typeof raw.odometer_km === "number") {
      /* `netto` is the counter reading: discharged minus charged. Its
       * difference over a stretch of driving **is** the energy used -
       * without a detour via state of charge and battery size, and with 0.117 Wh
       * resolution instead of 339. */
      const net = (typeof raw.discharge_kwh === "number")
        ? raw.discharge_kwh - (typeof raw.charged_kwh === "number"
                              ? raw.charged_kwh : 0)
        : null;
      consumption_track.push({
        timestamp: now, km: raw.odometer_km, net,
        // The two counters individually, for the display of recuperation.
        disch: typeof raw.discharge_kwh === "number" ? raw.discharge_kwh : null,
        chg: typeof raw.charged_kwh === "number" ? raw.charged_kwh : null,
        // The GPS distance is filled in in `showState` as soon as
        // the position of this point is known.
        gps: null,
        soc: typeof raw.soc_raw === "number" ? raw.soc_raw / 2.5 : null });
      // Generous: 20,000 points at a twelve-second beat are roughly
      // 66 hours. With 3000 the first points would have dropped out after
      // ten hours - and with them the start of the trip.
      if (consumption_track.length > 20000) consumption_track.shift();
    }
    for (const [name, val] of Object.entries(raw)) {
      if (typeof val === "number") {
        valuesAsOf[name] = { val, timestamp: now };
        unanswered.delete(name);
      }
    }
    // "Answered, but without a usable value" means: the data identifier
    // does not fit this vehicle. That stays so until a value does come
    // after all - hence remembered and not decided anew each round.
    for (const name of raw._empty || []) {
      if (!valuesAsOf[name]) unanswered.add(name);
    }
  }

  /* The consumption of the current trip in kWh/100 km.
   *
   * **Why computed and not read.** The MEB list has no parameter for
   * it; the car shows the value in the on-board computer but does not release it
   * via diagnostics. It arises here from two quantities that
   * both arrive every round:
   *
   *   kWh used = (state of charge at start − now) / 100 × net battery
   *   km driven    = odometer now − at start
   *
   * **The odometer and not the GPS.** It counts wheel revolutions and
   * knows neither cut-off bends nor dead spots - for a quantity with
   * the distance in the denominator that is the difference between usable and
   * misleading.
   *
   * It does resolve in whole kilometres, though. Under five driven
   * kilometres nothing is therefore shown: at two kilometres the figure would be
   * accurate to ±50 % and thus worse than none.
   */
  const CONSUMPTION_FROM_KM = 5.0;
  /* From how much energy **in** while the car is standing it
   * counts as a charging session. Recuperation exists only while driving; whoever stands and still
   * takes in energy is on the cable. */
  const CHARGING_AS_OF_KWH = 0.05;

  function runningConsumption(raw, soc) {
    const car = K.state.recVehicle
      || (K.state.trip && K.state.trip.vehicle);
    const battery = car && (car.capacity_kwh || car.battery_net_kwh);
    if (consumption_track.length < 2) return null;

    /* **Summed up instead of start against end.**
     *
     * Here used to be `netto - anfang.netto`, and that is right on a short trip.
     * On a **long** one it is not: the `geladen` counter also grows
     * at the charger. Whoever recharges forty kilowatt hours sees their
     * net counter fall by forty - the displayed consumption of the trip
     * would then be near zero or negative.
     *
     * So section by section, and charging sessions drop out: energy in
     * while the car is standing is not recuperation but the cable. Everything
     * else counts, including the consumption while standing - that is real.
     */
    let kwh = 0, km = 0;
    for (let i = 1; i < consumption_track.length; i++) {
      const a = consumption_track[i - 1], b = consumption_track[i];
      const dkm = (b.km ?? 0) - (a.km ?? 0);
      let d = null;
      if (a.net !== null && b.net !== null) d = b.net - a.net;
      else if (battery && a.soc !== null && b.soc !== null) {
        d = (a.soc - b.soc) / 100 * battery;
      }
      if (d === null) continue;
      if (dkm <= 0 && d < -CHARGING_AS_OF_KWH) continue;   // at the charger
      kwh += d;
      km += Math.max(0, dkm);
    }
    if (km < CONSUMPTION_FROM_KM) return null;
    return { kwh100: kwh / km * 100, kwh, km };
  }

  /* ---------- Consumption per time section ---------- */

  /* **Where the energy comes from, and what that means for the bar width.**
   *
   * First draft: sum up power (voltage times current) over time.
   * Wrong - the current changes every second,
   * reporting happens every twelve seconds. Five samples per minute are
   * not an integral but a survey; on the country road 25 % error,
   * in the city 59 %.
   *
   * Second draft: from the **state of charge**. Its quantisation error is
   * absolutely bounded (one step, 0.44 pp = 339 Wh), so relatively the
   * smaller the longer the section - from five minutes on everywhere under
   * 3 %. But only from five minutes on.
   *
   * Now: the vehicle's **energy counters**. Their difference is the
   * energy used, with 0.117 Wh resolution - almost three thousand times
   * finer than the state of charge. With that a bar per minute is no longer an
   * estimate but a measurement (0.05 % instead of 136 %).
   *
   * The bar width therefore follows the source, and not the wish:
   * one minute with the counter, five without.
   */
  const SECTION_WITH_COUNTER_S = 60;
  const SECTION_FROM_SOC_S = 300;
  /* On a long trip the minute becomes too fine.
   *
   * A bar per minute is just right over half an hour - over
   * six hours it would be 360 bars on roughly 340 pixels, i.e. 0.9 pixels
   * per bar. That is no longer a chart but a texture.
   *
   * So the section width grows with the trip, but only to round
   * values: two minutes you still read as two minutes, 87 seconds you do not.
   * Accuracy does not suffer - with the counters even the
   * minute is far above the resolution limit, wider only gets better. */
  const WIDTHS_MIN = [1, 2, 5, 10, 15, 30, 60];
  const BAR_AT_MOST = 60;

  function widthChoose(duration_ms, min_s) {
    for (const min of WIDTHS_MIN) {
      if (min * 60 < min_s) continue;
      if (duration_ms / (min * 60000) <= BAR_AT_MOST) return min * 60000;
    }
    return WIDTHS_MIN[WIDTHS_MIN.length - 1] * 60000;
  }
  // Below this distance kWh/100 km makes no sense - the car was standing.
  const BAR_MIN_KM = 0.3;

  /* **The distance per bar comes from the GPS, the energy from the counters.**
   *
   * That sounds like a step backwards - for the **total distance** of a
   * recording the odometer is precisely the better source, because
   * it neither cuts off bends nor knows dead spots. Over a single
   * minute this reverses: it resolves in whole kilometres, and one
   * minute at seventy km/h is 1.2 km. What gets measured is 1 or 2 -
   * forty percent error on the denominator. The GPS track, with a report
   * every twelve seconds, cuts off only a few percent.
   *
   * Noticed on a real trip: the kilometre column per minute stood
   * constantly at 0 or 1, and the bars fluctuated accordingly.
   *
   * The energy stays with the counters - there the resolution is 0.117 Wh
   * and thus no issue. Each quantity from the source that knows
   * it best. */
  function consumption_sections() {
    const points = consumption_track.filter((p) => typeof p.gps === "number");
    if (points.length < 2) return null;
    const withCounter = points.every((p) => typeof p.net === "number");
    const car = K.state.recVehicle
      || (K.state.trip && K.state.trip.vehicle) || {};
    const battery = car.capacity_kwh || car.battery_net_kwh;
    if (!withCounter && !battery) return null;

    const onset = points[0].timestamp;
    const bucketWidth = widthChoose(
      points[points.length - 1].timestamp - onset,
      withCounter ? SECTION_WITH_COUNTER_S : SECTION_FROM_SOC_S);
    const buckets = new Map();
    for (const p of points) {
      const n = Math.floor((p.timestamp - onset) / bucketWidth);
      if (!buckets.has(n)) buckets.set(n, []);
      buckets.get(n).push(p);
    }

    const bar = [];
    for (const [n, group] of [...buckets.entries()].sort((a, b) => a[0] - b[0])) {
      if (group.length < 2) continue;
      const first = group[0], final = group[group.length - 1];
      const km = final.gps - first.gps;
      if (km < BAR_MIN_KM) continue;
      const kwh = withCounter ? (final.net - first.net)
        : ((first.soc !== null && final.soc !== null)
           ? (first.soc - final.soc) / 100 * battery : null);
      if (kwh === null || !Number.isFinite(kwh)) continue;
      bar.push({ n, kwh100: kwh / km * 100, km });
    }
    return bar.length ? { bar, bucketWidth, withCounter } : null;
  }

  function drawConsumption() {
    const canvas = document.getElementById("live-consumption");
    const foot = document.getElementById("live-consumption-foot");
    if (!canvas || !foot) return;
    const sections = consumption_sections();
    if (!sections) { canvas.hidden = true; foot.hidden = true; return; }
    canvas.hidden = false; foot.hidden = false;

    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const vals = sections.bar.map((b) => b.kwh100);
    // The scale generous upwards, so that an outlier does not flatten the other
    // bars, and with a zero line: recuperation goes
    // below zero, and that is exactly what you should see.
    const upper = Math.max(40, ...vals) * 1.1;
    const bottom = Math.min(0, ...vals) * 1.1;
    const span = upper - bottom || 1;
    const edge = 6, footHeight = 16;
    const area = height - footHeight - edge;
    const y = (v) => edge + (upper - v) / span * area;

    /* Labelled axis. Without it a bar chart is a shape without a
     * message - you see that one minute was more expensive than another, but
     * not whether it is twenty or forty kWh/100 km. And that is
     * the number you compare with your own feeling.
     *
     * Labels go on the left, into the area: a separate column
     * for it would be too expensive on the phone. Three lines suffice - zero, a
     * round value in between and the maximum. */
    const axis = 30;
    const split = [0];
    const step = upper > 60 ? 25 : (upper > 25 ? 10 : 5);
    for (let w = step; w < upper; w += step) split.push(w);
    for (let w = -step; w > bottom; w -= step) split.push(w);

    ctx.font = "10px system-ui, sans-serif";
    ctx.textBaseline = "middle";
    for (const w of split) {
      const yy = y(w);
      ctx.strokeStyle = w === 0 ? "#3a4652" : "#222c36";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(axis, yy); ctx.lineTo(width - edge, yy);
      ctx.stroke();
      ctx.fillStyle = "#8a97a5";
      ctx.textAlign = "right";
      ctx.fillText(String(w), axis - 4, yy);
    }
    // The unit once at the top left, not on every tick.
    ctx.fillStyle = "#8a97a5";
    ctx.textAlign = "left";
    ctx.fillText("kWh/100", axis + 3, edge + 4);

    const field = width - edge - axis;
    const b = Math.max(2, field / sections.bar.length - 2);
    sections.bar.forEach((bar, i) => {
      const x = axis + i * (field / sections.bar.length);
      const high = y(bar.kwh100) - y(0);
      // Colour by height: what lies clearly above the average stands out.
      ctx.fillStyle = bar.kwh100 < 0 ? "#57c98a"
        : (bar.kwh100 > 35 ? "#e8804f" : "#ffc93c");
      ctx.fillRect(x, high < 0 ? y(bar.kwh100) : y(0),
                     b, Math.max(1, Math.abs(high)));
    });

    const average = vals.reduce((a, v) => a + v, 0) / vals.length;
    foot.children[0].textContent =
      `Verbrauch je ${sections.bucketWidth / 60000} min`
      + (sections.withCounter ? "" : " (aus dem Ladestand)");
    foot.children[1].textContent = `Ø ${K.num(average, 1)} kWh/100`;
  }

  function ageText(seconds) {
    if (seconds < 60) return `vor ${Math.round(seconds)} s`;
    return `vor ${Math.round(seconds / 60)} min`;
  }

  function rawValuesTable(raw) {
    const fields = (window.joltObd && window.joltObd.FIELDS) || [];
    if (!fields.length) return "";
    const missing = new Set(raw._missing || []);
    const now = Date.now();

    const rows = fields.map((f) => {
      const as_of = valuesAsOf[f.name];
      if (!as_of) {
        // Never a value yet. The reason differs, and the
        // difference is the real information when setting up.
        const reason = unanswered.has(f.name) ? "antwortet nicht"
          : (missing.has(f.name) ? "keine Antwort" : "–");
        return `<tr class="leer"><th>${f.title}</th><td>${reason}</td></tr>`;
      }
      const age = (now - as_of.timestamp) / 1000;
      const shown = K.num(as_of.val, f.put)
        + (f.unit ? " " + f.unit : "");
      // Fresh means: arrived in this round. Everything else gets its
      // age written next to it and becomes paler the older it is - this way
      // you can see at a glance which row is still alive.
      if (typeof raw[f.name] === "number") {
        return `<tr><th>${f.title}</th><td>${shown}</td></tr>`;
      }
      const category = age > 120 ? "alt sehr" : "alt";
      return `<tr class="${category}"><th>${f.title}</th>`
        + `<td>${shown}<span class="wann">${ageText(age)}</span></td></tr>`;
    });
    return `<table class="rohwerte"><tbody>${rows.join("")}</tbody></table>`;
  }

  /* Values the server accepts - and none else.
   *
   * Since the hardening (bug scan #49) the server rejects the impossible with 422:
   * speed outside 0 to 500 km/h, outside temperature outside -80 to
   * 70 °C. The car gives cause enough for that: the speed byte is at
   * 255 for "invalid" (this occurs in the stored trips), and the
   * outside temperature `b0 / 2 - 50` gives 77.5 °C at 0xFF. Such a value
   * must not cost the point it belongs to - and certainly not the
   * batch. Here it becomes "not measured".
   *
   * 250 km/h instead of 500: 255 is the car's placeholder, and no
   * vehicle that jolt knows drives 250. */
  function speedOrNull(val) {
    return (typeof val === "number" && Number.isFinite(val)
            && val >= 0 && val <= 250) ? val : null;
  }

  function temperatureOrNull(val) {
    return (typeof val === "number" && Number.isFinite(val)
            && val >= -80 && val <= 70) ? val : null;
  }

  async function reportPosition(coords, timeMs) {
    if (!K.state.sessionId) return;
    const payload = {
      lat: coords.latitude, lon: coords.longitude,
      // From the fix: m/s, and -1 or null if the device does not know.
      speed_kmh: speedOrNull(typeof coords.speed === "number"
                               ? coords.speed * 3.6 : null),
      // When it was measured, not when it arrives - otherwise all points
      // submitted late from a dead spot would be dated to the same second.
      timestamp: new Date(timeMs || Date.now()).toISOString(),
    };

    if (dongle && readAllowed() && window.joltObd
        && window.joltObd.linked()) {
      try {
        const raw = await window.joltObd.readRecord(readRound++);
        // Record the 12 V voltage along, as long as it is fresh: that way
        // you can later check how far it falls when switching off.
        if (latestVoltage !== null && Date.now() - latestVoltageTime < 15000) {
          raw.batt_v = latestVoltage;
        }
        if (typeof raw.elevation_m !== "number" && typeof coords.altitude === "number") {
          raw.elevation_m = Math.round(coords.altitude);
        }
        latestRawValues = raw;
        latestRawValuesTime = Date.now();
        quietReported = false;
        valuesRemember(raw);
        auxLoadRemember(raw);
        const val = window.joltObd.socFromRaw(raw.soc_raw);
        payload.soc = Math.round(val.hmi * 10) / 10;
        payload.raw_values = raw;
        // What the car itself measures beats any prediction - if it is
        // plausible. An "invalid" (255 km/h, 77.5 °C) leaves the GPS speed
        // in place and the temperature empty.
        const autoSpeed = speedOrNull(raw.speed_kmh);
        if (autoSpeed !== null) payload.speed_kmh = autoSpeed;
        const autoTemp = temperatureOrNull(raw.outside_temp_c);
        if (autoTemp !== null) payload.outside_temp_c = autoTemp;
      } catch (failure) {
        // A round without state of charge is still a position report -
        // and that carries the time factor and arrival forecast onward.
        console.log("[obd] Runde übersprungen:", failure);
      }
      quietWatch();
    }

    // Include the voltage even without a vehicle query. While standing nothing
    // is asked, but `ATRV` is still measured - and that is exactly where it falls when
    // the car goes off. Without this line the drop would be recorded nowhere, and the
    // threshold could not be checked against a real trip.
    if (!payload.raw_values && latestVoltage !== null
        && Date.now() - latestVoltageTime < 15000) {
      payload.raw_values = { batt_v: latestVoltage };
    }
    bufferAppend(payload);
    bufferProcess();
  }

  /* ---------- Buffering measurement points ---------- */

  /* Every measurement point goes into a queue first and from there to the
   * server - never directly. If the network drops, the points stay put and
   * go out together on the next attempt, with their measurement time.
   *
   * Before, `reportPosition` swallowed the failed POST. For a planned
   * trip that is harmless, the next point comes. For a recording it is
   * data loss: after twenty minutes without network up to 13 % of the route
   * was missing, and the learned factor shifted by up to 35 % -
   * invisibly, in a number that stays on the vehicle permanently.
   *
   * There is only ever **one** send operation. Two simultaneous ones could
   * overtake each other, and the server would get newer points before older ones.
   *
   * The queue is also kept in localStorage: if iOS reloads the page in the
   * background, the points should not vanish with it. The limit
   * protects against memory that grows forever; then the oldest go. */
  const BUFFER_MAX = 2000;
  const BATCH_MAX = 100;
  // How many points the next batch has. Equal to BATCH_MAX, unless the
  // server has just rejected a batch: then it is halved until the
  // one bad point is identified.
  let batchSize = BATCH_MAX;
  let buffer = [];
  let bufferSession = null;
  let bufferRun = null;
  let bufferAgain = false;
  let withoutGridReported = false;
  let supplied_later = 0;
  let rejected = 0;              // individual points rejected by the server

  function bufferStorage(id) { return "jolt-puffer-" + id; }

  function bufferFor(id) {
    if (bufferSession === id) return;
    bufferSession = id;
    buffer = [];
    try {
      const raw = JSON.parse(localStorage.getItem(bufferStorage(id)) || "[]");
      if (Array.isArray(raw)) buffer = raw;
    } catch (e) { /* no storage or corrupted: carry on without */ }
  }

  function saveBuffer() {
    if (bufferSession === null) return;
    try {
      if (buffer.length) {
        localStorage.setItem(bufferStorage(bufferSession), JSON.stringify(buffer));
      } else {
        localStorage.removeItem(bufferStorage(bufferSession));
      }
    } catch (e) { /* full or blocked: then it just stays in memory */ }
  }

  function bufferAppend(point) {
    bufferFor(K.state.sessionId);
    buffer.push(point);
    if (buffer.length > BUFFER_MAX) buffer.splice(0, buffer.length - BUFFER_MAX);
    // It is only saved when something piles up - normally the
    // point is on the server a second later.
    if (buffer.length > 1) saveBuffer();
  }

  function bufferProcess() {
    if (bufferRun) { bufferAgain = true; return bufferRun; }
    bufferRun = sendBuffer().finally(() => { bufferRun = null; });
    return bufferRun;
  }

  async function sendBuffer() {
    do {
      bufferAgain = false;
      const id = K.state.sessionId;
      while (id && buffer.length && K.state.sessionId === id) {
        const batch = buffer.slice(0, batchSize);
        let state;
        try {
          state = await K.api(`/api/live/${id}/points`,
            { method: "POST", body: { points: batch } });
        } catch (failure) {
          const status = failure.status;
          if (status === 404 || status === 409) {
            // The session no longer exists or has ended: sending on
            // would mean fetching the same rejection for all eternity.
            buffer = [];
          } else if (status === 422) {
            // The server considers the batch invalid - a point in it, say
            // with a timestamp from more than two days ago. The whole
            // batch would never be accepted and would clog everything behind it.
            //
            // Here the batch used to be discarded: up to ninety-nine good ones
            // because of one bad point. Now it is halved until the
            // one point is identified; only it gets thrown out. For a hundred
            // points that costs at most seven further requests.
            if (batch.length > 1) {
              batchSize = Math.ceil(batch.length / 2);
            } else {
              buffer.splice(0, 1);
              batchSize = BATCH_MAX;
              rejected += 1;
            }
          } else if (!withoutGridReported) {
            withoutGridReported = true;
            K.report("Keine Verbindung zu jolt – die Messpunkte werden "
              + "gesammelt und nachgereicht.", "hinweis");
          }
          saveBuffer();
          // Network gone, server overloaded or logged out: leave it lying, the
          // next point or the return of the network tries again.
          if (status !== 404 && status !== 409 && status !== 422) return;
          continue;
        }
        buffer.splice(0, batch.length);
        saveBuffer();
        if (withoutGridReported) supplied_later += batch.length;
        // Show the state only when the batch has reached the present.
        // In the middle of catching up it would be the one from ten minutes ago.
        if (!buffer.length) {
          if (withoutGridReported) {
            withoutGridReported = false;
            K.report("Verbindung wieder da – " + supplied_later
              + " Messpunkte nachgereicht.", "hinweis");
            supplied_later = 0;
          }
          showState(state);
        }
      }
    } while (bufferAgain);
    // After a rejection it continues in small batches until the bad
    // point is found (then the size is full again) - or until nothing
    // is waiting any more. Growing after every success would hit the bad point
    // again and again: for 13 points that cost seven rejected requests
    // instead of four.
    batchSize = BATCH_MAX;
    if (rejected > 0) {
      K.report(`${rejected} Messpunkt${rejected === 1 ? " wurde" : "e wurden"} `
        + "vom Server abgelehnt und verworfen - die übrigen sind angekommen.",
        "hinweis");
      rejected = 0;
    }
  }

  window.addEventListener("online", () => {
    if (K.state.sessionId) bufferProcess();
  });

  function fetchLocation() {
    return new Promise((fulfil, reject) => {
      if (!navigator.geolocation) {
        reject(new Error("Dieses Gerät liefert keinen Standort."));
        return;
      }
      navigator.geolocation.getCurrentPosition(
        (pos) => fulfil({ lat: pos.coords.latitude, lon: pos.coords.longitude,
                             speed_kmh: pos.coords.speed === null ? null
                               : pos.coords.speed * 3.6 }),
        // Without a location the state of charge alone is worthless: only the position
        // says which target value it is to be compared with.
        (failure) => reject(new Error("Standort nicht verfügbar ("
          + failure.message + "). Über HTTPS oder localhost erlaubt der "
          + "Browser den Zugriff.")),
        { enableHighAccuracy: true, timeout: 10000, maximumAge: 5000 });
    });
  }

  /* Write the last known state of charge into the field - but never while
   * someone is typing in it. At the charge point the value is typed in, and a field
   * that changes under the fingers while typing because a
   * message just came in over the WebSocket is worse than an empty one. */
  function socFieldPrefill(val) {
    const field = document.getElementById("actual-soc");
    if (!field || document.activeElement === field) return;
    if (val === null || val === undefined || Number.isNaN(val)) return;
    field.value = Math.round(val);
  }

  async function reportSoc() {
    if (!K.state.sessionId) { K.report("Keine Live-Fahrt.", "fehler"); return; }
    const btn = document.getElementById("soc-report");
    const field = document.getElementById("actual-soc");
    const soc = Number(field.value);
    if (!field.value || !(soc >= 0 && soc <= 100)) {
      K.report("Ladestand zwischen 0 und 100 % angeben.", "fehler");
      return;
    }

    btn.disabled = true;
    // Dismiss the keyboard, otherwise on the phone it covers exactly the values
    // for which the state of charge was just reported.
    field.blur();
    try {
      const place = await fetchLocation();
      const state = await K.api(`/api/live/${K.state.sessionId}/point`,
        { method: "POST", body: { lat: place.lat, lon: place.lon, soc: soc,
                                  speed_kmh: place.speed_kmh } });
      showState(state);
      // The deviation is the reason typing it in is worthwhile - so
      // it belongs on screen right afterwards as a sentence and not just
      // as a tile among five others.
      const explanation = document.getElementById("soc-explanation");
      if (explanation) {
        explanation.textContent = state.deviation_pp === null
          || state.deviation_pp === undefined
          ? "Aufgenommen."
          : (Math.abs(state.deviation_pp) < 0.5
            ? "Aufgenommen – genau im Plan."
            : `Aufgenommen – ${K.num(Math.abs(state.deviation_pp), 1)} `
              + `Prozentpunkte ${state.deviation_pp < 0 ? "unter" : "über"} Plan.`);
      }
    } catch (failure) {
      K.report(failure.message, "fehler");
    } finally {
      btn.disabled = false;
    }
  }

  async function finish() {
    if (!K.state.sessionId) return;
    // What is still in the queue belongs to the trip - and after
    // ending the server accepts nothing more.
    bufferFor(K.state.sessionId);
    if (buffer.length) await bufferProcess();
    if (buffer.length) {
      K.report(buffer.length + " Messpunkte konnten nicht mehr übertragen "
        + "werden - kein Netz.", "warnung");
    }
    let result = null;
    try {
      result = await K.api(`/api/live/${K.state.sessionId}/end`,
                             { method: "POST" });
    } catch (failure) { /* an already ended trip is not a problem */ }
    // The trips view has cached the list; a trip that has just
    // ended belongs in it.
    K.state.tripsStale = true;
    positionGiveUp();
    buffer = [];
    saveBuffer();
    bufferSession = null;
    withoutGridReported = false;
    supplied_later = 0;
    dongle = false;
    track = [];
    drivenKm = 0;
    history = [];
    latestRawValues = null;
    latestRawValuesTime = 0;
    quietReported = false;
    valuesAsOf = {};
    unanswered = new Set();
    consumptionStart = null;
    consumption_track = [];
    aux_load = null;
    if (reconnectClock) { clearTimeout(reconnectClock); reconnectClock = null; }
    if (socket) {
      try { socket.onclose = null; socket.close(); } catch (e) {}
    }
    K.state.sessionId = null;
    K.sessionRemember(null);
    drivingStateStart("steht");
    // The trip is over: the display in the car should disappear.
    try { if (window.joltDisplay) window.joltDisplay.finish(); }
    catch (e) { console.log("[anzeige]", e && e.message); }
    plan = null;
    const box = document.getElementById("live-change");
    if (box) box.hidden = true;
    document.getElementById("live-content").hidden = true;
    document.getElementById("live-empty").hidden = false;
    reportLearned(result);
    if (result && result.as_of_discarded) {
      K.report(`Die letzten ${result.as_of_discarded.discarded_minutes} min `
        + "Stillstand wurden verworfen - die Fahrt endet beim letzten Fahren.",
        "hinweis");
    }
  }

  /* What jolt has learned from the trip - and why not, if not.
   *
   * The backend updates the vehicle's correction factor at every
   * end of trip and reports the result back; so far nobody has
   * read it. For the purpose at hand - driving short known routes
   * and learning the real consumption from them -, this is the only
   * return channel. Without it you drive the same route three times and afterwards
   * do not know whether anything arrived at all.
   *
   * The absence is reported too: `gelernt: null` means "too short or
   * implausible". A trip that silently contributes nothing otherwise
   * looks like one that confirmed. */
  /* Where the elevations came from - and only if it was not the map.
   *
   * A consumption without an elevation profile cannot be interpreted: whether 22 kWh/100 km
   * was down to driving style or to four hundred metres of climb cannot be
   * separated from the consumption alone. If the map query fails, the
   * trip goes through anyway - but then you should know, instead of taking the number
   * at face value later. */
  function reportElevations(built) {
    if (!built || !built.ok) return;
    if (built.elevations === "gps") {
      K.report("Die Höhen dieser Fahrt kommen aus dem GPS, nicht aus der "
        + "Karte – geglättet, aber ungenauer. Der gelernte Faktor ist "
        + "entsprechend weicher.", "hinweis");
    } else if (built.elevations === "flach") {
      K.report("Für diese Fahrt gab es keine Höhendaten; sie wurde flach "
        + "gerechnet. Auf einer Runde macht das wenig aus, auf einer Fahrt "
        + "ins Gebirge viel.", "hinweis");
    }
  }

  function reportLearned(result) {
    if (!result) {
      K.report("Live-Fahrt beendet.", "hinweis");
      return;
    }
    reportElevations(result.recording);
    const g = result.learned;
    if (!g && result.not_learned) {
      // The surcharge for carrier or box is not an error, but the reason
      // why this trip deliberately does not go into the vehicle factor.
      K.report("Fahrt beendet. " + result.not_learned
        + " Die Aufzeichnung bleibt erhalten.", "hinweis");
      return;
    }
    if (!g) {
      K.report("Fahrt beendet. Für die Kalibrierung war sie nicht verwertbar "
        + "– unter 30 km, oder der gemessene Verbrauch lag ausserhalb des "
        + "Plausiblen.", "hinweis");
      return;
    }
    const direction = g.after > g.earlier ? "mehr" : "weniger";
    K.report(`Gelernt: Diese Fahrt brauchte ${K.num(g.raw_factor, 2)}× so viel `
      + `wie gerechnet. Der Korrekturfaktor des Fahrzeugs geht von `
      + `${K.num(g.earlier, 3)} auf ${K.num(g.after, 3)} – künftige `
      + `Planungen rechnen also ${direction}.`, "hinweis");
  }

  /* After a reload, carry on where it left off.
   *
   * The server knows whether the session is still running - it is the truth, not
   * the browser. If it runs, the view is restored and reporting
   * continues; if it has ended, the marker is silently discarded.
   *
   * Without asking: whoever reloads by accident does not want to be asked
   * whether they want to continue - they want it to carry on. */
  /* How many attempts resuming takes before it gives up.
   *
   * The trigger used to hang on a timer of 800 ms, with the
   * comment "only when the vehicle list is up". A guessed number: on
   * a cold-started phone in a French dead spot it is too
   * short, and there was no second attempt. */
  const RESUME_ATTEMPTS = 6;

  /* Rebuild track, state-of-charge curve and consumption bars from the stored
   * measurement points.
   *
   * The state only knows the last point. Without this reloading, after a reload
   * everything started from zero: empty map, empty
   * bar chart, a curve starting from the current kilometre. The trip kept
   * running but looked like a new one - and whoever thought it lost planned
   * a new one, which then ended the running one.
   *
   * Deliberately not via `showState()`: that would reset the map
   * for each point, write tiles and trigger messages. Here only the
   * state is built up, and drawing happens once at the end. */
  async function rechargeHistory(id) {
    let stored;
    try {
      stored = await K.api(`/api/live/${id}/points`);
    } catch (failure) {
      // No reason to let resuming fail - the trip runs on
      // without the history, it just looks poorer.
      console.log("[live] Verlauf nicht nachgeladen:", failure);
      return;
    }
    const points = (stored && stored.points) || [];
    if (!points.length) return;

    track = [];
    history = [];
    consumption_track = [];
    drivenKm = 0;

    for (const p of points) {
      if (typeof p.lat === "number" && typeof p.lon === "number") {
        // `spur` holds [lon, lat] - the same order as `messort()`,
        // and `spacingKm` computes with it.
        const place = [p.lon, p.lat];
        const most_recent = track[track.length - 1];
        if (!most_recent || most_recent[0] !== place[0] || most_recent[1] !== place[1]) {
          if (most_recent) drivenKm += spacingKm(most_recent, place);
          track.push(place);
        }
      }
      if (typeof p.odometer_km === "number") {
        const net = (typeof p.discharge_kwh === "number")
          ? p.discharge_kwh - (typeof p.charged_kwh === "number"
                              ? p.charged_kwh : 0)
          : null;
        consumption_track.push({
          timestamp: K.timeMs(p.timestamp), km: p.odometer_km, net,
          // The GPS distance is already known here - unlike in live operation,
          // where it is filled in later together with the position.
          gps: drivenKm,
          soc: typeof p.soc_raw === "number" ? p.soc_raw / 2.5 : null });
      }
      if (p.soc !== null && p.soc !== undefined) {
        history.push({
          km: p.km_on_route || 0, driven_km: drivenKm, soc: p.soc,
          /* Whether a state of charge was reported or computed is not stored in
           * the database. A point with a raw value came from the car, that
           * is certainly a measurement; a value typed in by hand appears
           * here wrongly as computed. Better this way round: the error
           * claims less than it knows. */
          reported: p.soc_raw !== null && p.soc_raw !== undefined });
      }
    }
    // The same upper limit as in live operation.
    while (consumption_track.length > 20000) consumption_track.shift();
    while (track.length > 20000) track.shift();

    const last = points[points.length - 1];
    if (typeof last.odometer_km === "number") {
      // Via K.zeit: UTC from the server. Read as local time the value would be two
      // hours old, and `quietWatch` would rebuild the connection after every
      // reload of the page ("antwortet seit zwei Minuten nicht" - no answer for two minutes).
      latestRawValuesTime = K.timeMs(last.timestamp);
    }
    drawHistory();
    drawConsumption();
    if (!K.state.trip && window.joltMap) {
      window.joltMap.setRoute(track);
    }
  }

  async function resumeSession(attempt = 1) {
    const id = K.rememberedSession();
    if (!id || K.state.sessionId) return;
    let state;
    try {
      state = await K.api(`/api/live/${id}`);
    } catch (failure) {
      /* jolt may only forget a running trip if the server
       * clearly says it does not exist.
       *
       * Before, **every** error deleted the remembered number - and `api()`
       * throws "Server nicht erreichbar." (server unreachable) even when only the network is gone.
       * Whoever reloaded the page in a tunnel lost the trip for good:
       * it kept running on the server, but the phone never offered it
       * again. On 2 September exactly that happened all day -
       * a journey of 654 km fell apart into nine trips, because after every
       * reload a new one had to be planned by hand. */
      if (/nicht gefunden|404/i.test(failure.message)) {
        K.sessionRemember(null);
        return;
      }
      if (attempt < RESUME_ATTEMPTS) {
        setTimeout(() => resumeSession(attempt + 1), 4000 * attempt);
      }
      return;
    }
    if (!state || state.running === false) { K.sessionRemember(null); return; }

    K.state.sessionId = id;
    // After a reload it is unknown where the car is and whether it is
    // unlocked. So ask only once driving happens.
    drivingStateStart("steht");
    bufferFor(id);
    if (buffer.length) bufferProcess();
    /* Fetch the trip too. The live view needs it for the
     * energy profile, the reserve marker and the planned curve; without it
     * it shows only half the truth. For a recording it does not exist
     * yet - then it stays null, and the view copes with that. */
    if (!K.state.trip && state.trip_id && window.joltRoute) {
      try { await window.joltRoute.tripCharging(state.trip_id); }
      catch (failure) { /* a recording does not have any geometry yet */ }
    }
    const empty = document.getElementById("live-empty");
    const contents = document.getElementById("live-content");
    if (empty) empty.hidden = true;
    if (contents) contents.hidden = false;
    if (state.plan) { plan = state.plan; drawPlan(); }
    // Before connecting: if a new point comes in over the WebSocket straight away,
    // it should meet the reloaded history and not nothing.
    await rechargeHistory(id);
    link(id);
    positionTrace();
    showDongle();
    /* Switch to the live view, as `starten()` also does.
     *
     * Without that you stayed in the planning view after the reload:
     * the trip kept running but was not visible anywhere. Whoever did not
     * know they had to tap "Live" thought it lost and
     * planned a new one - and that then ended the running one. */
    if (window.joltApp) window.joltApp.showView("live");
    K.report("Die laufende Fahrt geht weiter – die Messpunkte von vorher "
      + "sind erhalten. Falls der Dongle mitlas, einmal neu verbinden.",
      "hinweis");
  }

  function set_up() {
    // As with the map: in the hidden section the canvas has width
    // zero, and after showing or rotating it has to be redrawn.
    window.addEventListener("resize", drawHistory);
    window.addEventListener("resize", drawConsumption);
    K.sliderCouple("extra-consumption", "extra-consumption-output");
    K.sliderCouple("jam", "jam-output");
    K.at("live-start", "click", launch);
    K.at("simulate", "click", simulate);
    K.at("live-end", "click", finish);
    K.at("soc-report", "click", reportSoc);
    K.at("dongle-on", "click", connectDongle);
    // Only when the vehicle list is up - otherwise the battery size is missing.
    setTimeout(resumeSession, 800);
    K.at("dongle-pause", "click", donglePausieren);
    setInterval(examineVoltage, VOLTAGE_TICK_MS);
    const autoCheckbox = document.getElementById("dongle-car");
    if (autoCheckbox) {
      autoCheckbox.checked = autoMode;
      autoCheckbox.addEventListener("change", () => {
        autoMode = autoCheckbox.checked;
        try { localStorage.setItem("jolt-dongle-auto", autoMode ? "1" : "0"); }
        catch (e) { /* this session only */ }
        if (autoMode) driveState = "steht";
        else reconnectDongle();
      });
    }
    // On the phone the Enter key is a shorter way than aiming at
    // a button - `enterkeyhint="send"` labels it appropriately.
    K.at("actual-soc", "keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); reportSoc(); }
    });
  }

  return { set_up, launch, finish, link, positionTrace,
           dongleUse, drawHistory,
           driving_state: () => driveState, drivingStateStart, readAllowed, connectDongle,
           examineVoltage, notificationsSetUp, handshakeSafe,
           reconnectDongle,
           setAuto: (on) => { autoMode = !!on; } };
})();
