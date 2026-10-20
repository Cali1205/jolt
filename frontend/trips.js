/* The trips view: what has been planned and driven so far.
 *
 * The purpose is not bookkeeping but comparison: the same route in January
 * and in June, once empty and once loaded - only side by side does it become
 * visible what caused the difference of two charging stops. That is why each
 * row shows the consumption next to temperature, speed and payload, and not
 * just start and destination.
 */
window.joltTrips = (function () {
  "use strict";

  const K = window.jolt;
  let charged = false;

  function date(iso) {
    // Via K.zeit: the server delivers UTC, and `new Date` on a text without
    // a zone would read it as local time - two hours off.
    const d = K.timestamp(iso);
    if (!d) return "–";
    return d.toLocaleString("de-DE", { day: "2-digit", month: "2-digit",
                                       year: "numeric", hour: "2-digit",
                                       minute: "2-digit" });
  }

  const num = (value, digits) =>
    (value === null || value === undefined) ? "" : K.num(value, digits);

  /* What makes one trip incomparable with another.
   *
   * A trip with a bike rack is no benchmark for one without, and a mere
   * draft none for a driven route. Without these marks one compares apples
   * with oranges and wonders about the consumption. */
  function brands(trip) {
    const m = [];
    if (trip.recording) m.push('<span class="marke auf">aufgez.</span>');
    else if (trip.driven) m.push('<span class="marke gut">gefahren</span>');
    else m.push('<span class="marke">Entwurf</span>');
    if (trip.air_drag_factor && trip.air_drag_factor > 1.001) {
      m.push(`<span class="marke warn">Anbau ×${K.num(trip.air_drag_factor, 2)}</span>`);
    }
    if (trip.trailer_kg) {
      m.push(`<span class="marke warn">Anhänger ${K.num(trip.trailer_kg, 0)} kg</span>`);
    }
    if (trip.speed_max_kmh) {
      m.push(`<span class="marke">max ${K.num(trip.speed_max_kmh, 0)} km/h</span>`);
    }
    return m.join(" ");
  }

  /* A table instead of a list of dot-separated sentences.
   *
   * The purpose of this view is comparison - the same route in January and
   * in June, once empty and once loaded. Comparing means reading numbers
   * one below the other, and a table is the right tool for that: same
   * column, same place, right-aligned and in equal-width digits. In a text
   * line the consumption sometimes sits in third, sometimes in fifth place -
   * depending on what else is known.
   *
   * On narrow screens the rear columns drop out (see CSS), in the order of
   * their value for comparing. What remains is date, route, kilometres and
   * consumption. */
  /* Start and destination, or only what is known.
   *
   * For a recording the destination has no name - jolt can search for
   * places, but cannot turn a coordinate into a place name the other way
   * round. An arrow into nothing ("Dienstag 14:32 → ?") looks like an
   * error; the name of the recording alone is the more honest line. */
  function distance(trip) {
    const begin = (trip.start || "").trim();
    const past = (trip.destination || "").trim();
    if (begin && past) return `${begin} → ${past}`;
    return begin || past || "ohne Namen";
  }

  /* One card per trip instead of a table with eleven columns: on the phone
   * the rear columns used to drop out anyway. The two figures that decide
   * (consumption, kilometres) are big, the rest are chips. */
  function row(trip) {
    const chip = (text) => `<span class="chip">${text}</span>`;
    // A trip that has not been evaluated yet (a recording under way) has no
    // figures: then no chip, instead of "kWh" and "°C" with nothing in front.
    const has = (value) => typeof value === "number" && Number.isFinite(value);
    const chips = [has(trip.drive_time_minutes) ? chip(K.duration(trip.drive_time_minutes)) : "",
      has(trip.kwh_total) ? chip(num(trip.kwh_total, 0) + " kWh") : "",
      has(trip.outside_temp_c) ? chip(num(trip.outside_temp_c, 0) + " °C") : "",
      trip.payload_kg ? chip(num(trip.payload_kg, 0) + " kg") : "",
      trip.speed_factor ? chip("Tempo " + Math.round(trip.speed_factor * 100) + " %") : "",
      (trip.soc_at_target === null || trip.soc_at_target === undefined)
        ? "" : chip(`${num(trip.start_soc, 0)}→${num(trip.soc_at_target, 0)} %`),
    ].join("");
    return `<div class="fahrt" data-id="${trip.id}">
      <div class="fahrt-kopf">
        <div class="fahrt-text">
          <div class="titel">${distance(trip)}</div>
          <div class="unter">${date(trip.created_at)} · ${trip.vehicle || ""} ${brands(trip)}</div>
        </div>
        <div class="fahrt-zahlen">
          <div class="stark">${has(trip.consumption_kwh_100km)
            ? num(trip.consumption_kwh_100km, 1) + "<small> kWh/100</small>" : "–"}</div>
          <div class="unter">${has(trip.distance_km) ? num(trip.distance_km, 0) + " km" : ""}</div>
        </div>
      </div>
      <div class="chips" style="margin-top:8px">${chips}</div>
      <div class="fahrt-tat">
        <button class="klein" data-open="${trip.id}">öffnen</button>
        <button class="klein" data-delete="${trip.id}" aria-label="löschen">×</button>
      </div>
    </div>`;
  }

  /* Planned and driven apart: saved plans (drafts) belong to "Planen",
   * what was driven or recorded belongs to "Fahrten". One request, two lists. */
  async function load() {
    const holder = document.getElementById("trips-list");
    const plans = document.getElementById("plans-list");
    if (!holder && !plans) return;
    if (holder) holder.innerHTML = '<p class="leer">lädt …</p>';
    loadLogbook();
    try {
      const trips = await K.api("/api/trips");
      for (const trip of trips) {
        if (trip.session_id) sessions.set(trip.id, trip.session_id);
      }
      const driven = trips.filter((t) => t.driven || t.recording);
      const drafts = trips.filter((t) => !(t.driven || t.recording));
      if (holder) {
        holder.innerHTML = driven.length ? driven.map(row).join("")
          : '<p class="leer">Noch keine Fahrt gefahren oder aufgezeichnet.</p>';
      }
      if (plans) {
        plans.innerHTML = drafts.length ? drafts.map(row).join("")
          : '<p class="leer">Noch keine Planung gespeichert.</p>';
        const count = document.getElementById("plans-count");
        if (count) count.textContent = String(drafts.length);
      }
      charged = true;
    } catch (failure) {
      if (holder) holder.innerHTML = "";
      if (plans) plans.innerHTML = "";
      K.report("Fahrten: " + failure.message, "fehler");
    }
  }

  /* ---------- Detail of a driven trip (stays in this tab) ---------- */

  const sessions = new Map();   // trip id -> session id, from the list

  function haversineKm(a, b) {
    const rad = Math.PI / 180;
    const dLat = (b.lat - a.lat) * rad, dLon = (b.lon - a.lon) * rad;
    const h = Math.sin(dLat / 2) ** 2
      + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
    return 12742 * Math.asin(Math.sqrt(h));
  }

  /* The measured points as (km, soc). `km_on_route` is only set on a planned
   * trip; a recording has none, so the distance is summed up from the track. */
  function measuredCurve(points) {
    let run = 0, last = null;
    return points.filter((p) => typeof p.soc === "number").map((p) => {
      if (typeof p.km_on_route === "number") {
        run = p.km_on_route;
      } else if (last) {
        run += haversineKm(last, p);
      }
      last = p;
      return { km: run, soc: p.soc };
    });
  }

  function drawTripProfile(trip, curve) {
    const canvas = document.getElementById("trip-profile");
    if (!canvas) return;
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    const pen = canvas.getContext("2d");
    pen.setTransform(ratio, 0, 0, ratio, 0, 0);
    pen.clearRect(0, 0, width, height);
    const planned = (trip.profile || []).filter((p) => typeof p.soc === "number");
    const maxKm = Math.max(1, ...planned.map((p) => p.km || 0),
                           ...curve.map((p) => p.km));
    const x = (km) => (km / maxKm) * (width - 8) + 4;
    const y = (soc) => height - 6 - (Math.max(0, Math.min(100, soc)) / 100) * (height - 24);
    const line = (list, color, dash) => {
      if (list.length < 2) return;
      pen.beginPath();
      list.forEach((p, i) => (i ? pen.lineTo(x(p.km), y(p.soc)) : pen.moveTo(x(p.km), y(p.soc))));
      pen.strokeStyle = color; pen.lineWidth = dash ? 1.5 : 2;
      pen.setLineDash(dash ? [4, 4] : []); pen.stroke(); pen.setLineDash([]);
    };
    line(planned, "#8a97a5", true);
    line(curve, "#ffc93c", false);
  }

  function chip(text) { return `<span class="chip">${text}</span>`; }

  async function openDetail(id) {
    const overview = document.getElementById("trips-overview");
    const detail = document.getElementById("trip-detail");
    if (!detail) return;
    try {
      const trip = await K.api("/api/trips/" + id);
      const sessionId = sessions.get(Number(id));
      let points = [];
      if (sessionId) {
        points = (await K.api(`/api/live/${sessionId}/points`)).points || [];
      }
      const curve = measuredCurve(points);

      const first = points[0], last = points[points.length - 1];
      const measuredKm = curve.length ? curve[curve.length - 1].km : null;
      const minutes = (first && last)
        ? (new Date(last.timestamp) - new Date(first.timestamp)) / 60000 : null;
      const socFrom = curve.length ? curve[0].soc : trip.start_soc;
      const socTo = curve.length ? curve[curve.length - 1].soc : trip.soc_at_target;

      // Consumption from the car's own counters, if it delivered them.
      let consumption = null;
      const withCounter = points.filter((p) => typeof p.odometer_km === "number"
                                                && typeof p.discharge_kwh === "number");
      if (withCounter.length > 1) {
        const a = withCounter[0], b = withCounter[withCounter.length - 1];
        const km = b.odometer_km - a.odometer_km, kwh = b.discharge_kwh - a.discharge_kwh;
        if (km > 1 && kwh > 0) consumption = kwh / km * 100;
      }

      document.getElementById("trip-title").textContent =
        distance({ start: trip.start.text, destination: trip.destination.text });
      document.getElementById("trip-sub").textContent =
        `${trip.vehicle.name} · ${points.length} Messpunkte`;
      document.getElementById("trip-metrics").innerHTML = [
        K.valueTile("Strecke", K.num(measuredKm !== null ? measuredKm : trip.distance_km) + " km"),
        K.valueTile("Dauer", K.duration(minutes !== null ? minutes : trip.drive_time_minutes)),
        K.valueTile("Ladestand", socTo === null || socTo === undefined
          ? "–" : `${K.num(socFrom)}→${K.num(socTo)} %`),
      ].join("");
      document.getElementById("trip-more").innerHTML = [
        K.valueTile(consumption !== null ? "Verbrauch (gemessen)" : "Verbrauch (geplant)",
          K.num(consumption !== null ? consumption : trip.consumption_kwh_100km, 1) + " kWh/100"),
        K.valueTile("Geplant", K.num(trip.distance_km) + " km · "
          + K.duration(trip.drive_time_minutes)),
        K.valueTile("Gerechnet bei", K.num(trip.weather.temp_c, 1) + " °C"),
      ].join("");
      document.getElementById("trip-chips").innerHTML = chip(
        K.num(consumption !== null ? consumption : trip.consumption_kwh_100km, 1) + "/100");

      overview.hidden = true;
      detail.hidden = false;
      drawTripProfile(trip, curve);

      // Map: the track driven, otherwise the planned route.
      const track = points.length > 1 ? points.map((p) => [p.lon, p.lat]) : trip.geometry;
      window.joltMap.drawNew();
      window.joltMap.setRoute(track);
      const end = track[track.length - 1], begin = track[0];
      window.joltMap.setMarker([
        { lat: begin[1], lon: begin[0], kind: "start", text: "Start" },
        { lat: end[1], lon: end[0], kind: "ziel", text: "Ziel" }]);
      window.joltMap.onRouteFit();
      window.scrollTo(0, 0);
    } catch (failure) {
      K.report("Fahrt öffnen: " + failure.message, "fehler");
    }
  }

  function closeDetail() {
    document.getElementById("trip-detail").hidden = true;
    document.getElementById("trips-overview").hidden = false;
    // The map goes back to what planning shows.
    const planned = K.state.trip;
    if (planned && planned.geometry) window.joltMap.setRoute(planned.geometry);
  }

  /* Bring an old trip back onto the map.
   *
   * Deliberately via joltRoute and not with its own drawing logic: it is
   * the same view as after a fresh calculation, and two ways of drawing the
   * same thing inevitably drift apart. */
  async function open_it(id) {
    try {
      // View first, then draw: a canvas in a hidden view has no size, and the
      // charge curve over the route would stay blank.
      window.joltApp.showView("plan");
      await window.joltRoute.tripCharging(Number(id));
    } catch (failure) {
      K.report("Fahrt öffnen: " + failure.message, "fehler");
    }
  }

  /* Ask before deleting.
   *
   * The cross sits in a table row, on the phone a thumb's width next to
   * "öffnen" (open). And a recorded trip cannot be restored: it is a
   * measurement that took place exactly once - unlike a planned route,
   * which can be recalculated. */
  async function remove(id, label) {
    if (!await window.joltSheet.confirm({ title: `„${label}" löschen?`,
        text: "Eine aufgezeichnete Fahrt lässt sich nicht wiederherstellen.",
        ok: "Löschen", danger: true })) {
      return;
    }
    try {
      await K.api("/api/trips/" + id, { method: "DELETE" });
      await load();
    } catch (failure) {
      K.report("Löschen: " + failure.message, "fehler");
    }
  }

  /* Start a recording: fetch position, create the trip, switch to the
   * live view. From then on it is a live trip like any other - only without
   * a plan to hold itself against. Route and energy profile are created
   * from the measurement points when it ends. */
  /* Start a recording - with dongle, if one is available.
   *
   * The order is not arbitrary: `requestDevice` may only run immediately
   * following a user gesture. Anyone who waits for GPS or an API response
   * beforehand has used up the gesture and gets a `SecurityError` - that is
   * why the dongle comes **first**, before everything else.
   *
   * If it fails, it carries on without. That is the whole point: Safari has
   * no Web Bluetooth, the dongle may not be plugged in in the car, and in
   * both cases a recording with a charge level reported by hand is better
   * than none.
   */
  // Why the last start did not work - for CarPlay, which sees no message
  // from the UI and has to display the reason itself.
  let lastStartError = "";

  /* Starting a recording, in the order it happens: connect the dongle, pick the
   * vehicle, get the position, ask the car for the charge level, create the
   * trip, show it. Each step is a function of its own below. */

  /* Try the dongle first; it is no reason to abort if it is not there.
   * `present`: connected and answering. `later`: not connected, but worth
   * knocking again, because the car is often only switched on now. */
  async function connectForStart(as_of) {
    const dongle = { present: false, later: false };
    if (!(window.joltObd && window.joltObd.obtainable())) return dongle;
    as_of("Verbinde mit dem OBD2-Dongle …");
    try {
      window.joltObd.set_up((t) => console.log("[obd]", t));
      // First without a dialog: if the dongle has been allowed once
      // before, it connects without a touch.
      await window.joltObd.attach();
      if (await window.joltLive.handshakeSafe()) dongle.present = true;
    } catch (failure) {
      // No reason to abort - only one to carry on without the dongle.
      console.log("[obd] Verbindung nicht zustande gekommen:", failure);
    }
    if (!dongle.present) {
      as_of("Ohne Dongle – der Ladestand kommt von Hand.");
      dongle.later = true;
    }
    return dongle;
  }

  /* The choice from this section, not the one from the planning view -
   * and **no** fallback to the first vehicle in the list. That was the
   * bug: it silently assigned the trip to the "Allgemeines E-Auto"
   * (generic EV), and everything was calculated with it afterwards -
   * battery size, mass, drag. Better not to record at all than for the
   * wrong car. */
  function chosenVehicleId() {
    const choice = document.getElementById("rec-vehicle");
    return choice && choice.value ? Number(choice.value) : null;
  }

  /* Which addresses and requests the dongle uses depends on the vehicle, so
   * the profile is chosen before the handshake - the handshake is what puts
   * the platform's AT setup onto the dongle. A vehicle without a platform
   * (old cached data) is a MEB. */
  function usePlatformOfChoice() {
    const id = chosenVehicleId();
    const O = window.joltObd;
    if (!id || !O || !O.usePlatform) return;
    const vehicle = (K.state.vehicles || []).find((v) => v.id === id);
    O.usePlatform(vehicle ? vehicle.platform : null);
  }

  function currentPosition() {
    return new Promise((fulfil, reject) => {
      if (!navigator.geolocation) {
        reject(new Error("Dieses Gerät liefert keinen Standort."));
        return;
      }
      navigator.geolocation.getCurrentPosition(
        (p) => fulfil(p.coords),
        // Without a start position there would be no first point of the route.
        (f) => reject(new Error("Standort: " + f.message)),
        { enableHighAccuracy: true, timeout: 10000 });
    });
  }

  /* With a dongle, pass the real start charge level right away - better than
   * the 100 % the server otherwise assumes. */
  async function dongleCharge(dongle) {
    if (!dongle.present) return null;
    try {
      const reading = await window.joltObd.readSoc();
      if (reading) return Math.round(reading.hmi * 10) / 10;
    } catch (failure) {
      /* No answer to the first query does **not** mean "no
       * dongle". A car that is still asleep does not answer - the
       * dongle is there anyway, and as soon as it drives, the values come.
       *
       * Here `withDongle = false` used to stand. With that the whole
       * recording ran without vehicle values although the dongle was
       * connected: on 5 Oct a trip (session 6) delivered only GPS for
       * three minutes until someone restarted by hand. Only if the
       * connection itself is gone does the dongle count as absent. */
      if (!window.joltObd.linked()) {
        dongle.present = false;
        dongle.later = true;
      } else {
        console.log("[obd] Startladestand ohne Antwort:", failure);
      }
    }
    return null;
  }

  /* Without a reading from the dongle, the manufacturer cloud's last report
   * will do if it is recent: the server fetches it only when it is set up
   * (docs/vw-portal.md, docs/stellantis.md), otherwise this is a quick 404.
   * Never a reason for the start to wait or fail. */
  const PORTAL_MAX_AGE_MS = 12 * 3600 * 1000;
  const PORTAL_FROM = { vw_portal: "aus dem VW-Portal", stellantis: "aus der Stellantis-Cloud" };

  async function portalCharge(vehicleId) {
    try {
      const snap = await K.api(`/api/vehicles/${vehicleId}/snapshot`, { timeout: 3000 });
      const soc = snap && snap.values && snap.values.soc_pct;
      const at = K.timestamp(snap && snap.captured_at);
      if (typeof soc !== "number" || !at
          || Date.now() - at.getTime() > PORTAL_MAX_AGE_MS) return null;
      return { soc, at, source: snap.source };
    } catch (failure) {
      return null;
    }
  }

  async function startCharge(dongle, vehicleId) {
    const soc = await dongleCharge(dongle);
    if (soc !== null) return { soc, portal: null };
    const portal = await portalCharge(vehicleId);
    return { soc: portal ? portal.soc : null, portal };
  }

  function portalNote(portal) {
    const time = portal.at.toLocaleTimeString("de-DE",
      { hour: "2-digit", minute: "2-digit" });
    const from = PORTAL_FROM[portal.source] || "aus der Hersteller-Cloud";
    return `Startladestand ${K.num(portal.soc)} % ${from} (Stand ${time}). `;
  }

  function enterLiveView(response, id) {
    K.state.sessionId = response.session_id;
    K.sessionRemember(response.session_id);
    K.state.recVehicle =
      (K.state.vehicles || []).find((f) => f.id === id) || null;
    // The new recording belongs in the list.
    K.state.tripsStale = true;
    window.joltApp.showView("live");
    document.getElementById("live-empty").hidden = true;
    document.getElementById("live-content").hidden = false;
    window.joltLive.link(response.session_id);
    // Whoever starts the recording is sitting in the car: reading is allowed
    // until the phone says the car is stationary. Without that it began in the
    // state "steht" (standing) and only read from 15 km/h - i.e. never at a
    // standstill. The live display stayed empty, and anyone who did not drive
    // off saw nothing.
    window.joltLive.drivingStateStart("faehrt");
    window.joltLive.positionTrace();
  }

  function announceStart(dongle, start, response) {
    const note = start.portal ? portalNote(start.portal) : "";
    if (response.pending) {
      K.report("Kein Netz – die Aufzeichnung läuft auf dem Gerät und wird "
        + "nachgeliefert, sobald wieder Verbindung besteht.", "warnung");
    }
    if (dongle.present) {
      window.joltLive.dongleUse();
      K.report(start.soc !== null && !start.portal
        ? "Aufzeichnung läuft, Ladestand kommt aus dem Auto."
        : "Aufzeichnung läuft. " + note + "Das Auto antwortet noch nicht – "
          + "jolt liest den Ladestand, sobald du fährst.", "hinweis");
      return;
    }
    if (dongle.later) {
      // The dongle may still come: the car is often only switched on now.
      // jolt knocks on its own instead of waiting for the button - and
      // gives up after a few attempts.
      window.joltLive.dongleUse();
      window.joltLive.reconnectDongle();
    }
    K.report("Aufzeichnung läuft. " + note + "Den Ladestand unterwegs gelegentlich "
      + "melden – ohne ihn lässt sich hinterher nichts lernen.", "hinweis");
  }

  async function startRecording() {
    lastStartError = "";
    const btn = document.getElementById("rec-start");
    const as_of = (text) => {
      const el = document.getElementById("rec-status");
      if (el) el.textContent = text;
    };
    btn.disabled = true;
    try {
      usePlatformOfChoice();
      const dongle = await connectForStart(as_of);
      const id = chosenVehicleId();
      if (!id) {
        K.report("Erst ein Fahrzeug wählen – ohne das gehört die "
                 + "Aufzeichnung niemandem.", "fehler");
        lastStartError = "Kein Fahrzeug gewählt – in jolt einmal auswählen.";
        return false;
      }
      as_of("Standort holen …");
      const city = await currentPosition();
      const start = await startCharge(dongle, id);

      as_of("Fahrt anlegen …");
      // Without network the recording starts locally and is created on the
      // server later (`pending.js`) - the start must not depend on reception.
      const response = await window.joltPending.start(K.api, {
        vehicle_id: id, lat: city.latitude, lon: city.longitude,
        soc: start.soc, name: document.getElementById("rec-name").value });
      enterLiveView(response, id);
      if (window.joltHaptic) window.joltHaptic.good();
      announceStart(dongle, start, response);
      // The last recorded vehicle applies again next time -
      // also for the start from CarPlay, where one cannot choose any.
      try { localStorage.setItem("jolt-aufz-fahrzeug", String(id)); } catch (e) { /* without memory */ }
      reportVehicle();
      as_of("");
    } catch (failure) {
      K.report("Aufzeichnung: " + failure.message, "fehler");
      lastStartError = failure.message || "Start fehlgeschlagen.";
      as_of("");
    } finally {
      btn.disabled = false;
    }
    return !!K.state.sessionId;
  }

  /* ---------- CarPlay ----------
   *
   * The CarPlay list (plugins/jolt-display) can start and stop the recording.
   * It calls in here via an event of the plugin and gets the result back -
   * CarPlay displays anything that went wrong itself, because nobody in the
   * car sees this UI's messages.
   *
   * It starts with the **last used vehicle**: in the car none can be chosen,
   * and the start on the phone uses the same memory. */
  function carplayPlugin() {
    const shell = window.joltBlePlugin;
    if (!shell || !shell.JoltDisplay || !shell.Capacitor
        || !shell.Capacitor.isNativePlatform()) return null;
    return shell.JoltDisplay;
  }

  function reportVehicle() {
    const p = carplayPlugin();
    if (!p) return;
    const choice = document.getElementById("rec-vehicle");
    const option = choice && choice.selectedOptions && choice.selectedOptions[0];
    const name = option && choice.value ? String(option.textContent || "").trim() : "";
    try {
      const response = p.ready({ vehicle: name });
      if (response && response.catch) response.catch(() => {});
    } catch (e) { /* no plugin, no CarPlay */ }
  }

  /* ---------- Starting on its own ----------
   *
   * Triggers: CarPlay connects, or the Shortcut for the car's Wi-Fi opens
   * `jolt://auto` (both only with ignition on). The trigger alone does not
   * start anything: the dongle has to be reachable and the 12 V voltage has
   * to be up - the DC/DC converter only runs when the car is on or charging.
   * `ATRV` is measured by the dongle's own chip and sends nothing onto the
   * bus, so waiting for it does not wake the car. The converter needs a few
   * seconds after switching on, hence a few attempts. */
  const AUTO_KEY = "jolt-auto-aufzeichnung";
  const AUTO_VOLT = 13.0;
  const AUTO_WAIT_MS = 180000;
  const AUTO_TICK_MS = 5000;
  let autoBusy = false;

  function autoEnabled() {
    try { return localStorage.getItem(AUTO_KEY) !== "0"; }
    catch (e) { return true; }
  }

  async function autoStart() {
    if (!autoEnabled() || autoBusy || K.state.sessionId) return false;
    const O = window.joltObd;
    if (!O || !O.obtainable() || typeof O.voltage !== "function") return false;
    autoBusy = true;
    try {
      const deadline = Date.now() + AUTO_WAIT_MS;
      let volt = null;
      while (Date.now() < deadline && !K.state.sessionId) {
        try {
          O.set_up((t) => console.log("[obd]", t));
          await O.attach();
          if (O.linked()) {
            volt = await O.voltage();
            if (typeof volt === "number" && volt >= AUTO_VOLT) break;
          }
        } catch (failure) {
          console.log("[auto] Dongle noch nicht da:", failure);
        }
        volt = null;
        await new Promise((r) => setTimeout(r, AUTO_TICK_MS));
      }
      if (K.state.sessionId) return true;
      if (volt === null) {
        // No dongle or the voltage stayed low: not a drive, nothing to record.
        try { O.detach(); } catch (e) { /* already disconnected */ }
        return false;
      }
      K.report("Auto erkannt (" + volt.toFixed(1) + " V) – jolt startet die "
               + "Aufzeichnung.", "hinweis");
      return await startRecording();
    } finally {
      autoBusy = false;
    }
  }

  async function carplayAction(action) {
    if (action === "auto") return autoStart();
    const p = carplayPlugin();
    let ok = false, text = "";
    try {
      if (action === "starten") {
        if (K.state.sessionId) {
          ok = true; text = "Die Aufzeichnung läuft schon.";
        } else if (document.getElementById("rec-start")
                   && document.getElementById("rec-start").disabled) {
          ok = false; text = "Ein Start läuft gerade.";
        } else {
          ok = await startRecording();
          text = ok ? "" : (lastStartError || "Der Start hat nicht geklappt.");
        }
      } else if (action === "beenden") {
        if (!K.state.sessionId) {
          ok = true; text = "Es läuft keine Aufzeichnung.";
        } else {
          await window.joltLive.finish();
          ok = !K.state.sessionId;
          text = ok ? "" : "Das Beenden hat nicht geklappt.";
        }
      } else {
        return;
      }
    } catch (failure) {
      ok = false;
      text = (failure && failure.message) || "Unbekannter Fehler.";
    }
    if (p) {
      try {
        const response = p.actionResult({ action, ok, text });
        if (response && response.catch) response.catch(() => {});
      } catch (e) { /* CarPlay then simply shows nothing */ }
    }
    return ok;
  }

  function carplayConnect() {
    const p = carplayPlugin();
    if (!p || typeof p.addListener !== "function") return;
    try {
      const h = p.addListener("carplayAction", (e) => carplayAction(e && e.action));
      if (h && h.catch) h.catch(() => {});
    } catch (e) { /* no plugin, no CarPlay */ }
    reportVehicle();
  }

  /* On opening, say what this browser can do - before someone taps and
   * wonders why no device dialog appears. */
  function dongleHint() {
    const el = document.getElementById("rec-dongle-hint");
    if (!el) return;
    el.innerHTML = (window.joltObd && window.joltObd.obtainable())
      ? "Dieser Browser kann Bluetooth – beim Starten wird versucht, den "
        + "OBD2-Dongle zu verbinden. Klappt es nicht, läuft die Aufzeichnung "
        + "trotzdem, dann mit dem Ladestand von Hand."
      : "Dieser Browser kann kein Bluetooth, der Ladestand kommt also von "
        + "Hand. Mit Dongle: dieselbe Adresse in <strong>Bluefy</strong> "
        + "öffnen, dann geht es automatisch.";
  }

  /* ---------- Logbook: drives and charges from VW data and jolt ---------- */

  const SOURCE_NAMES = { vw_history: "VW", vw_portal: "VW", stellantis: "Stellantis", jolt: "jolt" };

  function sourceBrand(sources) {
    const names = [...new Set((sources || []).map((s) => SOURCE_NAMES[s] || s))];
    return `<span class="marke${names.includes("jolt") ? " auf" : ""}">${names.join(" + ")}</span>`;
  }

  /* "Mo., 16.10.2026 · 15:44–15:55"; a charge over midnight gets the end
   * day as well, otherwise 22:00–03:09 reads like a five-hour gap backwards. */
  function period(item) {
    const a = K.timestamp(item.started_at), b = K.timestamp(item.ended_at);
    if (!a || !b) return "–";
    const day = (d) => d.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit",
                                                        month: "2-digit", year: "numeric" });
    const time = (d) => d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
    const end = day(a) === day(b) ? time(b)
      : b.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" }) + " " + time(b);
    return `${day(a)} · ${time(a)}–${end}`;
  }

  function minutes(item) {
    return (K.timestamp(item.ended_at) - K.timestamp(item.started_at)) / 60000;
  }

  const has = (value) => typeof value === "number" && Number.isFinite(value);

  function socChip(item) {
    return has(item.soc_start) && has(item.soc_end)
      ? chip(`${num(item.soc_start, 0)}→${num(item.soc_end, 0)} %`) : "";
  }

  function logbookDrive(d) {
    const chips = [chip(K.duration(minutes(d))), socChip(d),
      has(d.energy_used_kwh) ? chip(num(d.energy_used_kwh, 1) + " kWh") : "",
      has(d.odo_end) ? chip("km " + num(d.odo_end, 0)) : ""].join("");
    // A map only for what jolt recorded: the VW data has no position.
    const open = d.trip_id
      ? `<div class="fahrt-tat"><button class="klein" data-trip="${d.trip_id}"
           data-session="${d.live_session_id}">Aufzeichnung</button></div>` : "";
    return `<div class="fahrt">
      <div class="fahrt-kopf">
        <div class="fahrt-text">
          <div class="titel">${period(d)}</div>
          <div class="unter">${sourceBrand(d.sources)}</div>
        </div>
        <div class="fahrt-zahlen">
          <div class="stark">${num(d.distance_km, 0)}<small> km</small></div>
          <div class="unter">${has(d.kwh_per_100km) ? num(d.kwh_per_100km, 1) + " kWh/100" : ""}</div>
        </div>
      </div>
      <div class="chips" style="margin-top:8px">${chips}</div>${open}
    </div>`;
  }

  function logbookCharge(c) {
    const chips = [chip(K.duration(minutes(c))), socChip(c),
      has(c.avg_power_kw) ? chip("Ø " + num(c.avg_power_kw, 1) + " kW") : "",
      has(c.odometer_km) ? chip("km " + num(c.odometer_km, 0)) : ""].join("");
    const kind = c.charge_type ? `<span class="marke${c.charge_type === "DC" ? " warn" : ""}">${c.charge_type}</span>` : "";
    return `<div class="fahrt">
      <div class="fahrt-kopf">
        <div class="fahrt-text">
          <div class="titel">${period(c)}</div>
          <div class="unter">${kind} ${sourceBrand(c.sources)}</div>
        </div>
        <div class="fahrt-zahlen">
          <div class="stark">${num(c.energy_kwh, 1)}<small> kWh</small></div>
          <div class="unter">${has(c.max_power_kw) ? `max ${num(c.max_power_kw, 0)} kW` : "aus dem Ladestand"}</div>
        </div>
      </div>
      <div class="chips" style="margin-top:8px">${chips}</div>
    </div>`;
  }

  const LOGBOOK = {
    drives: { row: logbookDrive, summary(t) {
      const avg = t.distance_km_with_energy > 0
        ? chip(num(t.energy_used_kwh / t.distance_km_with_energy * 100, 1) + " kWh/100") : "";
      return chip(String(t.count)) + chip(num(t.distance_km, 0) + " km") + avg;
    } },
    charges: { row: logbookCharge, summary(t) {
      const kinds = ["AC", "DC"].filter((k) => t.by_type[k])
        .map((k) => chip(`${k} ${t.by_type[k].count}`)).join("");
      return chip(String(t.count)) + chip(num(t.energy_kwh, 0) + " kWh") + kinds;
    } },
  };

  /* The choices above each list. Every option is the query it adds; a period
   * is computed when asked, so "letzte 30 Tage" moves with the calendar. */
  function startOfYear(offset) {
    return new Date(new Date().getFullYear() + offset, 0, 1).toISOString();
  }
  const PERIOD = { label: "Zeitraum", options: [
    ["alle", () => ({})],
    ["letzte 30 Tage", () => ({ since: new Date(Date.now() - 30 * 864e5).toISOString() })],
    ["dieses Jahr", () => ({ since: startOfYear(0) })],
    ["letztes Jahr", () => ({ since: startOfYear(-1), until: startOfYear(0) })],
  ] };
  const LOGBOOK_FILTERS = {
    drives: {
      length: { label: "Länge", options: [
        ["alle", () => ({})],
        ["unter 10 km", () => ({ max_km: 10 })],
        ["10–50 km", () => ({ min_km: 10, max_km: 50 })],
        ["50–150 km", () => ({ min_km: 50, max_km: 150 })],
        ["über 150 km", () => ({ min_km: 150 })],
      ] },
      period: PERIOD,
      recorded: { label: "Karte", options: [
        ["alle", () => ({})],
        ["mit Aufzeichnung", () => ({ recorded: true })],
        ["nur VW-Daten", () => ({ recorded: false })],
      ] },
    },
    charges: {
      type: { label: "Art", options: [
        ["alle", () => ({})],
        ["AC", () => ({ type: "AC" })],
        ["DC", () => ({ type: "DC" })],
      ] },
      size: { label: "Menge", options: [
        ["alle", () => ({})],
        ["ab 1 kWh", () => ({ min_kwh: 1 })],
        ["ab 10 kWh", () => ({ min_kwh: 10 })],
        ["ab 30 kWh", () => ({ min_kwh: 30 })],
      ] },
      period: PERIOD,
    },
  };
  const FILTER_KEY = "jolt-fahrtenbuch-filter";

  function filterChoice() {
    try { return JSON.parse(localStorage.getItem(FILTER_KEY)) || {}; }
    catch (e) { return {}; }
  }

  function filterQuery(kind) {
    const chosen = filterChoice()[kind] || {};
    const params = new URLSearchParams();
    for (const [name, filter] of Object.entries(LOGBOOK_FILTERS[kind])) {
      const option = filter.options[chosen[name] || 0] || filter.options[0];
      for (const [key, value] of Object.entries(option[1]())) params.set(key, value);
    }
    const text = params.toString();
    return text ? "&" + text : "";
  }

  function renderFilters(kind) {
    const box = document.getElementById(`logbook-${kind}-filter`);
    if (!box || box.dataset.ready) return;
    box.dataset.ready = "1";
    const chosen = filterChoice()[kind] || {};
    box.innerHTML = Object.entries(LOGBOOK_FILTERS[kind]).map(([name, filter]) =>
      `<label>${filter.label}<select data-filter="${kind}" data-name="${name}">`
      + filter.options.map(([text], i) =>
        `<option value="${i}"${i === (chosen[name] || 0) ? " selected" : ""}>${text}</option>`).join("")
      + `</select></label>`).join("");
    box.addEventListener("change", (event) => {
      const select = event.target.closest("[data-filter]");
      if (!select) return;
      const all = filterChoice();
      all[kind] = { ...(all[kind] || {}), [select.dataset.name]: Number(select.value) };
      try { localStorage.setItem(FILTER_KEY, JSON.stringify(all)); } catch (e) { /* not remembered */ }
      loadLogbookPart(kind).catch((failure) =>
        K.report("Fahrtenbuch: " + failure.message, "fehler"));
    });
  }

  function logbookVehicle() {
    const vehicles = K.state.vehicles || [];
    return chosenVehicleId() || (vehicles.length === 1 ? vehicles[0].id : null);
  }

  async function loadLogbookPart(kind, before) {
    const vehicleId = logbookVehicle();
    const list = document.getElementById("logbook-" + kind);
    if (!vehicleId || !list) return 0;
    renderFilters(kind);
    const query = (before ? "&before=" + encodeURIComponent(before) : "") + filterQuery(kind);
    const page = await K.api(`/api/vehicles/${vehicleId}/${kind}?limit=30${query}`);
    const more = page.next_before
      ? `<button class="klein" data-more="${kind}" data-before="${page.next_before}">ältere</button>` : "";
    const html = (page.items.length || before ? page.items.map(LOGBOOK[kind].row).join("")
      : '<p class="leer">Nichts für diese Auswahl.</p>') + more;
    // The first page replaces, so two loads that overlap (switching to the
    // tab while the vehicle choice changes) do not list everything twice.
    if (!before) {
      list.innerHTML = html;
    } else {
      const old = list.querySelector("[data-more]");
      if (old) old.remove();
      list.insertAdjacentHTML("beforeend", html);
    }
    document.getElementById(`logbook-${kind}-sum`).innerHTML = LOGBOOK[kind].summary(page.totals);
    return page.totals.count;
  }

  async function loadLogbook() {
    const block = document.getElementById("logbook");
    if (!block) return;
    try {
      const counts = await Promise.all(Object.keys(LOGBOOK).map((k) => loadLogbookPart(k)));
      // Hidden only when there is nothing at all, not when a filter
      // leaves a list empty: the filters must stay reachable.
      block.hidden = !counts.some((n) => n > 0) && !Object.values(filterChoice())
        .some((chosen) => Object.values(chosen || {}).some(Boolean));
    } catch (failure) {
      block.hidden = true;
      K.report("Fahrtenbuch: " + failure.message, "fehler");
    }
  }

  function set_up() {
    K.at("rec-start", "click", startRecording);
    K.at("rec-vehicle", "change", usePlatformOfChoice);
    K.at("rec-vehicle", "change", loadLogbook);
    K.at("logbook", "click", (event) => {
      const more = event.target.closest("[data-more]");
      if (more) {
        loadLogbookPart(more.dataset.more, more.dataset.before)
          .catch((failure) => K.report("Fahrtenbuch: " + failure.message, "fehler"));
        return;
      }
      const open = event.target.closest("[data-trip]");
      if (open) {
        sessions.set(Number(open.dataset.trip), Number(open.dataset.session));
        openDetail(open.dataset.trip);
      }
    });
    const autoSwitch = document.getElementById("rec-auto");
    if (autoSwitch) {
      autoSwitch.checked = autoEnabled();
      autoSwitch.addEventListener("change", () => {
        try { localStorage.setItem(AUTO_KEY, autoSwitch.checked ? "1" : "0"); }
        catch (e) { /* this session only */ }
      });
    }
    K.at("trip-back", "click", closeDetail);
    carplayConnect();
    dongleHint();
    // One listener on the holder instead of one per row: the list is rebuilt
    // after every deletion, individually bound listeners would then be dead.
    for (const holder of [document.getElementById("trips-list"),
                          document.getElementById("plans-list")]) {
      if (!holder) continue;
      holder.addEventListener("click", (event) => {
        const uphill = event.target.closest("[data-open]");
        if (uphill) {
          if (holder.id === "trips-list") openDetail(uphill.dataset.open);
          else open_it(uphill.dataset.open);
          return;
        }
        const path = event.target.closest("[data-delete]");
        if (path) {
          const row = path.closest(".fahrt");
          const title = row ? row.querySelector(".titel") : null;
          remove(path.dataset.delete,
                 title ? title.textContent.trim() : "Diese Fahrt");
        }
      });
    }
  }

  // Load on switching to the view, not at startup: whoever never taps the
  // tab should not pay for the list either.
  //
  // It is only cached as long as nothing has changed. `geladen` (loaded) was
  // originally never reset anywhere - the list was frozen after the first
  // opening, and a freshly planned or just ended trip only appeared after
  // reloading the page. Whoever creates a trip now sets
  // `K.zustand.tripsStale` (trips stale); here the mark is read and
  // cleared again.
  function show() {
    if (!charged || K.state.tripsStale) {
      K.state.tripsStale = false;
      load();
    }
  }

  return { set_up, load, show, startRecording,
           carplayAction, reportVehicle, loadLogbook };
})();
