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
  let quietBox = null;      // the message about the silent car, while it applies
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
      drive.reset("faehrt");
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
      window.joltNotify.subscribe();
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
    if (isLocalSession(sessionId)) {
      // Recording without network: nothing to connect to yet. The points are
      // collected and go out as soon as the server can be reached.
      showConnection("offline – wird nachgeliefert", "#e8804f");
      return;
    }
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

  /* What the view does with every state message, in the order it happens. The
   * steps are functions of their own below; this one only lines them up. */
  function showState(z) {
    reportToDisplay(z);
    const trip = K.state.trip;
    const reserve = trip ? trip.vehicle.reserve_soc : 10;
    adoptPlan(z);

    // The last known state of charge as a suggestion for the next report: at
    // a charge point the new value is higher, on the road lower - in both
    // cases the last value is a shorter way than a fixed number.
    socFieldPrefill(z.actual_soc);

    /* The answer first, and the answer is not the state of charge.
     *
     * The state of charge is an input - the question in the car is "will
     * it be enough?", and the arrival value answers it. As long as a
     * charging plan exists, the next stop is the nearer and therefore more
     * urgent answer; without a plan the destination counts. */
    showResponse(z, reserve);
    drawValues(z);

    const place = followTrack(z);
    drawHistory();
    drawConsumption();
    autoRow(z);
    showDongle();
    drawBar(z, reserve);
    drawMap(z, trip, place);
  }

  /* The display model for everything outside this UI (CarPlay,
   * widget): a few numbers, throttled. An error there must never drag
   * the display here down with it. */
  function reportToDisplay(z) {
    try {
      if (window.joltDisplay) {
        window.joltDisplay.report(z, { track: consumption_track, vals: valuesAsOf,
                                       aux: aux_load, plan: z.plan || plan });
      }
    }
    catch (e) { console.log("[anzeige]", e && e.message); }
  }

  /* A newly computed plan arrives with the state. Only if it really
   * differs is it pointed out - a plan that announces itself
   * every thirty seconds is no plan. */
  function adoptPlan(z) {
    if (!z.plan) return;
    plan = z.plan;
    drawPlan();
    if (z.plan_changed) reportChange(z.change);
  }

  /* Below the answer only what changes a decision. State of charge and
   * deviation deliberately sit here and not above: they are evidence, not
   * the answer - you read them when you want to follow up the big number. */
  function drawValues(z) {
    const deviationVariety = z.deviation_pp === null ? ""
      : (z.deviation_pp <= -5 ? "schlecht"
        : (z.deviation_pp <= -2 ? "warnung" : "gut"));
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
  }

  /* Grow the driven track, the distance and the history by this message, and
   * return where the car is ([lon, lat] or null).
   *
   * Every measurement point arrives here **twice**: once as the reply to
   * our own POST, once via the WebSocket, which mirrors it back to all
   * viewers - and our own browser is one of them. Without this
   * check every point would appear twice in the history and in the track;
   * over a long distance that would be a thousand entries too many. */
  function followTrack(z) {
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
    return place;
  }

  function drawBar(z, reserve) {
    const bar = document.getElementById("live-bars");
    bar.style.width = Math.max(0, Math.min(100, z.actual_soc)) + "%";
    bar.style.background = z.actual_soc <= reserve ? "#e2596a"
      : (z.actual_soc <= reserve + 10 ? "#e8804f" : "#57c98a");

    const hint = document.getElementById("live-hint");
    hint.textContent = z.reason || "im Plan";
    hint.style.color = z.replanning_required ? "#e8804f" : "";
  }

  /* The reserve marker moves along: that is the actual message of the
   * live function - not "you are using more", but "it is now only enough
   * as far as there".
   *
   * Serve the map **without** a planned route too. This used to be
   * `if (fahrt && ...)`, and `K.zustand.fahrt` is only set if a route was
   * computed beforehand. A recording has none - so the whole block was
   * skipped and the map stayed empty although the position had long been
   * coming in. The own position has nothing to do with whether a route
   * exists. */
  function drawMap(z, trip, place) {
    if (!window.joltMap) return;
    const here = place || [z_lon(z), z_lat(z)];
    const marker = [{ lat: here[1], lon: here[0], kind: "auto", text: "hier" }];
    // In a recording the driven track is what there is to see:
    // it grows along and shows that data is really being written.
    // The map follows it by itself (map.js) until someone touches it.
    if (!trip) window.joltMap.setRoute(track);
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

  /* An empty black box looks broken. The curve appears with the first
   * measurements, or at once if there is a planned route to show. */
  function drawHistory() {
    const canvas = document.getElementById("live-history");
    const foot = document.getElementById("live-history-foot");
    const trip = K.state.trip;
    const planned = !!(trip && trip.profile && trip.profile.length > 1);
    const show = history.length > 1 || planned;
    if (canvas) canvas.hidden = !show;
    if (foot) foot.hidden = !show;
    if (show) window.joltHistoryChart.draw(canvas, trip, history, plan);
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
        K.esc(plan.reason || "Kein Ladeplan möglich.")}</li>`;
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
          <div class="titel">${i + 1}. ${K.esc(s.name || s.operator
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
    window.joltNotify.show(text || "Der Ladeplan hat sich geändert.");
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
    drive.examine(coords, timeMs);
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
    window.joltAwake.hold();
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
    window.joltAwake.hold();
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
    window.joltAwake.release();
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
    window.joltAwake.hold();
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
  const VOLTAGE_TICK_MS = 2000;
  // No dongle in range (bicycle, bus): after this many failed attempts
  // it stops until the vehicle has stopped again.
  const AGAIN_ATTEMPTS_MAX = 8;
  const AGAIN_ATTEMPTS_DRIVING = 40;

  const drive = window.joltDriveState.create({
    sessionActive: () => !!K.state.sessionId,
    onPark(reason) {
      if (dongle && window.joltObd) {
        try { window.joltObd.detach(); } catch (e) { /* already disconnected */ }
        K.report(reason + " – jolt fragt das Auto nicht mehr, bis du losfährst. "
          + "So löst ein abgeschlossenes Auto keinen Alarm aus.", "hinweis");
      }
      showDongle();
    },
    onDrive(wasParked) {
      if (wasParked && dongle) {
        K.report("Fahrt erkannt – jolt verbindet den Dongle wieder.", "hinweis");
      }
      reconnectDongle();
      showDongle();
    },
  });

  function readAllowed() {
    if (donglePause) return false;
    // While listening in (settings) nothing may be queried.
    if (window.joltObd && window.joltObd.listens && window.joltObd.listens()) return false;
    return drive.motionAllows();
  }

  let voltageRunning = false;

  async function examineVoltage() {
    if (voltageRunning || !K.state.sessionId || !dongle || !drive.auto()
        || drive.state() === "geparkt" || donglePause || !window.joltObd
        || !window.joltObd.linked() || !window.joltObd.voltage) return;
    voltageRunning = true;
    try {
      const v = await window.joltObd.voltage();
      if (typeof v !== "number" || !(v > 5 && v < 30)) return;
      drive.voltageSample(v);
    } finally {
      voltageRunning = false;
    }
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
      const bound = drive.driving() ? AGAIN_ATTEMPTS_DRIVING : AGAIN_ATTEMPTS_MAX;
      if (drive.auto() && ++attempts > bound) {
        drive.giveUp();
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
      drive.manualConnect();
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

  /* Heating and air conditioning in kW, derived: the auxiliary consumers
   * minus the 12 V network (DC/DC converter).
   *
   * There is no direct reading of the cabin heater on this car. DID 221620,
   * once taken for "heating current", is the *battery* heater and sat at
   * 0 A on every drive while the auxiliary consumers followed the cold
   * (0.9 kW at 13 degrees, 2.6 kW at 6). What remains of `aux_load_kw` after
   * the DC/DC power is heater and A/C compressor - the two consumers one can
   * influence. The DC/DC current is read only every tenth round, so its last
   * value counts for five minutes. An estimate, hence the "≈". */
  function climateKw(raw) {
    const fresh = (name, ms) => {
      if (typeof raw[name] === "number") return raw[name];
      const last = valuesAsOf[name];
      return last && typeof last.val === "number"
        && Date.now() - last.timestamp < ms ? last.val : null;
    };
    const aux = typeof raw.aux_load_kw === "number" ? raw.aux_load_kw : null;
    const current = fresh("dcdc_current_a", 300000);
    const volt = fresh("batt_v", 300000);
    if (aux === null || current === null || volt === null) return null;
    return Math.max(0, aux - current * volt / 1000);
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
      const climate = climateKw(raw);
      if (climate !== null) {
        n += `, davon ≈<b>${K.num(climate, 1)}</b> Heizung/Klima`;
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
        quietBox = K.report("Seit drei Minuten kommt nichts mehr aus dem Auto. jolt "
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
    // The power is deliberately not included - see joltConsumption.sections().
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
        soc: socBms(raw.soc_raw) });
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
    const car = vehicleOnRoad();
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

  function vehicleOnRoad() {
    return K.state.recVehicle || (K.state.trip && K.state.trip.vehicle) || {};
  }

  function drawConsumption() {
    window.joltConsumption.draw(
      document.getElementById("live-consumption"),
      document.getElementById("live-consumption-foot"),
      consumption_track, vehicleOnRoad());
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

  /* The charge level the battery itself reports, for the chart. What the raw
   * value means depends on the platform, so the conversion is the profile's. */
  function socBms(raw) {
    const soc = window.joltObd.socFromRaw(raw);
    return soc ? soc.bms : null;
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
        const volt = drive.freshVoltage();
        if (volt !== null) raw.batt_v = volt;
        if (typeof raw.elevation_m !== "number" && typeof coords.altitude === "number") {
          raw.elevation_m = Math.round(coords.altitude);
        }
        latestRawValues = raw;
        latestRawValuesTime = Date.now();
        quietReported = false;
        // The car answers again: the message is obsolete and goes away
        // instead of waiting for the next one to pile up on top of it.
        if (quietBox) { quietBox.remove(); quietBox = null; }
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
    const freshVolt = drive.freshVoltage();
    if (!payload.raw_values && freshVolt !== null) {
      payload.raw_values = { batt_v: freshVolt };
    }
    queue.append(payload);
    queue.process();
  }

  /* ---------- Buffering measurement points ---------- */

  // The queue itself lives in buffer.js.
  const queue = window.joltBuffer.create({
    sessionId: () => K.state.sessionId,
    api: (...args) => K.api(...args),
    report: (...args) => K.report(...args),
    isLocal: (id) => isLocalSession(id),
    materialize: () => materializeCurrent(),
    onState: (state) => showState(state),
  });

  function isLocalSession(id) {
    return !!(window.joltPending && window.joltPending.isLocal(id));
  }

  // Not more often than every 15 s: with every point a request that times out
  // would otherwise run back to back.
  const MATERIALIZE_EVERY_MS = 15000;
  let materializeTried = 0;

  /* A recording that was started without network becomes a real one as soon
   * as the server can be reached. True when the session now has a real id. */
  async function materializeCurrent(force) {
    const local = K.state.sessionId;
    if (!isLocalSession(local)) return true;
    if (!force && Date.now() - materializeTried < MATERIALIZE_EVERY_MS) return false;
    materializeTried = Date.now();
    let result;
    try { result = await window.joltPending.materialize(K.api); }
    catch (failure) {
      K.report("Aufzeichnung: " + failure.message, "fehler");
      return false;
    }
    if (!result) return false;
    const real = result.session_id;
    window.joltPending.renameBuffer(local, real);
    queue.rename(local, real);
    K.state.sessionId = real;
    K.sessionRemember(real);
    window.joltPending.release();
    K.state.tripsStale = true;
    K.report("Verbindung da – die Aufzeichnung läuft jetzt auf dem Server.",
             "hinweis");
    link(real);
    return true;
  }


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
      if (isLocalSession(K.state.sessionId)) {
        queue.append({ lat: place.lat, lon: place.lon, soc: soc,
                       speed_kmh: place.speed_kmh,
                       timestamp: new Date().toISOString() });
        queue.save();
        queue.process();
        K.report("Ladestand gemerkt – wird übertragen, sobald es Netz gibt.",
                 "hinweis");
        return;
      }
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
    queue.forSession(K.state.sessionId);
    if (queue.length() || isLocalSession(K.state.sessionId)) {
      if (isLocalSession(K.state.sessionId)) await materializeCurrent(true);
      if (queue.length()) await queue.process();
    }
    // Still without a real session: the end and the remaining points are
    // carried out later by `joltPending.watch`.
    const endLater = isLocalSession(K.state.sessionId);
    if (endLater) {
      window.joltPending.requestEnd();
      queue.save();
      K.report("Kein Netz – die Fahrt wird beendet und übertragen, sobald "
        + "wieder Verbindung besteht. Die Daten sind auf dem Gerät gesichert.",
        "warnung");
    } else if (queue.length()) {
      K.report(queue.length() + " Messpunkte konnten nicht mehr übertragen "
        + "werden - kein Netz.", "warnung");
    }
    let result = null;
    if (!endLater) {
      try {
        result = await K.api(`/api/live/${K.state.sessionId}/end`,
                               { method: "POST" });
      } catch (failure) { /* an already ended trip is not a problem */ }
    }
    // The trips view has cached the list; a trip that has just
    // ended belongs in it.
    K.state.tripsStale = true;
    if (window.joltHaptic) window.joltHaptic.good();
    positionGiveUp();
    queue.end(endLater);
    dongle = false;
    track = [];
    drivenKm = 0;
    history = [];
    latestRawValues = null;
    latestRawValuesTime = 0;
    quietReported = false;
    if (quietBox) { quietBox.remove(); quietBox = null; }
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
    drive.reset("steht");
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
          soc: socBms(p.soc_raw) });
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

  /* After a reload: the recording that is still waiting for the network. */
  function resumeLocal(id) {
    const rec = window.joltPending.load();
    if (!rec || rec.id !== id || rec.end_requested) {
      K.sessionRemember(null);
      return;
    }
    K.state.sessionId = id;
    const wanted = rec.body && rec.body.vehicle_id;
    K.state.recVehicle = (K.state.vehicles || []).find((f) => f.id === wanted)
      || K.state.recVehicle;
    drive.reset("steht");
    queue.forSession(id);
    if (queue.length()) queue.process();
    const empty = document.getElementById("live-empty");
    const contents = document.getElementById("live-content");
    if (empty) empty.hidden = true;
    if (contents) contents.hidden = false;
    link(id);
    positionTrace();
    showDongle();
    if (window.joltApp) window.joltApp.showView("live");
    K.report("Die Aufzeichnung läuft weiter – sie wartet noch auf Netz und "
      + "wird nachgeliefert.", "hinweis");
  }

  async function resumeSession(attempt = 1) {
    const id = K.rememberedSession();
    if (!id || K.state.sessionId) return;
    if (isLocalSession(id)) { resumeLocal(id); return; }
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
    drive.reset("steht");
    queue.forSession(id);
    if (queue.length()) queue.process();
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
    // A tap by mistake must not end a recording that cannot be resumed.
    K.at("live-end", "click", async () => {
      if (!K.state.sessionId) return;
      if (await window.joltSheet.confirm({ title: "Fahrt beenden?",
          text: "Die Aufzeichnung wird abgeschlossen und ausgewertet.", ok: "Beenden", danger: true })) {
        finish();
      }
    });
    K.at("soc-report", "click", reportSoc);
    K.at("dongle-on", "click", connectDongle);
    // Only when the vehicle list is up - otherwise the battery size is missing.
    setTimeout(resumeSession, 800);
    // A recording that was ended without network is delivered here.
    if (window.joltPending) {
      window.joltPending.watch(K.api, () => {
        K.state.tripsStale = true;
        K.report("Die Offline-Aufzeichnung ist übertragen und beendet.", "hinweis");
      });
    }
    K.at("dongle-pause", "click", donglePausieren);
    setInterval(examineVoltage, VOLTAGE_TICK_MS);
    const autoCheckbox = document.getElementById("dongle-car");
    if (autoCheckbox) {
      autoCheckbox.checked = drive.auto();
      autoCheckbox.addEventListener("change", () => {
        drive.chooseAuto(autoCheckbox.checked);
        if (!drive.auto()) reconnectDongle();
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
           driving_state: drive.state, drivingStateStart: drive.reset, readAllowed, connectDongle,
           examineVoltage, handshakeSafe,
           reconnectDongle,
           setAuto: drive.setAuto };
})();
