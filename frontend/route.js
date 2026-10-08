/* The planning view: search places, compute the route, show the result. */
window.joltRoute = (function () {
  "use strict";

  const K = window.jolt;
  const chosen = { start: null, destination: null };
  // What is currently on the map. The charger list and the charging plan are
  // loaded separately but draw into the same map - without this shared state
  // one would delete the markers of the other.
  let latestChargers = [];
  let lastPlan = null;
  // The variants of the last computation (fastest/shortest/recommended,
  // merged where identical) - for the switch cards and to know, when a card
  // is tapped, which trip_id it corresponds to.
  let latestVariants = [];
  let latestDeparture = null;       // what the traffic applies to: null = now

  /* ---------- Place search ---------- */

  function placeSearchSetUp(fieldId, hitId, keyname) {
    const field = document.getElementById(fieldId);
    const lst = document.getElementById(hitId);
    if (!field || !lst) return;
    let wait = null;

    field.addEventListener("input", () => {
      chosen[keyname] = null;
      clearTimeout(wait);
      const text = field.value.trim();
      if (text.length < 2) { lst.innerHTML = ""; return; }
      // Typing is faster than answering. Without this pause every character
      // sends a request - with the free ORS quota of 2500 requests per day
      // that is the quickest way to use it up.
      wait = setTimeout(() => seek(text, lst, field, keyname), 400);
    });
  }

  async function seek(text, lst, field, keyname) {
    try {
      const response = await K.api("/api/orte?text=" + encodeURIComponent(text));
      lst.innerHTML = "";
      if (!response.hit.length) {
        lst.innerHTML = '<li class="leer">Nichts gefunden.</li>';
        return;
      }
      for (const city of response.hit) {
        const entry = document.createElement("li");
        const btn = document.createElement("button");
        btn.textContent = city.name;
        btn.addEventListener("click", () => {
          chosen[keyname] = city;
          field.value = city.name;
          lst.innerHTML = "";
        });
        entry.appendChild(btn);
        lst.appendChild(entry);
      }
    } catch (failure) {
      lst.innerHTML = "";
      K.report("Ortssuche: " + failure.message, "fehler");
    }
  }

  /* ---------- Compute route ---------- */

  async function compute() {
    const btn = document.getElementById("rechnen");
    const vehicleId = Number(document.getElementById("fahrzeug-wahl").value);
    if (!vehicleId) { K.report("Erst ein Fahrzeug anlegen.", "fehler"); return; }
    if (!chosen.start || !chosen.destination) {
      K.report("Start und Ziel aus der Vorschlagsliste auswählen.", "fehler");
      return;
    }

    K.reportsClear();
    btn.disabled = true;
    btn.textContent = "rechnet …";
    try {
      // A computed route is stored as a trip - the list in the "Fahrten" tab
      // is therefore no longer up to date.
      K.state.tripsStale = true;
      const response = await K.api("/api/route", { method: "POST", body: {
        vehicle_id: vehicleId,
        start: { lat: chosen.start.lat, lon: chosen.start.lon,
                 text: chosen.start.name },
        destination: { lat: chosen.destination.lat, lon: chosen.destination.lon,
                text: chosen.destination.name },
        start_soc: Number(document.getElementById("start-soc").value),
        speed_factor: Number(document.getElementById("tempo").value) / 100,
        air_drag_factor: Number(document.getElementById("anbau").value),
        alternative: document.getElementById("alternative").checked,
        own_trips: document.getElementById("eigene-fahrten").checked,
        tomtom: document.getElementById("tomtom").checked,
        departure: departureIso(document.getElementById("abfahrt").value),
        // The slider sits at the far left on a negative value - that means
        // "not set", and then the vehicle profile applies. A separate switch
        // next to it would be a second control for a question the slider
        // already answers.
        payload_kg: payloadValue(),
        ...trailerValues(),
        speed_max_kmh: numberOrNull("tempo-max"),
      }});
      latestVariants = response.variants || [];
      latestDeparture = response.departure || null;
      drawVariants();
      // The most economical variant is jolt's basic stance - it is
      // preselected, tapping another card switches.
      const suggestion = latestVariants.find(
        (v) => v.labels.includes("insgesamt schnellste")) || latestVariants[0];
      if (suggestion) await variantChoose(suggestion);
    } catch (failure) {
      K.report("Route: " + failure.message, "fehler");
    } finally {
      btn.disabled = false;
      btn.textContent = "Route rechnen";
    }
  }

  async function variantChoose(variant) {
    K.state.trip = variant;
    show(variant);
    drawVariants();
    await chargersCharging();
    await chargePlanCharging();
  }

  /* Load a saved trip and draw it.
   *
   * The endpoint returns the same shape as a fresh variant, so the same path
   * as after computing is enough. The variant cards stay empty: a trip
   * loaded on its own has no siblings any more - the other variants from
   * back then are separate trips with their own ID. */
  async function tripCharging(tripId) {
    const trip = await K.api("/api/fahrten/" + tripId);
    latestVariants = [];
    drawVariants();
    K.state.trip = trip;
    show(trip);
    await chargersCharging();
    await chargePlanCharging();
  }

  /* ---------- Variant cards ---------- */

  function drawVariants() {
    const block = document.getElementById("varianten-block");
    const lst = document.getElementById("varianten");
    if (!block || !lst) return;
    block.hidden = latestVariants.length < 2;
    if (latestVariants.length < 2) { lst.innerHTML = ""; return; }

    const activeId = K.state.trip && K.state.trip.trip_id;
    lst.innerHTML = "";
    for (const v of latestVariants) {
      const btn = document.createElement("button");
      btn.className = "variante";
      btn.type = "button";
      btn.setAttribute("aria-pressed", String(v.trip_id === activeId));
      const labels = v.labels.map((e) =>
        `<span class="etikett ${e === "insgesamt schnellste" ? "sparsamste" : ""}">${e}</span>`
      ).join("");
      /* The key figures show what matters: the time **including charging** and
       * the cost. Before, travel time and kWh were shown there - neither says
       * when you arrive if you have to charge twice. */
      /* With traffic, if TomTom delivered it: the ranking computes the same
       * way, and the number on the label must match it. */
      const withTraffic = typeof v.plan_total_with_traffic_min === "number";
      const total = v.plan_feasible
        ? `${K.duration(withTraffic ? v.plan_total_with_traffic_min : v.plan_total_minutes)} `
          + `inkl. Laden${withTraffic ? " und Verkehr" : ""} · ${v.plan_stops} Stopps`
        : (v.plan_feasible === false ? "kein Ladeplan möglich"
                                    : K.duration(v.drive_time_minutes) + " Fahrzeit");
      const cost = (v.plan_cost_eur !== undefined && v.plan_cost_eur !== null)
        ? ` · ${K.num(v.plan_cost_eur, 2)} €` : "";
      const traffic = typeof v.traffic_min === "number"
        ? ` · Verkehr ${v.traffic_min >= 0.5 ? "+" + K.num(v.traffic_min, 0) + " min" : "frei"}` : "";
      btn.innerHTML = `
        <span class="etiketten">${labels}</span>
        <span class="kennwerte">${K.num(v.distance_km)} km · ${total}${cost}${traffic}</span>`;
      btn.addEventListener("click", () => {
        if (v.trip_id !== (K.state.trip && K.state.trip.trip_id)) {
          variantChoose(v);
        }
      });
      lst.appendChild(btn);
    }
    /* Name the source: the traffic comes from TomTom, and anyone seeing a number
     * should know where it is from. */
    if (latestVariants.some((v) => v.traffic_source)) {
      const source = document.createElement("div");
      source.className = "unter";
      source.textContent = latestDeparture
        ? "Verkehr: TomTom, Prognose für " + departureText(latestDeparture) + " · Wetter: Vorhersage für diese Zeit"
        : "Verkehr: TomTom, Stand jetzt";
      lst.appendChild(source);
    }
  }

  /* Make access restrictions visible.
   *
   * A charge point can look flawless - 300 kW, eight stalls, no detour -
   * and still be unusable because it is behind a barrier or open only to
   * hotel guests. jolt has known this data since the rework of the OCM
   * import; having it and not showing it would be the worst of all
   * options. */
  function accessHint(k) {
    const parts = [];
    if (k.access && !/^public$/i.test(k.access)) parts.push(sanitize(k.access));
    if (k.membership_required) parts.push("Mitgliedschaft nötig");
    const h = k.hints || {};
    if (h.cost) parts.push(sanitize(String(h.cost).slice(0, 40)));
    if (h.access) parts.push(sanitize(String(h.access).slice(0, 60)));
    return parts.length ? ` · <span style="color:var(--warnung)">${parts.join(" · ")}</span>` : "";
  }

  /* ---------- Result ---------- */

  /* The traffic tile of the selected route - or nothing.
   *
   * The information is not part of the stored trip (nothing is stored from
   * TomTom), only of the response to the last planning. It is therefore
   * looked up there, via the trip ID: that way it also appears for a single
   * route, where there are no variant cards. A trip from the list for which
   * no planning exists gets no tile - traffic from yesterday would be worse
   * than none. */
  function trafficTile(trip, variants) {
    const v = (variants || []).find((x) => x.trip_id === trip.trip_id);
    if (!v || typeof v.traffic_min !== "number") return "";
    const text = v.traffic_min >= 0.5 ? "+" + K.num(v.traffic_min, 0) + " min" : "frei";
    // From a quarter of an hour it stands out: that is the difference between
    // "a bit of traffic" and "a different arrival time".
    // Forecast or live: a number for Friday 4 pm is not a measurement from
    // now, and that should be visible.
    const variety = v.traffic_basis === "prognose" ? "Verkehr (Prognose)" : "Verkehr";
    return K.valueTile(variety + " · " + (v.traffic_source || "TomTom"), text,
                        v.traffic_min >= 15 ? "schlecht" : "");
  }

  function show(trip) {
    document.getElementById("ergebnis").hidden = false;

    const reserveVariety = trip.suffices ? "gut" : "schlecht";
    document.getElementById("kennzahlen").innerHTML = [
      K.valueTile("Strecke", K.num(trip.distance_km) + " km"),
      K.valueTile("Fahrzeit", K.duration(trip.drive_time_minutes)),
      trafficTile(trip, latestVariants),
      K.valueTile("Energie", K.num(trip.kwh_total, 1) + " kWh"),
      K.valueTile("Verbrauch", K.num(trip.consumption_kwh_100km, 1) + " kWh/100"),
      trip.suffices
        ? K.valueTile("Am Ziel", K.num(trip.soc_at_target) + " %", "gut")
        : K.valueTile("Reserve bei", K.num(trip.reserve_at_km) + " km",
                       reserveVariety),
      K.valueTile("Gerechnet bei", K.num(trip.weather.temp_c, 1) + " °C"),
    ].join("");

    if (!trip.suffices) {
      K.report(`Die Strecke ist ohne Nachladen nicht zu schaffen: Die Reserve `
        + `von ${K.num(trip.vehicle.reserve_soc)} % wird nach `
        + `${K.num(trip.reserve_at_km)} km erreicht. Der Ladeplan steht `
        + `unten.`, "warnung");
    }

    drawProfile(trip);

    window.joltMap.setRoute(trip.geometry);
    latestChargers = [];
    lastPlan = null;
    setMarker();
    window.joltMap.onRouteFit();
  }

  function setMarker() {
    const trip = K.state.trip;
    const points = (trip && trip.geometry) || [];
    if (points.length < 2) return;
    const lst = [
      { lat: points[0][1], lon: points[0][0], kind: "start", text: "Start" },
      { lat: points[points.length - 1][1], lon: points[points.length - 1][0],
        kind: "ziel", text: "Ziel" },
    ];
    if (trip.reserve_point) {
      lst.push({ lat: trip.reserve_point.lat, lon: trip.reserve_point.lon,
                   kind: "reserve",
                   text: "Reserve " + K.num(trip.reserve_point.km) + " km" });
    }

    // Planned stops last and labelled: they should cover the other charge
    // points, not the other way round.
    const planned = new Set((lastPlan && lastPlan.stops || [])
      .map((s) => s.id));
    for (const s of latestChargers) {
      if (planned.has(s.id)) continue;
      lst.push({ lat: s.lat, lon: s.lon,
                   kind: s.occupied_reported ? "saeuleBelegt" : "saeule" });
    }
    (lastPlan && lastPlan.stops || []).forEach((s, i) => {
      lst.push({ lat: s.lat, lon: s.lon, kind: "stopp",
                   text: `${i + 1}. ${K.duration(s.charge_time_minutes)}` });
    });
    window.joltMap.setMarker(lst);
  }

  /* ---------- State of charge along the route ---------- */

  function drawProfile(trip) {
    const canvas = document.getElementById("profil");
    if (!canvas) return;
    const ratio = window.devicePixelRatio || 1;
    const extent = canvas.clientWidth, elevation = canvas.clientHeight;
    canvas.width = extent * ratio;
    canvas.height = elevation * ratio;
    const pen = canvas.getContext("2d");
    pen.setTransform(ratio, 0, 0, ratio, 0, 0);
    pen.clearRect(0, 0, extent, elevation);

    const profile = trip.profile || [];
    if (profile.length < 2) return;
    const maxKm = profile[profile.length - 1].km || 1;
    const reserve = trip.vehicle.reserve_soc;

    const x = (km) => (km / maxKm) * (extent - 8) + 4;
    // Nothing is drawn below zero: a negative state of charge is not a
    // statement about the battery but about the missing charging plan.
    const y = (soc) => elevation - 6 - (Math.max(0, Math.min(100, soc)) / 100)
      * (elevation - 24);

    // Elevation profile as a muted background - it explains the kinks in the
    // SoC curve, and without this explanation they look like measurement errors.
    let maxElevation = 1;
    for (const p of profile) maxElevation = Math.max(maxElevation, p.elevation || 0);
    pen.beginPath();
    pen.moveTo(x(0), elevation);
    for (const p of profile) {
      pen.lineTo(x(p.km), elevation - ((p.elevation || 0) / maxElevation) * (elevation * 0.35));
    }
    pen.lineTo(x(maxKm), elevation);
    pen.closePath();
    pen.fillStyle = "#2a333d66";
    pen.fill();

    // Reserve line
    pen.beginPath();
    pen.moveTo(4, y(reserve));
    pen.lineTo(extent - 4, y(reserve));
    pen.strokeStyle = "#e2596a88";
    pen.setLineDash([4, 4]);
    pen.lineWidth = 1;
    pen.stroke();
    pen.setLineDash([]);

    // State of charge
    pen.beginPath();
    profile.forEach((p, i) => {
      const px = x(p.km), py = y(p.soc);
      if (i === 0) pen.moveTo(px, py); else pen.lineTo(px, py);
    });
    pen.strokeStyle = "#ffc93c";
    pen.lineWidth = 2;
    pen.stroke();

    pen.font = "11px system-ui, sans-serif";
    pen.fillStyle = "#8a97a5";
    pen.fillText(`Reserve ${reserve} %`, 6, y(reserve) - 4);
    document.getElementById("profil-legende").textContent =
      `0 – ${K.num(maxKm)} km · bis ${K.num(maxElevation)} m`;
  }

  /* ---------- Charge points ---------- */

  async function chargersCharging() {
    const trip = K.state.trip;
    const lst = document.getElementById("saeulen");
    if (!trip || !lst) return;

    lst.innerHTML = '<li class="leer">sucht …</li>';
    try {
      const response = await K.api(`/api/saeulen/entlang/${trip.trip_id}`
        + `?min_kw=${document.getElementById("min-kw").value}`
        + `&radius_km=${document.getElementById("radius").value}`);
      drawChargers(response, lst);
      latestChargers = response.candidates || [];
      setMarker();
    } catch (failure) {
      lst.innerHTML = "";
      K.report("Ladepunkte: " + failure.message, "fehler");
    }
  }

  /* ---------- Charging plan ---------- */

  /* What a stop costs before charging begins - the slider in the
   * charging plan view. If it is missing (old UI in the cache), the server's
   * default applies, instead of sending a zero and fragmenting the plan. */
  function holding_cost() { return slider("haltekosten", 5); }

  function slider(id, preset) {
    const el = document.getElementById(id);
    return el ? el.value : preset;
  }

  async function chargePlanCharging() {
    const trip = K.state.trip;
    const lst = document.getElementById("ladeplan");
    const vals = document.getElementById("ladeplan-werte");
    if (!trip || !lst || !vals) return;

    lst.innerHTML = '<li class="leer">plant …</li>';
    vals.innerHTML = "";
    try {
      const plan = await K.api(`/api/fahrten/${trip.trip_id}/ladeplan`
        + `?min_kw=${document.getElementById("min-kw").value}`
        + `&radius_km=${document.getElementById("radius").value}`
        + `&stop_fixed_cost_min=${holding_cost()}`
        + `&charge_park_bonus_min=${slider("ladepark", 4)}`
        + `&time_value_eur_h=${slider("zeitwert", 30)}`,
        { method: "POST" });
      lastPlan = plan;
      drawPlan(plan, lst, vals);
      setMarker();
    } catch (failure) {
      lst.innerHTML = "";
      lastPlan = null;
      K.report("Ladeplan: " + failure.message, "fehler");
    }
  }

  function drawPlan(plan, lst, vals) {
    if (!plan.feasible) {
      lst.innerHTML = `<li class="leer">${sanitize(plan.reason)}</li>`;
      return;
    }

    vals.innerHTML = [
      K.valueTile("Gesamt", K.duration(plan.total_minutes)),
      K.valueTile("davon Laden", K.duration(plan.charge_time_minutes)),
      K.valueTile("davon Umwege", K.duration(plan.detour_time_minutes)),
      // Make visible what the mere number of stops costs - otherwise the slider
      // next to it is a number without effect that you can see.
      K.valueTile("davon Halte", K.duration(plan.holding_cost_minutes)),
      // The second yardstick next to time. Without it the time-value slider
      // would be a setting whose effect you cannot see.
      K.valueTile("Stromkosten", K.num(plan.cost_eur, 2) + " €"),
      K.valueTile("Stopps", String(plan.stop_count)),
      K.valueTile("Am Ziel", K.num(plan.soc_at_target) + " %",
                   plan.soc_at_target >= 15 ? "gut" : ""),
    ].join("");

    if (!plan.stop_count) {
      lst.innerHTML = '<li class="leer">Kein Ladestopp nötig – die Strecke '
        + 'reicht mit dem Ladestand beim Start.</li>';
      return;
    }

    lst.innerHTML = "";
    plan.stops.forEach((s, i) => {
      const entry = document.createElement("li");
      // The fallback location is the reason you do not have to search anew in
      // front of an occupied charger - it belongs visibly at the stop, not in a
      // submenu. If it is missing, that too is a statement.
      //
      // How urgent it is depends on the stop itself: a charging park with twelve
      // points hardly needs a fallback plan, a single charger does.
      // That is why the missing fallback is red only where it really hurts -
      // otherwise the warning would be read at every stop and thus at
      // none.
      const tight = (s.point_count || 1) < 4;
      const detour_alt = s.detour_alt
        ? `<div class="unter">Ausweich: ${sanitize(s.detour_alt.name
            || s.detour_alt.operator || "Ladepunkt")} bei km
            ${K.num(s.detour_alt.km_on_route)} – Ankunft mit
            ${K.num(s.detour_alt.arrival_soc)} %</div>`
        : `<div class="unter"${tight ? ' style="color:#e2596a"' : ""}>Kein
            Ausweichstandort ohne Nachladen erreichbar${tight
              ? " – und hier stehen nur wenige Ladepunkte" : ""}</div>`;

      entry.innerHTML = `
        <div class="haupt">
          <div class="titel">${i + 1}. ${sanitize(s.name || s.operator
            || "Ladepunkt")}</div>
          <div class="unter">km ${K.num(s.km_on_route)} · nach
            ${K.duration(s.arrival_minute)} · ${K.num(s.arrival_soc)} %
            → ${K.num(s.departure_soc)} % · ${K.num(s.kwh_charged, 1)} kWh
            · Umweg ${K.num(s.detour_minutes, 1)} min
            · ${s.point_count} Ladepunkte${
              s.cost_eur ? " · " + K.num(s.cost_eur, 2) + " €" : ""}</div>
          ${detour_alt}
        </div>
        <div class="kw">${K.duration(s.charge_time_minutes)}</div>`;
      lst.appendChild(entry);
    });
  }

  function drawChargers(response, lst) {
    if (!response.count) {
      lst.innerHTML = '<li class="leer">Keine passenden Ladepunkte gefunden. '
        + 'Ist das Ladesäulenregister schon importiert? '
        + '(tools/import_bnetza.py)</li>';
      return;
    }

    lst.innerHTML = "";
    for (const k of response.candidates.slice(0, 60)) {
      const entry = document.createElement("li");
      const occupied = k.occupied_reported
        ? ' · <span style="color:#e2596a">als belegt gemeldet</span>' : "";
      entry.innerHTML = `
        <div class="haupt">
          <div class="titel">${sanitize(k.name || k.operator || "Ladepunkt")}</div>
          <div class="unter">km ${K.num(k.km_on_route)} · Umweg
            ${K.num(k.detour_minutes, 1)} min · ${k.point_count} Ladepunkte${
              accessHint(k)}
            · ${sanitize(k.connector_types)}${occupied}</div>
        </div>
        <div class="kw">${K.num(k.max_kw)} kW</div>`;

      const btn = document.createElement("button");
      btn.className = "tat neben";
      btn.style.width = "auto";
      btn.style.margin = "0";
      btn.style.padding = "6px 10px";
      btn.style.fontSize = "12px";
      btn.textContent = k.occupied_reported ? "frei" : "belegt";
      btn.addEventListener("click", async () => {
        try {
          await K.api(`/api/saeulen/${k.id}/belegt`,
                      { method: k.occupied_reported ? "DELETE" : "POST" });
          await chargersCharging();
          // "Everything here is full" is the only availability information that
          // is really true - it must change the plan, not just colour the
          // list.
          await chargePlanCharging();
        } catch (failure) { K.report(failure.message, "fehler"); }
      });
      entry.appendChild(btn);
      lst.appendChild(entry);
    }
  }

  /* The names come from third-party data sources and end up in innerHTML. */
  function sanitize(text) {
    const helper = document.createElement("div");
    helper.textContent = text || "";
    return helper.innerHTML;
  }

  /* ---------- Payload ---------- */

  /* The slider treats a value below its minimum as "not set". That saves a
   * second switch for the question "own payload or the one from the
   * profile?" - far left means profile. */
  function payloadValue() {
    const slider = document.getElementById("zuladung");
    if (!slider) return null;
    const val = Number(slider.value);
    return val < 0 ? null : val;
  }

  function payloadCouple() {
    const slider = document.getElementById("zuladung");
    const display = document.getElementById("zuladung-wert");
    if (!slider || !display) return;
    const refresh = () => {
      const val = payloadValue();
      display.textContent = val === null ? "wie im Profil" : val + " kg";
    };
    slider.addEventListener("input", refresh);
    refresh();
  }

  /* ---------- Departure time ---------- */

  /* "Fr., 09.10., 16:00" in the device's local time. */
  function departureText(iso) {
    const timestamp = new Date(iso);
    if (Number.isNaN(timestamp.getTime())) return iso;
    return timestamp.toLocaleString("de-DE", { weekday: "short", day: "2-digit",
                                          month: "2-digit", hour: "2-digit",
                                          minute: "2-digit" });
  }

  /* The field delivers local time without a zone ("2026-10-09T16:00"). The
   * server needs a point in time: `new Date` reads the text as this
   * device's local time, `toISOString` turns it into UTC with Z - and thus
   * the zone can no longer be confused. Empty or unreadable means "now". */
  function departureIso(val) {
    if (!val) return null;
    const timestamp = new Date(val);
    return Number.isNaN(timestamp.getTime()) ? null : timestamp.toISOString();
  }

  /* "2026-10-09T16:00" for `min` and `max` of the field, in local time. */
  function localForField(timestamp) {
    const z = (n) => String(n).padStart(2, "0");
    return `${timestamp.getFullYear()}-${z(timestamp.getMonth() + 1)}-${z(timestamp.getDate())}`
      + `T${z(timestamp.getHours())}:${z(timestamp.getMinutes())}`;
  }

  /* The server also rejects the past and more than 60 days ahead; the field
   * shows it already when selecting. */
  function departureLimits() {
    const field = document.getElementById("abfahrt");
    if (!field) return;
    const now_ts = new Date();
    field.min = localForField(now_ts);
    field.max = localForField(new Date(now_ts.getTime() + 60 * 24 * 3600 * 1000));
  }

  /* ---------- Trailer and maximum speed ---------- */

  /* An empty field means "not set" and goes out as null - not as 0, which
   * the server would read as "zero km/h". */
  function numberOrNull(id) {
    const el = document.getElementById(id);
    if (!el || el.value === "") return null;
    const val = Number(el.value);
    return Number.isFinite(val) ? val : null;
  }

  function trailerValues() {
    const kg = numberOrNull("anhaenger-kg");
    if (!kg) return { trailer_kg: null, trailer_cwa_m2: null };
    return { trailer_kg: kg, trailer_cwa_m2: numberOrNull("anhaenger-cwa") || 0 };
  }

  /* The selection prefills the two fields; they stay editable. Anyone who
   * picks a trailer and has not yet entered a limit gets 100 km/h - the
   * limit for a combination in Germany, and the one thing you
   * otherwise forget. */
  function trailerCouple() {
    const choice = document.getElementById("anhaenger");
    const fields = document.getElementById("anhaenger-werte");
    if (!choice || !fields) return;
    choice.addEventListener("change", () => {
      const kg = document.getElementById("anhaenger-kg");
      const cwa = document.getElementById("anhaenger-cwa");
      const speedMax = document.getElementById("tempo-max");
      if (choice.value === "") {
        fields.hidden = true;
        kg.value = ""; cwa.value = "";
        return;
      }
      fields.hidden = false;
      if (choice.value !== "eigen") {
        const [mass, area] = choice.value.split(",");
        kg.value = mass;
        cwa.value = area;
      }
      if (speedMax && speedMax.value === "") speedMax.value = "100";
    });
  }

  /* ---------- Set up ---------- */

  function set_up() {
    placeSearchSetUp("start", "start-treffer", "start");
    placeSearchSetUp("ziel", "ziel-treffer", "ziel");
    K.sliderCouple("start-soc", "start-soc-wert");
    K.sliderCouple("tempo", "tempo-wert");
    payloadCouple();
    trailerCouple();
    departureLimits();
    const departureField = document.getElementById("abfahrt");
    // Anyone touching the field again after an hour's break should not work
    // with the limits from before.
    if (departureField) departureField.addEventListener("focus", departureLimits);

    let wait = null;
    let waitPlan = null;
    const newCharging = () => {
      clearTimeout(wait);
      // Both sliders act on the same candidate set - the charging plan has to
      // follow, otherwise the map shows stops that no longer exist after the
      // new filtering.
      wait = setTimeout(async () => {
        await chargersCharging();
        await chargePlanCharging();
      }, 350);
    };
    K.sliderCouple("min-kw", "min-kw-wert", newCharging);
    K.sliderCouple("radius", "radius-wert", newCharging);
    // The effort per stop only changes the planning, not the candidates -
    // hence without chargersCharging(), otherwise the list flickers for no reason.
    // Both sliders only change the planning, not the candidates - hence
    // without chargersCharging(), otherwise the list flickers for no reason.
    const catchUpPlan = () => {
      clearTimeout(waitPlan);
      waitPlan = setTimeout(chargePlanCharging, 350);
    };
    K.sliderCouple("haltekosten", "haltekosten-wert", catchUpPlan);
    K.sliderCouple("ladepark", "ladepark-wert", catchUpPlan);
    K.sliderCouple("zeitwert", "zeitwert-wert", catchUpPlan);

    K.at("rechnen", "click", compute);
    window.addEventListener("resize", () => {
      if (K.state.trip) drawProfile(K.state.trip);
    });
  }

  return { set_up, show, trafficTile, departureIso, localForField, chargersCharging, chargePlanCharging, variantChoose,
           tripCharging };
})();
