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

  function row(trip) {
    const soc = (trip.soc_at_target === null || trip.soc_at_target === undefined)
      ? "" : `${num(trip.start_soc, 0)}→${num(trip.soc_at_target, 0)} %`;
    return `<tr data-id="${trip.id}">
      <td class="datum">${date(trip.created_at)}</td>
      <td class="strecke">
        <div class="titel">${distance(trip)}</div>
        <div class="unter">${trip.vehicle || ""} ${brands(trip)}</div>
      </td>
      <td class="num">${num(trip.distance_km, 0)}</td>
      <td class="num weg-eng">${K.duration(trip.drive_time_minutes)}</td>
      <td class="num stark">${num(trip.consumption_kwh_100km, 1)}</td>
      <td class="num weg-schmal">${num(trip.kwh_total, 0)}</td>
      <td class="num weg-schmal">${num(trip.outside_temp_c, 0)}</td>
      <td class="num weg-schmal">${num(trip.payload_kg, 0)}</td>
      <td class="num weg-schmal">${trip.speed_factor
        ? Math.round(trip.speed_factor * 100) : ""}</td>
      <td class="num weg-schmal">${soc}</td>
      <td class="tat-spalte">
        <button class="klein" data-open="${trip.id}">öffnen</button>
        <button class="klein" data-delete="${trip.id}">×</button>
      </td>
    </tr>`;
  }

  async function load() {
    const holder = document.getElementById("trips-list");
    if (!holder) return;
    holder.innerHTML = '<p class="leer">lädt …</p>';
    try {
      const trips = await K.api("/api/trips");
      if (!trips.length) {
        holder.innerHTML = '<p class="leer">Noch keine Fahrt geplant.</p>';
        return;
      }
      holder.innerHTML = `
        <div class="tabelle-halter">
          <table class="fahrten">
            <thead><tr>
              <th class="datum">Datum</th>
              <th>Strecke</th>
              <th class="num">km</th>
              <th class="num weg-eng">Zeit</th>
              <th class="num">kWh/100</th>
              <th class="num weg-schmal">kWh</th>
              <th class="num weg-schmal">°C</th>
              <th class="num weg-schmal">Zuladung</th>
              <th class="num weg-schmal">Tempo %</th>
              <th class="num weg-schmal">Ladestand</th>
              <th></th>
            </tr></thead>
            <tbody>${trips.map(row).join("")}</tbody>
          </table>
        </div>`;
      charged = true;
    } catch (failure) {
      holder.innerHTML = "";
      K.report("Fahrten: " + failure.message, "fehler");
    }
  }

  /* Bring an old trip back onto the map.
   *
   * Deliberately via joltRoute and not with its own drawing logic: it is
   * the same view as after a fresh calculation, and two ways of drawing the
   * same thing inevitably drift apart. */
  async function open_it(id) {
    try {
      await window.joltRoute.tripCharging(Number(id));
      window.joltApp.showView("plan");
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
    if (!window.confirm(`„${label}" löschen?\n\nEine aufgezeichnete `
                        + "Fahrt lässt sich nicht wiederherstellen.")) {
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

  async function startRecording() {
    lastStartError = "";
    const btn = document.getElementById("rec-start");
    const as_of = (text) => {
      const el = document.getElementById("rec-status");
      if (el) el.textContent = text;
    };
    btn.disabled = true;
    try {
      let withDongle = false;
      let dongleLater = false;   // not connected, but Bluetooth is there
      if (window.joltObd && window.joltObd.obtainable()) {
        as_of("Verbinde mit dem OBD2-Dongle …");
        try {
          window.joltObd.set_up((t) => console.log("[obd]", t));
          // First without a dialog: if the dongle has been allowed once
          // before, it connects without a touch.
          await window.joltObd.attach();
          if (await window.joltLive.handshakeSafe()) withDongle = true;
        } catch (failure) {
          // No reason to abort - only one to carry on without the dongle.
          console.log("[obd] Verbindung nicht zustande gekommen:", failure);
        }
        if (!withDongle) {
          as_of("Ohne Dongle – der Ladestand kommt von Hand.");
          dongleLater = true;
        }
      }

      // The choice from this section, not the one from the planning view -
      // and **no** fallback to the first vehicle in the list. That was the
      // bug: it silently assigned the trip to the "Allgemeines E-Auto"
      // (generic EV), and everything was calculated with it afterwards -
      // battery size, mass, drag. Better not to record at all than for the
      // wrong car.
      const choice = document.getElementById("rec-vehicle");
      const id = choice && choice.value ? Number(choice.value) : null;
      if (!id) {
        K.report("Erst ein Fahrzeug wählen – ohne das gehört die "
                 + "Aufzeichnung niemandem.", "fehler");
        lastStartError = "Kein Fahrzeug gewählt – in jolt einmal auswählen.";
        return false;
      }

      as_of("Standort holen …");
      const city = await new Promise((fulfil, reject) => {
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

      // With a dongle, pass the real start charge level right away - better than
      // the 100 % the server otherwise assumes.
      let soc = null;
      if (withDongle) {
        try {
          const reading = window.joltObd.socFromResponse(
            await window.joltObd.command("22028C"));
          if (reading) soc = Math.round(reading.hmi * 10) / 10;
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
            withDongle = false;
            dongleLater = true;
          } else {
            console.log("[obd] Startladestand ohne Antwort:", failure);
          }
        }
      }

      as_of("Fahrt anlegen …");
      const response = await K.api("/api/live/recording", {
        method: "POST",
        body: { vehicle_id: id, lat: city.latitude, lon: city.longitude,
                soc: soc,
                name: document.getElementById("rec-name").value },
      });
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
      if (withDongle) {
        window.joltLive.dongleUse();
        K.report(soc !== null
          ? "Aufzeichnung läuft, Ladestand kommt aus dem Auto."
          : "Aufzeichnung läuft. Das Auto antwortet noch nicht – jolt liest "
            + "den Ladestand, sobald du fährst.", "hinweis");
      } else {
        if (dongleLater) {
          // The dongle may still come: the car is often only switched on now.
          // jolt knocks on its own instead of waiting for the button - and
          // gives up after a few attempts.
          window.joltLive.dongleUse();
          window.joltLive.reconnectDongle();
        }
        K.report("Aufzeichnung läuft. Den Ladestand unterwegs gelegentlich "
          + "melden – ohne ihn lässt sich hinterher nichts lernen.", "hinweis");
      }
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

  async function carplayAction(action) {
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

  function set_up() {
    K.at("rec-start", "click", startRecording);
    carplayConnect();
    dongleHint();
    const holder = document.getElementById("trips-list");
    if (!holder) return;
    // One listener on the holder instead of one per row: the list is rebuilt
    // after every deletion, individually bound listeners would then be dead.
    holder.addEventListener("click", (event) => {
      const uphill = event.target.closest("[data-open]");
      if (uphill) { open_it(uphill.dataset.open); return; }
      const path = event.target.closest("[data-delete]");
      if (path) {
        const row = path.closest("tr");
        const title = row ? row.querySelector(".titel") : null;
        remove(path.dataset.delete,
                 title ? title.textContent.trim() : "Diese Fahrt");
      }
    });
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
           carplayAction, reportVehicle };
})();
