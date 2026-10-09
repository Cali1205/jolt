/* Web Bluetooth against an ELM327 dongle.
 *
 * The dongle is a serial interface in BLE disguise: one writes ASCII
 * commands to one characteristic and gets the response back as
 * notifications on a second one, terminated by a '>' as the prompt. There
 * is no more protocol than that.
 *
 * This page deliberately guesses little and shows a lot: every command
 * and every response is in the log. Whether the assumptions about the
 * ID.Buzz PIDs are correct is decided by what the car sends back - not by
 * what is written here.
 */
(function () {
  "use strict";

  /* Which GATT service an ELM327 clone offers is not standardized. Web
   * Bluetooth, however, requires that all services one wants to touch be
   * registered **beforehand** - one cannot connect first and look
   * afterwards. Hence the list of the common ones; the Vgate iCar Pro uses
   * 0xFFF0 according to common accounts, the others cost nothing.
   *
   * Written out as a 128-bit UUID and not in the short form `0xfff0`: the
   * specification allows both, but Bluefy passes the options on to a native
   * layer, and that stumbled over the number - `RequestDevice: Request
   * payload could not be parsed`, before a device dialog even appeared. The
   * written-out form leaves nothing to interpret, and Chrome accepts it
   * just as well. */
  const el = (id) => document.getElementById(id);
  const O = window.joltObd;   // connection, ELM327, readings
  let lastSoc = null;

  /* ---------- Log ---------- */

  function esc(text) {
    return String(text === null || text === undefined ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function log(text, direction) {
    const timestamp = new Date().toLocaleTimeString("de-DE");
    const arrow = direction === "raus" ? "→" : (direction === "rein" ? "←" : " ");
    el("log").textContent += `${timestamp} ${arrow} ${text}\n`;
    el("log").scrollTop = el("log").scrollHeight;
  }

  function as_of(text, level) {
    const k = el("connection");
    k.textContent = text;
    k.className = "stand " + (level || "");
  }

  function buttons(at) {
    for (const id of ["init", "soc", "send", "report", "trip-start", "check"]) el(id).disabled = !at;
  }

  /* ---------- Charge level ---------- */

  async function readSoc() {
    el("soc-output").textContent = "…";
    try {
      // Address and filters have been in place since the handshake; setting
      // them again here would not repeat ATCP17 and ATCAF1 and would thereby
      // destroy exactly what matters.
      const response = await O.command("22028C");
      const reading = O.socFromResponse(response);
      if (reading === null) {
        el("soc-output").textContent = "?";
        log("Antwort enthält kein 62028C - siehe oben. Entweder ist die "
            + "Datenkennung eine andere, oder das Steuergerät antwortet "
            + "nicht auf dieser Kennung.");
        return;
      }
      lastSoc = Math.round(reading.hmi * 10) / 10;
      // Show both numbers: the big one is what is in the car and what jolt
      // gets; the small one next to it makes it traceable what it came from.
      el("soc-output").textContent = lastSoc + " %";
      el("soc-origin").textContent =
        `Rohwert 0x${reading.raw.toString(16).toUpperCase()} = ${reading.raw}`
        + ` → brutto ${reading.bms.toFixed(1)} % → Anzeige ${reading.hmi.toFixed(1)} %`;
      log(`Ladestand: brutto ${reading.bms.toFixed(1)} %, `
          + `Anzeige ${reading.hmi.toFixed(1)} % (Rohwert ${reading.raw})`);
    } catch (failure) {
      el("soc-output").textContent = "–";
      log("FEHLER " + failure.message);
    }
  }


  /* ---------- The usual way: everything in one go ---------- */

  /* One button instead of five. Fully automatic is not possible -
   * `requestDevice` strictly requires a user gesture, a page may not
   * connect to a device by itself on load. But one gesture suffices for
   * the whole chain, and that is the difference between "doable in the car"
   * and "too cumbersome in the car".
   *
   * The trip is created here right away: without a running session, jolt
   * does accept the measurement points but stores them nowhere - and one
   * only notices afterwards. */
  function goAsOf(text, level) {
    const k = el("go-status");
    k.textContent = text;
    k.className = "stand " + (level || "");
  }

  function joltToken() {
    // The same login as the main app: whoever logged in there does not have
    // to do it again here.
    try { return localStorage.getItem("jolt-token") || ""; }
    catch (e) { return ""; }
  }

  /* How fast the car has to be to count as "driving". Ten km/h is safely
   * above GPS noise and above shunting in the yard, and safely below
   * anything that is a trip. */
  const DRIVES_FROM_KMH = 10;
  // Two measurements in a row, so that a single outlier does not create a
  // trip.
  const DRIVES_ROUNDS = 2;
  let moved = 0;

  /* A name nobody has to type.
   *
   * The name field was one step too many: whoever sits in the car does not
   * type. Date and time are the detail one searches by later anyway - and
   * jolt fills in start and destination itself when finishing, from the
   * first and last measurement point. */
  function tripName() {
    const own = el("trip-name").value.trim();
    if (own) return own;
    return new Date().toLocaleString("de-DE", {
      weekday: "short", day: "2-digit", month: "2-digit",
      hour: "2-digit", minute: "2-digit" });
  }

  async function start_driving() {
    const btn = el("go");
    btn.disabled = true;
    try {
      if (!O.linked()) {
        goAsOf("Dongle suchen …");
        await O.attach();
        if (!O.linked()) throw new Error("keine Verbindung zum Dongle");
      }

      goAsOf("Steuergerät vorbereiten …");
      if (!(await O.handshake())) {
        throw new Error("Handshake unvollständig – siehe Protokoll");
      }

      // First check whether anything arrives at all. Starting a recording that
      // then collects only positions without a charge level would be a lost
      // trip - and that would only be noticed at the destination.
      goAsOf("Ladestand lesen …");
      const probe = O.socFromResponse(await O.command("22028C"));
      if (!probe) throw new Error("Das Auto liefert keinen Ladestand");
      el("soc-output").textContent = Math.round(probe.hmi * 10) / 10 + " %";

      if (el("auto").checked) {
        // The same check as with the manual start. Without it the loop would
        // start and fail with a 401 on every creation of the trip - visible only
        // in the log, while "Bereit" is shown above.
        if (!el("token").value.trim() && !joltToken()) {
          throw new Error("Erst in jolt anmelden oder ein Logger-Token "
                          + "eintragen");
        }
        // Do not create it immediately: whoever connects while standing would
        // otherwise get a trip that begins at the driveway and contains half an
        // hour of parking. The page waits until something moves.
        goAsOf("Bereit – wartet, bis das Auto fährt.", "gut");
        running = true;
        moved = 0;
        // `round` controls which rarely read values are due (`readRecord`).
        // Without resetting, the second trip of a session continues counting
        // where the first one stopped.
        round = 0;
        el("trip-start").hidden = true;
        el("trip-stop").hidden = false;
        await screenAwakeHold();
        log("Automatik: warte auf Bewegung.");
        tripLoop();
        return;
      }

      await createTrip(probe);
      await startTrip();
    } catch (failure) {
      goAsOf("Ging nicht: " + failure.message, "schlecht");
      log("FEHLER " + failure.message);
    } finally {
      btn.disabled = false;
    }
  }

  /* Create the trip in jolt. Separate from connecting, because with the
   * automatic mode switched on it only comes into being once the car
   * drives off. */
  async function createTrip(soc) {
    goAsOf("Fahrt anlegen …");
    const place = await getPosition();
    const response = await fetch("/api/live/recording", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Token": joltToken() },
      body: JSON.stringify({
        vehicle_id: vehicleId(),
        lat: place.lat, lon: place.lon,
        soc: soc ? Math.round(soc.hmi * 10) / 10 : null,
        name: tripName() }),
    });
    if (!response.ok) {
      const failure = await response.json().catch(() => ({}));
      throw new Error(failure.detail || `jolt antwortet HTTP ${response.status}`);
    }
    const trip = await response.json();
    sessionId = trip.session_id;
    log(`Aufzeichnung ${trip.trip_id} läuft (Sitzung ${trip.session_id}).`);
    goAsOf(`Aufzeichnung läuft – Fahrt ${trip.trip_id}`, "gut");
    return trip;
  }

  /* Which vehicle - asked, not guessed.
   *
   * `fahrzeuge[0]` used to stand here. The list comes sorted by ID, and the
   * first one is the "Allgemeine E-Auto" created on first start - not the
   * one you are sitting in. The recording would have been attributed to the
   * wrong vehicle, and worse: the calibration would have shifted the
   * correction factor of a car nobody drove.
   *
   * The choice stays in the browser. Whoever sits in the car wants to make
   * it once and never again. */
  async function vehiclesCharging() {
    const selection = el("vehicle-choice-obd");
    if (!selection) return;
    try {
      const response = await fetch("/api/vehicles",
                                  { headers: { "X-Token": joltToken() } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const vehicles = await response.json();
      let remembered = null;
      try { remembered = localStorage.getItem("jolt-obd-fahrzeug"); } catch (e) {}
      selection.innerHTML = vehicles
        .map((f) => `<option value="${f.id}">${esc(f.name)}</option>`).join("");
      if (remembered && vehicles.some((f) => String(f.id) === remembered)) {
        selection.value = remembered;
      }
      selection.addEventListener("change", () => {
        try { localStorage.setItem("jolt-obd-fahrzeug", selection.value); }
        catch (e) {}
      });
    } catch (failure) {
      log("Fahrzeugliste: " + failure.message
          + " - erst in jolt anmelden, dann hier neu laden.");
    }
  }

  function vehicleId() {
    const selection = el("vehicle-choice-obd");
    if (!selection || !selection.value) {
      throw new Error("Kein Fahrzeug gewählt - erst in jolt anmelden, "
                      + "dann hier neu laden.");
    }
    return Number(selection.value);
  }

  /* ---------- Recording ---------- */

  let running = false;
  let round = 0;
  let wake_lock = null;   // WakeLockSentinel
  let sessionId = null;    // set when this page created the trip

  /* Keep the screen awake. Without it iOS switches it off after a minute,
   * and with the screen the page content sleeps - the connection survives
   * the lock screen, but the loop does not.
   *
   * The lock is lost when the page goes to the background and does not come
   * back by itself; that is why it is re-acquired on return. If the browser
   * does not know the interface, the recording runs anyway - then one has
   * to keep the screen on via the settings. */
  async function screenAwakeHold() {
    if (!("wakeLock" in navigator)) {
      log("Dieser Browser kennt keine Bildschirmsperre-Verhinderung. "
          + "Automatische Sperre bitte in den iOS-Einstellungen auf 'Nie'.");
      return;
    }
    try {
      wake_lock = await navigator.wakeLock.request("screen");
      log("Bildschirm wird wachgehalten.");
    } catch (failure) {
      log("Bildschirm wachhalten ging nicht: " + failure.message);
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (running && document.visibilityState === "visible" && !wake_lock) {
      screenAwakeHold();
    }
  });

  function tiles(vals) {
    el("trip-values").innerHTML = vals.map(([name, num]) =>
      `<div class="wert"><div class="zahl">${num}</div>`
      + `<div class="name">${name}</div></div>`).join("");
  }

  async function aRound() {
    const raw = await O.readRecord(round);
    round += 1;

    const soc = O.socFromRaw(raw.soc_raw);
    const place = await getPosition().catch((f) => {
      log("Standort: " + f.message);
      return null;
    });
    if (!place) return null;

    if (typeof place.elevation_m === "number") raw.elevation_m = Math.round(place.elevation_m);

    /* Automatic: wait until the car is really driving.
     *
     * Measurement is by the vehicle's speed, not by GPS - the car knows it
     * more precisely and delivers it anyway. If the value is missing, GPS
     * serves as fallback; if that is missing too, there is no waiting and
     * recording starts right away. An automatic that does nothing for lack
     * of a measurement would be the worst kind of automatic.
     *
     * Two rounds in a row, so that a single outlier does not create a trip -
     * and no trip comes into being while the car shunts in the yard. */
    if (!sessionId && el("auto").checked && !el("token").value.trim()) {
      const speed = typeof raw.speed_kmh === "number" ? raw.speed_kmh
        : (typeof place.speed_kmh === "number" && !Number.isNaN(place.speed_kmh)
           ? place.speed_kmh : null);
      if (speed !== null && speed < DRIVES_FROM_KMH) {
        moved = 0;
        return { soc, raw, waits: true,
                 records: { reason: `steht (${Math.round(speed)} km/h)` } };
      }
      moved += 1;
      if (speed !== null && moved < DRIVES_ROUNDS) {
        return { soc, raw, waits: true,
                 records: { reason: `fährt an (${Math.round(speed)} km/h)` } };
      }
      log(`Bewegung erkannt${speed === null ? " (kein Tempo messbar)"
                                            : ` (${Math.round(speed)} km/h)`}`
          + " - Fahrt wird angelegt.");
      try {
        await createTrip(soc);
      } catch (failure) {
        // Do not give up: the next round tries again. A dead spot when driving
        // off is the normal case, not the exception.
        log("Fahrt anlegen: " + failure.message + " - nächste Runde erneut");
        moved = 0;
        return { soc, raw, waits: true,
                 records: { reason: "jolt nicht erreichbar" } };
      }
    }

    const payload = {
      lat: place.lat, lon: place.lon,
      soc: Math.round(soc.hmi * 10) / 10,
      raw_values: raw,
    };
    // What the car measures itself beats any forecast: the outside
    // temperature used to go into the consumption model from Open-Meteo.
    if (typeof raw.speed_kmh === "number") payload.speed_kmh = raw.speed_kmh;
    if (typeof raw.outside_temp_c === "number") {
      payload.outside_temp_c = raw.outside_temp_c;
    }

    /* Two ways in, and which one applies depends on who created the trip. If
     * this page did, it knows the session and reports directly to it. If the
     * trip runs in the jolt app on another device, this page does not know
     * the session - then it identifies itself with the vehicle's logger
     * token, and jolt looks for the running session itself. */
    const url = sessionId
      ? `/api/live/${sessionId}/point`
      : "/api/live/report";
    if (!sessionId) payload.token = el("token").value.trim();

    // `/point` requires login; `/report` identifies itself with the logger
    // token in the body and does not need the header, but it does no harm
    // either.
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Token": joltToken() },
      body: JSON.stringify(payload),
    });
    const records = await response.json().catch(() => ({}));
    // The session route answers with the state and knows no "aufgenommen" -
    // if it gives 200, the point is in.
    if (sessionId && response.ok) records.recorded = true;
    return { soc, raw, records, status: response.status };
  }

  async function tripLoop() {
    while (running) {
      const startedAt = Date.now();
      try {
        const result = await aRound();
        if (result && result.waits) {
          // In the waiting state measurements are taken but nothing is reported.
          // What was read is displayed anyway - otherwise the page would look as
          // if it did nothing.
          tiles([
            ["Ladestand", Math.round(result.soc.hmi * 10) / 10 + " %"],
            ["Zustand", "wartet auf Fahrt"],
          ]);
          stand2("Bereit – " + (result.records.reason || "wartet"), "gut");
        } else if (result) {
          const { soc, raw, records } = result;
          lastSoc = Math.round(soc.hmi * 10) / 10;
          el("soc-output").textContent = lastSoc + " %";
          const power = (typeof raw.voltage_v === "number"
                            && typeof raw.current_a === "number")
            ? (raw.voltage_v * raw.current_a / 1000).toFixed(1) + " kW" : "–";
          tiles([
            ["Ladestand", lastSoc + " %"],
            ["brutto", soc.bms.toFixed(1) + " %"],
            ["Leistung", power],
            ["Spannung", (raw.voltage_v ?? "–") + " V"],
            ["aufgenommen", records.recorded ? "ja" : "nein"],
            ["Runde", String(round)],
          ]);
          stand2(records.recorded
            ? `läuft – zuletzt ${new Date().toLocaleTimeString("de-DE")}`
            : `läuft – jolt: ${records.reason || "nicht aufgenommen"}`,
            records.recorded ? "gut" : "");
          log(`Runde ${round}: ${lastSoc} % (roh ${raw.soc_raw})`
              + `${raw._missing ? ", ohne " + raw._missing.join("/") : ""}`
              + ` → jolt ${records.recorded ? "ok" : (records.reason || "?")}`);
        }
      } catch (failure) {
        // A dropout does not end the trip. Tunnel, dead spot, a control unit that
        // just does not feel like it - next time it works again, and an aborted
        // recording is only noticed afterwards.
        stand2("Aussetzer: " + failure.message, "schlecht");
        log("Runde übersprungen: " + failure.message);
      }
      const rest = Number(el("tick").value) * 1000 - (Date.now() - startedAt);
      await new Promise((w) => setTimeout(w, Math.max(1000, rest)));
    }
  }

  function stand2(text, level) {
    const k = el("trip-status");
    k.textContent = text;
    k.className = "stand " + (level || "");
  }

  async function startTrip() {
    if (!el("token").value.trim() && !joltToken()) {
      stand2("Erst in jolt anmelden oder ein Logger-Token eintragen.",
             "schlecht");
      return;
    }
    running = true;
    round = 0;
    el("trip-start").hidden = true;
    el("trip-stop").hidden = false;
    await screenAwakeHold();
    log("Aufzeichnung gestartet.");
    tripLoop();
  }

  async function endTrip() {
    running = false;
    // Finish the trip in jolt if this page created it. Without it the
    // recording stays open, and the measurement points never turn into a
    // route - the whole purpose would be missed.
    if (sessionId) {
      try {
        const response = await fetch(`/api/live/${sessionId}/end`, {
          method: "POST", headers: { "X-Token": joltToken() } });
        const data = await response.json().catch(() => ({}));
        const built = data.recording || {};
        if (built.ok) {
          log(`Fahrt abgeschlossen: ${built.distance_km} km, `
              + `${built.consumption_kwh} kWh gerechnet, Höhen aus `
              + `${built.elevations}.`);
        } else if (built.reason) {
          log("Fahrt nicht auswertbar: " + built.reason);
        }
        if (data.learned) {
          log(`Gelernt: Faktor ${data.learned.earlier} → `
              + `${data.learned.after} (Fahrt ×${data.learned.raw_factor})`);
        } else if (data.not_learned) {
          log("Nichts gelernt: " + data.not_learned);
        }
      } catch (failure) {
        log("Fahrt beenden: " + failure.message);
      }
      sessionId = null;
    }
    el("trip-start").hidden = false;
    el("trip-stop").hidden = true;
    stand2("beendet");
    if (wake_lock) {
      try { await wake_lock.release(); } catch (e) {}
      wake_lock = null;
    }
    log("Aufzeichnung beendet.");
  }

  /* ---------- Check values without driving ---------- */

  /* What would be plausible. Two kinds of check:
   *
   *  - **Range**: Is the value where it physically must be? Catches
   *    formula errors - a battery capacity of 3 kWh or 8000.
   *  - **Cross-comparison**: Do two independently read values fit
   *    together? That is the sharper test. Discharge counter divided by
   *    odometer must give the lifetime consumption - if it hits 15 to 35
   *    kWh/100 km, **both** formulas are right, without ever having
   *    driven.
   */
  const RANGES = {
    soc_raw: [0, 255, "Rohwert, geteilt durch 2,5 ergibt Prozent"],
    voltage_v: [250, 450, "Packspannung eines 400-V-Systems"],
    current_a: [-600, 600, "im Stand nahe null"],
    charge_limit_a: [0, 600],
    ptc_current_a: [-5, 100, "im Stand meist null"],
    speed_kmh: [0, 260, "im Stand null"],
    outside_temp_c: [-40, 60],
    inside_temp_c: [-40, 80],
    aux_load_kw: [-2, 20, "im Stand ein bis drei kW"],
    odometer_km: [1, 999999],
    dcdc_current_a: [-400, 400],
    battery_kwh: [10, 200, "nutzbar - beim ID.Buzz 77 kWh neu, weniger mit "
               + "den Jahren"],
    range_km: [0, 999, "mit der Anzeige im Auto vergleichen"],
    batterie_c: [-40, 80, "nach dem Stehen nahe der Aussentemperatur"],
    /* Lifetime counter. The bound used to be [1, 999999] and thereby let
     * 482 961 kWh through - exactly the value that the missing sign handling
     * produced. A bound that does not catch the error it is supposed to
     * catch is none. 100 000 kWh correspond to half a million kilometres at
     * 20 kWh/100 km. */
    discharge_kwh: [100, 100000, "Lebensdauerzähler"],
    charged_kwh: [100, 100000, "Lebensdauerzähler"],
  };

  function check_row(title, value, verdict, note) {
    const colour = verdict === "ok" ? "gut"
      : (verdict === "fehlt" ? "" : "schlecht");
    return `<tr class="${colour}"><th>${title}</th><td>${value}</td>`
      + `<td>${note || ""}</td></tr>`;
  }

  async function valuesCall() {
    const btn = el("check");
    btn.disabled = true;
    const resultEl = el("check-result");
    resultEl.innerHTML = "<p>lese …</p>";
    try {
      if (!O.linked()) {
        await O.attach();
        if (!O.linked()) throw new Error("keine Verbindung zum Dongle");
        if (!(await O.handshake())) throw new Error("Handshake unvollständig");
      }
      // Round 0 - so that the rarely read values are included too.
      const raw = await O.readRecord(0);
      const fields = O.FIELDS;
      const empty = new Set(raw._empty || []);
      const missing = new Set(raw._missing || []);

      const rows = [];
      let good = 0, bad = 0, without = 0;
      for (const f of fields) {
        const w = raw[f.name];
        if (typeof w !== "number") {
          without += 1;
          rows.push(check_row(f.title, "–",
            "fehlt", empty.has(f.name) ? "antwortet nicht"
              : (missing.has(f.name) ? "keine Antwort" : "nicht gelesen")));
          continue;
        }
        const b = RANGES[f.name];
        const text = `${Math.round(w * 100) / 100}${f.unit ? " " + f.unit : ""}`;
        if (!b) { rows.push(check_row(f.title, text, "ok", "")); good += 1; continue; }
        const inside = w >= b[0] && w <= b[1];
        if (inside) good += 1; else bad += 1;
        rows.push(check_row(f.title, text, inside ? "ok" : "schlecht",
          inside ? (b[2] || "") : `erwartet ${b[0]} bis ${b[1]}`));
      }

      /* The cross-comparison. It needs no trip and checks two formulas at
       * once: if discharge counter and odometer together give a sensible
       * lifetime consumption, both can hardly be wrong - an error in either
       * of the two byte positions would shift the result by orders of
       * magnitude. */
      if (typeof raw.discharge_kwh === "number"
          && typeof raw.odometer_km === "number" && raw.odometer_km > 100) {
        const net = raw.discharge_kwh
          - (typeof raw.charged_kwh === "number" ? raw.charged_kwh : 0);
        const je100 = raw.discharge_kwh / raw.odometer_km * 100;
        const inside = je100 >= 12 && je100 <= 40;
        // Count it as well. Before, it showed red in the table, but the line
        // above still reported "0 auffaellig" - and that is the one read first.
        if (inside) good += 1; else bad += 1;
        rows.push(check_row(
          "<strong>Kreuzvergleich</strong>",
          `${je100.toFixed(1)} kWh/100 km`,
          inside ? "ok" : "schlecht",
          inside ? `${raw.discharge_kwh.toFixed(0)} kWh entladen auf `
                 + `${raw.odometer_km} km – das passt zusammen`
               : "erwartet 12 bis 40 – eine der beiden Formeln stimmt nicht"));
        rows.push(check_row("Zähler netto",
          `${net.toFixed(1)} kWh`, "ok",
          "entladen minus geladen, über die Lebensdauer"));
      }

      resultEl.innerHTML =
        `<p><b>${good}</b> plausibel, <b>${bad}</b> auffällig, `
        + `<b>${without}</b> ohne Wert</p>`
        + `<table class="pruef"><tbody>${rows.join("")}</tbody></table>`;
      log(`Prüfung: ${good} plausibel, ${bad} auffällig, ${without} ohne Wert`);
    } catch (failure) {
      resultEl.innerHTML = `<p class="stand schlecht">${esc(failure.message)}</p>`;
      log("Prüfung: " + failure.message);
    } finally {
      btn.disabled = false;
    }
  }

  /* ---------- Narrowing down the A/C compressor ---------- */

  /* The response to 220800 carries four 16-bit numbers, and none of the
   * three sources (spot2000, WiCAN, codingABI) names a conversion.
   * Guessing would be especially tempting and especially wrong here - four
   * candidates, all in the plausible watt range.
   *
   * A difference measurement decides it without any assumption: read
   * twice, once with the compressor running and once without. What changes
   * by hundreds is the power; what stays the same is something else. */
  let climateA = null;

  async function readClimate() {
    if (!O.linked()) {
      await O.attach();
      if (!O.linked()) throw new Error("keine Verbindung zum Dongle");
      if (!(await O.handshake())) throw new Error("Handshake unvollständig");
    }
    await O.series(["ATSP6", "ATSH746", "ATFCSH746", "ATFCSD300000",
                   "ATFCSM1", "ATCRA7B0"]);
    const response = await O.command("220800", 8000);
    await O.command("ATSP7").catch(() => {});
    const bytes = O.payload_bytes(response, "220800");
    if (!bytes || bytes.length < 8) {
      throw new Error("keine brauchbare Antwort auf 220800");
    }
    return bytes;
  }

  /* Both measurements side by side.
   *
   * The first version slid a two-byte window **byte by byte** through the
   * response and thereby showed nothing but overlapping phantom values:
   * "byte 1-2 = 9408" next to "byte 2-3 = 49188", of which only the first
   * is a quantity. A multi-byte number does not start at every byte.
   *
   * Now both separately: first every byte on its own, then the **aligned**
   * pairs from byte 1 - the way the control unit means them. What differs
   * clearly in both columns is the candidate. */
  function showClimate() {
    const resultEl = el("climate-result");
    if (!climateA || !climateA.b) { resultEl.innerHTML = ""; return; }
    const a = climateA.a, b = climateA.b;
    const n = Math.min(a.length, b.length);

    const single = [];
    for (let i = 0; i < n; i++) {
      const d = b[i] - a[i];
      single.push(`<tr class="${d ? "gut" : ""}"><th>Byte ${i}</th>`
        + `<td>${a[i]}</td><td>${b[i]}</td>`
        + `<td>${d > 0 ? "+" : ""}${d || "–"}</td></tr>`);
    }

    const pairs = [];
    const candidates = [];
    for (let i = 1; i + 1 < n; i += 2) {
      const va = a[i] * 256 + a[i + 1], vb = b[i] * 256 + b[i + 1];
      const d = vb - va;
      const conspicuous = Math.abs(d) >= 100;
      if (conspicuous) candidates.push(`Byte ${i}–${i + 1}: ${va} → ${vb}`);
      pairs.push(`<tr class="${conspicuous ? "gut" : ""}">`
        + `<th>Byte ${i}–${i + 1}</th><td>${va}</td><td>${vb}</td>`
        + `<td>${d > 0 ? "+" : ""}${d || "–"}</td></tr>`);
    }

    resultEl.innerHTML =
      `<p>Bit 0 von Byte 0: <b>${a[0] & 1}</b> → <b>${b[0] & 1}</b>`
      + `${(a[0] & 1) !== (b[0] & 1) ? " – das ist das An/Aus-Bit." : ""}</p>`
      + `<table class="pruef"><tbody>`
      + `<tr><th>einzeln</th><td>aus</td><td>an</td><td>Δ</td></tr>`
      + single.join("")
      + `<tr><th>Paare ab Byte 1</th><td>aus</td><td>an</td><td>Δ</td></tr>`
      + pairs.join("")
      + `</tbody></table>`
      + `<p>${candidates.length
          ? "Deutlich verändert: <b>" + candidates.join(", ") + "</b>. "
            + "Die kleinste dieser Zahlen ist meist die Leistung in Watt, "
            + "die grösseren sind Soll- und Ist-Drehzahl."
          : "Nichts hat sich deutlich verändert – war der Kompressor bei "
            + "beiden Messungen im selben Zustand?"}</p>`;
  }

  /* ---------- Report to jolt ---------- */

  function getPosition() {
    return new Promise((fulfil, reject) => {
      if (!navigator.geolocation) { reject(new Error("kein GPS")); return; }
      navigator.geolocation.getCurrentPosition(
        // The GPS elevation is recorded along, although it is too imprecise for
        // the gradient (it scatters by ten to twenty metres). It costs nothing
        // and is the fallback if no map data can be obtained on finishing.
        // `speed` comes in m/s and is often null (cold fix, standstill).
        // Without this conversion the GPS fallback of the movement detection
        // further below was dead code: `place.speed_kmh` simply did not exist, and
        // without speed from the car the automatic created the trip immediately -
        // parking lot included.
        (p) => fulfil({ lat: p.coords.latitude, lon: p.coords.longitude,
                           elevation_m: p.coords.altitude,
                           speed_kmh: typeof p.coords.speed === "number"
                             ? p.coords.speed * 3.6 : null }),
        (f) => reject(new Error("Standort: " + f.message)),
        { enableHighAccuracy: true, timeout: 10000 });
    });
  }

  async function report() {
    const token = el("token").value.trim();
    if (!token) { log("Kein Logger-Token eingetragen."); return; }
    if (lastSoc === null) { log("Erst den Ladestand abfragen."); return; }
    try {
      const place = await getPosition();
      const response = await fetch("/api/live/report", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, soc: lastSoc,
                               lat: place.lat, lon: place.lon }),
      });
      const data = await response.json();
      log(`jolt: HTTP ${response.status} ${JSON.stringify(data).slice(0, 200)}`);
    } catch (failure) {
      log("FEHLER " + failure.message);
    }
  }

  /* ---------- Setup ---------- */

  // `O.obtainable()` instead of `navigator.bluetooth`: in the iOS app the
  // Web API does not exist, but Bluetooth does - there it comes via
  // CoreBluetooth (see obd-ble-native.js). Whoever checks the Web API
  // directly here declares the page unusable precisely where it works best.
  if (!O.obtainable()) {
    el("unsuitable").hidden = false;
    el("connect").disabled = true;
  }

  /* Errors in readings.js belong on the page, not only in the console. A
   * mistyped address name looks like a silent control unit at the car -
   * and then one searches in the wrong place. */
  if (O.TABLE_ERROR && O.TABLE_ERROR.length) {
    const box = el("unsuitable");
    box.hidden = false;
    box.innerHTML = "<strong>Fehler in der Messwert-Tabelle "
      + "(readings.js):</strong><ul><li>"
      + O.TABLE_ERROR.map((t) => t.replace(/[<&]/g, "")).join("</li><li>")
      + "</li></ul>";
  }
  // The module reports everything here, and a dropout during a recording
  // is a reason to reconnect - otherwise not.
  O.set_up(log, () => { if (running) O.reconnect(1, () => running); });
  el("connect").addEventListener("click", async () => {
    as_of("verbinde …");
    await O.attach();
    if (O.linked()) { as_of("verbunden", "gut"); buttons(true); }
    else { as_of("nicht verbunden", "schlecht"); }
  });
  el("init").addEventListener("click", async () => {
    if (await O.handshake()) log("Handshake durch.");
  });
  el("soc").addEventListener("click", readSoc);
  /* The button locks itself while the series is running.
   *
   * A multi-frame response takes over a second. Whoever taps again in that
   * time starts a second series, and it stabs the first command in the
   * back: "es läuft noch ein Befehl". In the log it then looked as if the
   * control unit had not answered - when it was the UI. */
  el("send").addEventListener("click", async () => {
    const btn = el("send");
    btn.disabled = true;
    const earlier = btn.textContent;
    btn.textContent = "läuft …";
    try {
      await O.series(el("free").value.split("\n"));
    } finally {
      btn.disabled = false;
      btn.textContent = earlier;
    }
  });
  el("report").addEventListener("click", report);
  el("check").addEventListener("click", valuesCall);
  el("climate-a").addEventListener("click", async () => {
    const k = el("climate-a");
    k.disabled = true;
    try {
      climateA = { a: await readClimate(), b: null };
      log("Klima-Messung 1 (aus): " + climateA.a.join(" "));
      el("climate-result").innerHTML =
        "<p>Erste Messung steht. Jetzt die Klimaanlage <strong>kräftig "
        + "einschalten</strong> (kalt, hohe Gebläsestufe), eine halbe Minute "
        + "warten und dann die zweite Messung.</p>";
      el("climate-b").disabled = false;
    } catch (failure) {
      el("climate-result").innerHTML =
        `<p class="stand schlecht">${esc(failure.message)}</p>`;
      log("Klima-Messung 1: " + failure.message);
    } finally {
      k.disabled = false;
    }
  });
  el("climate-b").addEventListener("click", async () => {
    const k = el("climate-b");
    k.disabled = true;
    try {
      climateA.b = await readClimate();
      log("Klima-Messung 2 (an): " + climateA.b.join(" "));
      showClimate();
    } catch (failure) {
      el("climate-result").innerHTML =
        `<p class="stand schlecht">${esc(failure.message)}</p>`;
      log("Klima-Messung 2: " + failure.message);
    } finally {
      k.disabled = false;
    }
  });
  el("log-clear").addEventListener("click", () => { el("log").textContent = ""; });
  /* On the phone, selecting inside a scrolling box is fiddly, and a
   * screenshot loses exactly what matters: the hex responses character by
   * character. */
  el("go").addEventListener("click", start_driving);
  el("trip-start").addEventListener("click", startTrip);
  el("trip-stop").addEventListener("click", endTrip);
  el("tick").addEventListener("input", (e) => {
    el("tick-output").textContent = e.target.value;
  });
  el("log-copy").addEventListener("click", async () => {
    const text = el("log").textContent;
    try {
      await navigator.clipboard.writeText(text);
      el("log-copy").textContent = "kopiert";
      setTimeout(() => { el("log-copy").textContent = "Protokoll kopieren"; }, 2000);
    } catch (failure) {
      // Without clipboard (older browser, missing permission) selecting by hand
      // remains - then at least select everything at once.
      const zone = document.createRange();
      zone.selectNodeContents(el("log"));
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(zone);
      log("Zwischenablage nicht verfügbar - Protokoll ist markiert, bitte "
          + "von Hand kopieren.");
    }
  });
  vehiclesCharging();
  log("Bereit. Dongle einstecken, Zündung an, dann „Fahrt starten“.");
})();
