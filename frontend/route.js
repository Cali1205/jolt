/* Die Planen-Ansicht: Orte suchen, Route rechnen, Ergebnis zeigen. */
window.joltRoute = (function () {
  "use strict";

  const K = window.jolt;
  const chosen = { start: null, destination: null };
  // Was gerade auf der Karte liegt. Säulenliste und Ladeplan werden getrennt
  // geladen, zeichnen aber in dieselbe Karte - ohne diesen gemeinsamen Stand
  // löscht der eine die Marker des anderen.
  let latestChargers = [];
  let lastPlan = null;
  // Die Varianten der letzten Berechnung (schnellste/kürzeste/empfohlene,
  // ggf. zusammengelegt) - für die Wechsel-Karten und um beim Tippen auf eine
  // Karte zu wissen, welcher fahrt_id sie entspricht.
  let latestVariants = [];
  let latestDeparture = null;       // wofür der Verkehr gilt: null = jetzt

  /* ---------- Ortssuche ---------- */

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
      // Getippt wird schneller als geantwortet. Ohne diese Pause schickt
      // jedes Zeichen eine Anfrage - beim freien ORS-Kontingent von 2500
      // Anfragen am Tag ist das der schnellste Weg, es aufzubrauchen.
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

  /* ---------- Route rechnen ---------- */

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
      // Eine gerechnete Route wird als Fahrt abgelegt - die Liste im Reiter
      // "Fahrten" ist damit nicht mehr aktuell.
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
        // Der Regler steht ganz links auf einem negativen Wert - das ist
        // "nicht gesetzt", und dann gilt das Fahrzeugprofil. Ein eigener
        // Schalter daneben wäre ein zweites Bedienelement für eine Frage,
        // die der Regler schon beantwortet.
        payload_kg: payloadValue(),
        ...trailerValues(),
        speed_max_kmh: numberOrNull("tempo-max"),
      }});
      latestVariants = response.variants || [];
      latestDeparture = response.departure || null;
      drawVariants();
      // Die sparsamste Variante ist jolts Grundhaltung - sie wird
      // vorausgewählt, ein Tippen auf eine andere Karte wechselt.
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

  /* Eine gespeicherte Fahrt laden und zeichnen.
   *
   * Der Endpunkt liefert dieselbe Form wie eine frische Variante, deshalb
   * genügt derselbe Weg wie nach dem Rechnen. Die Varianten-Karten bleiben
   * leer: Zu einer einzeln geladenen Fahrt gibt es keine Geschwister mehr -
   * die anderen Varianten von damals sind eigene Fahrten mit eigener ID. */
  async function tripCharging(tripId) {
    const trip = await K.api("/api/fahrten/" + tripId);
    latestVariants = [];
    drawVariants();
    K.state.trip = trip;
    show(trip);
    await chargersCharging();
    await chargePlanCharging();
  }

  /* ---------- Varianten-Karten ---------- */

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
      /* Die Kennwerte zeigen, was zählt: die Zeit **inklusive Laden** und
       * die Kosten. Vorher standen dort Fahrzeit und kWh - beides sagt
       * nichts darüber, wann man ankommt, wenn zweimal geladen werden muss. */
      /* Mit Verkehr, wenn TomTom ihn geliefert hat: Die Rangfolge rechnet
       * genauso, und die Zahl am Etikett muss zu ihr passen. */
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
    /* Die Quelle nennen: Der Verkehr kommt von TomTom, und wer eine Zahl sieht,
     * soll wissen, woher sie ist. */
    if (latestVariants.some((v) => v.traffic_source)) {
      const source = document.createElement("div");
      source.className = "unter";
      source.textContent = latestDeparture
        ? "Verkehr: TomTom, Prognose für " + departureText(latestDeparture) + " · Wetter: Vorhersage für diese Zeit"
        : "Verkehr: TomTom, Stand jetzt";
      lst.appendChild(source);
    }
  }

  /* Zugangsbeschränkungen sichtbar machen.
   *
   * Ein Ladepunkt kann tadellos aussehen - 300 kW, acht Säulen, kein Umweg -
   * und trotzdem unbrauchbar sein, weil er hinter einer Schranke steht oder
   * nur Hotelgästen offensteht. jolt kennt diese Angaben seit dem Umbau des
   * OCM-Imports; sie zu haben und nicht zu zeigen wäre die schlechteste
   * aller Möglichkeiten. */
  function accessHint(k) {
    const parts = [];
    if (k.access && !/^public$/i.test(k.access)) parts.push(sanitize(k.access));
    if (k.membership_required) parts.push("Mitgliedschaft nötig");
    const h = k.hints || {};
    if (h.cost) parts.push(sanitize(String(h.cost).slice(0, 40)));
    if (h.access) parts.push(sanitize(String(h.access).slice(0, 60)));
    return parts.length ? ` · <span style="color:var(--warnung)">${parts.join(" · ")}</span>` : "";
  }

  /* ---------- Ergebnis ---------- */

  /* Die Verkehrskachel der gewählten Route - oder nichts.
   *
   * Die Angabe liegt nicht an der gespeicherten Fahrt (von TomTom wird nichts
   * gespeichert), sondern nur in der Antwort der letzten Planung. Gesucht
   * wird sie deshalb dort, über die Fahrt-ID: So erscheint sie auch bei
   * einer einzelnen Route, wo es keine Variantenkarten gibt. Eine Fahrt aus
   * der Liste, zu der keine Planung vorliegt, bekommt keine Kachel - ein
   * Verkehr von gestern wäre schlimmer als keiner. */
  function trafficTile(trip, variants) {
    const v = (variants || []).find((x) => x.trip_id === trip.trip_id);
    if (!v || typeof v.traffic_min !== "number") return "";
    const text = v.traffic_min >= 0.5 ? "+" + K.num(v.traffic_min, 0) + " min" : "frei";
    // Ab einer Viertelstunde fällt es auf: Das ist der Unterschied zwischen
    // "ein bisschen Verkehr" und "eine andere Ankunftszeit".
    // Prognose oder live: Eine Zahl für Freitag 16 Uhr ist keine Messung von
    // jetzt, und das soll man ihr ansehen.
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

    // Geplante Stopps zuletzt und beschriftet: Sie sollen die übrigen
    // Ladepunkte überdecken, nicht umgekehrt.
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

  /* ---------- Ladestand über der Strecke ---------- */

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
    // Unter null wird nicht gezeichnet: Ein negativer Ladestand ist keine
    // Aussage über den Akku, sondern über die fehlende Ladeplanung.
    const y = (soc) => elevation - 6 - (Math.max(0, Math.min(100, soc)) / 100)
      * (elevation - 24);

    // Höhenprofil als gedämpfter Hintergrund - es erklärt die Knicke in der
    // SoC-Kurve, und ohne diese Erklärung wirken sie wie Messfehler.
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

    // Reservelinie
    pen.beginPath();
    pen.moveTo(4, y(reserve));
    pen.lineTo(extent - 4, y(reserve));
    pen.strokeStyle = "#e2596a88";
    pen.setLineDash([4, 4]);
    pen.lineWidth = 1;
    pen.stroke();
    pen.setLineDash([]);

    // Ladestand
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

  /* ---------- Ladepunkte ---------- */

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

  /* ---------- Ladeplan ---------- */

  /* Was ein Halt kostet, bevor geladen wird - der Regler in der
   * Ladeplan-Ansicht. Fehlt er (alte Oberfläche im Cache), gilt die Vorgabe
   * des Servers, statt eine Null zu schicken und den Plan zu zersplittern. */
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
      // Sichtbar machen, was die blosse Anzahl der Halte kostet - sonst ist
      // der Regler daneben eine Zahl ohne Wirkung, die man sehen kann.
      K.valueTile("davon Halte", K.duration(plan.holding_cost_minutes)),
      // Der zweite Massstab neben der Zeit. Ohne ihn wäre der Zeitwert-Regler
      // eine Einstellung, deren Wirkung man nicht sieht.
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
      // Der Ausweichstandort ist der Grund, warum man vor einer belegten Säule
      // nicht neu suchen muss - er gehört sichtbar an den Stopp, nicht in ein
      // Untermenü. Fehlt er, ist auch das eine Aussage.
      //
      // Wie dringend sie ist, hängt am Stopp selbst: Ein Ladepark mit zwölf
      // Punkten braucht kaum einen Rückfallplan, ein einzelner Lader schon.
      // Deshalb ist die fehlende Ausweichmöglichkeit nur dort rot, wo sie
      // wirklich weh tut - sonst wäre die Warnung an jedem Stopp zu lesen und
      // damit an keinem.
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
          // "Hier ist alles voll" ist die einzige Verfügbarkeitsinformation,
          // die wirklich stimmt - sie muss den Plan ändern, nicht nur die
          // Liste einfärben.
          await chargePlanCharging();
        } catch (failure) { K.report(failure.message, "fehler"); }
      });
      entry.appendChild(btn);
      lst.appendChild(entry);
    }
  }

  /* Die Namen kommen aus fremden Datenquellen und landen in innerHTML. */
  function sanitize(text) {
    const helper = document.createElement("div");
    helper.textContent = text || "";
    return helper.innerHTML;
  }

  /* ---------- Zuladung ---------- */

  /* Der Regler kennt einen Wert unterhalb seines Minimums als "nicht
   * gesetzt". Das erspart einen zweiten Schalter für die Frage "eigene
   * Zuladung oder die aus dem Profil?" - ganz links heisst Profil. */
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

  /* ---------- Abfahrtszeit ---------- */

  /* "Fr., 09.10., 16:00" in der Ortszeit des Geräts. */
  function departureText(iso) {
    const timestamp = new Date(iso);
    if (Number.isNaN(timestamp.getTime())) return iso;
    return timestamp.toLocaleString("de-DE", { weekday: "short", day: "2-digit",
                                          month: "2-digit", hour: "2-digit",
                                          minute: "2-digit" });
  }

  /* Das Feld liefert Ortszeit ohne Zone ("2026-10-09T16:00"). Der Server
   * braucht einen Zeitpunkt: `new Date` liest den Text als Ortszeit dieses
   * Geräts, `toISOString` macht daraus UTC mit Z - und damit ist die Zone
   * nicht mehr zu verwechseln. Leer oder unlesbar heisst "jetzt". */
  function departureIso(val) {
    if (!val) return null;
    const timestamp = new Date(val);
    return Number.isNaN(timestamp.getTime()) ? null : timestamp.toISOString();
  }

  /* "2026-10-09T16:00" für `min` und `max` des Felds, in Ortszeit. */
  function localForField(timestamp) {
    const z = (n) => String(n).padStart(2, "0");
    return `${timestamp.getFullYear()}-${z(timestamp.getMonth() + 1)}-${z(timestamp.getDate())}`
      + `T${z(timestamp.getHours())}:${z(timestamp.getMinutes())}`;
  }

  /* Vergangenheit und mehr als 60 Tage lehnt auch der Server ab; das Feld
   * zeigt es schon beim Auswählen. */
  function departureLimits() {
    const field = document.getElementById("abfahrt");
    if (!field) return;
    const now_ts = new Date();
    field.min = localForField(now_ts);
    field.max = localForField(new Date(now_ts.getTime() + 60 * 24 * 3600 * 1000));
  }

  /* ---------- Anhänger und Höchstgeschwindigkeit ---------- */

  /* Ein leeres Feld heisst "nicht gesetzt" und geht als null hinaus - nicht
   * als 0, das der Server als "null km/h" läse. */
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

  /* Die Auswahl füllt die beiden Felder vor; sie bleiben änderbar. Wer einen
   * Anhänger wählt und noch keine Grenze eingetragen hat, bekommt 100 km/h -
   * die Grenze für ein Gespann in Deutschland, und das, was man sonst
   * vergisst. */
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

  /* ---------- Einrichten ---------- */

  function set_up() {
    placeSearchSetUp("start", "start-treffer", "start");
    placeSearchSetUp("ziel", "ziel-treffer", "ziel");
    K.sliderCouple("start-soc", "start-soc-wert");
    K.sliderCouple("tempo", "tempo-wert");
    payloadCouple();
    trailerCouple();
    departureLimits();
    const departureField = document.getElementById("abfahrt");
    // Wer das Feld nach einer Stunde Pause wieder anfasst, soll nicht mit den
    // Grenzen von vorhin arbeiten.
    if (departureField) departureField.addEventListener("focus", departureLimits);

    let wait = null;
    let waitPlan = null;
    const newCharging = () => {
      clearTimeout(wait);
      // Beide Regler wirken auf denselben Kandidatensatz - der Ladeplan muss
      // mitziehen, sonst zeigt die Karte Stopps, die es nach der neuen
      // Filterung gar nicht mehr gibt.
      wait = setTimeout(async () => {
        await chargersCharging();
        await chargePlanCharging();
      }, 350);
    };
    K.sliderCouple("min-kw", "min-kw-wert", newCharging);
    K.sliderCouple("radius", "radius-wert", newCharging);
    // Der Aufwand je Halt ändert nur die Planung, nicht die Kandidaten -
    // deshalb ohne saeulenLaden(), sonst flackert die Säulenliste ohne Grund.
    // Beide Regler ändern nur die Planung, nicht die Kandidaten - deshalb
    // ohne saeulenLaden(), sonst flackert die Säulenliste ohne Grund.
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
