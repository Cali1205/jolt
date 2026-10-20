/* Maintain vehicle profiles: physics parameters and charging curve.
 *
 * The fields are deliberately physical and not "consumption in kWh/100 km".
 * Only this way can one answer what 130 instead of 110 km/h costs or what a
 * pass consumes. So that this does not put anyone off on the first day, there
 * are templates - and after that the correction factor pulls the values
 * towards one's own car anyway.
 */
window.joltVehicle = (function () {
  "use strict";

  const K = window.jolt;
  let templates = [];

  const FIELDS = [
    { name: "name", title: "Name", kind: "text" },
    { name: "battery_gross_kwh", title: "Akku brutto (kWh)", step: 0.1 },
    { name: "battery_net_kwh", title: "Akku nutzbar (kWh)", step: 0.1,
      hint: "Gerechnet wird ausschliesslich mit netto." },
    { name: "curb_mass_kg", title: "Leergewicht (kg)", step: 10 },
    { name: "payload_kg", title: "Zuladung (kg)", step: 10 },
    { name: "c_w", title: "cw-Wert", step: 0.001 },
    { name: "frontal_area_m2", title: "Stirnfläche (m²)", step: 0.01 },
    { name: "c_rr", title: "Rollwiderstand", step: 0.001 },
    { name: "max_speed_kmh", title: "Höchstgeschwindigkeit (km/h)", step: 1,
      optional: true,
      hint: "Leer = keine Grenze im Modell. Der Tempo-Regler der Planung "
        + "stösst sonst nirgends an und rechnet bei 130 % mit 165 km/h." },
    { name: "eta_drive", title: "Wirkungsgrad Antrieb", step: 0.01 },
    { name: "eta_regen", title: "Wirkungsgrad Rekuperation", step: 0.01 },
    { name: "p_aux_w", title: "Grundlast (W)", step: 10 },
    { name: "heat_pump", title: "Wärmepumpe", kind: "checkbox",
      hint: "Halbiert grob den Heizbedarf – im Winter der grösste "
        + "Einzelunterschied." },
    { name: "reserve_soc", title: "Reserve (%)", step: 1 },
    { name: "target_soc", title: "Ladestand am Ziel (%)", step: 1 },
    { name: "max_charge_power_kw", title: "Max. Ladeleistung (kW)", step: 1 },
    { name: "connector_type", title: "Steckertyp", kind: "auswahl",
      vals: ["CCS", "Typ2", "CHAdeMO"] },
    /* The dongle profiles are the frontend's own knowledge (platforms/*.js),
     * so the choice is built from them. A vehicle without the field - old
     * cached data - is a MEB, the only thing the dongle could talk to before. */
    { name: "platform", title: "Plattform (OBD2)", kind: "auswahl",
      standard: "meb", wide: true,
      vals: ((window.joltObd && window.joltObd.platforms()) || [])
        .map((p) => ({ value: p.id, label: p.title })),
      hint: "Bestimmt, welche Steuergeräte-Adressen und PIDs der Dongle "
        + "abfragt. Unbekannte Fahrzeuge: Standard-OBD2 liefert nur, was der "
        + "Gesetzgeber vorschreibt." },
    { name: "preferred_operators", title: "Bevorzugte Ladeanbieter", kind: "liste",
      hint: "Namen oder Namensteile, durch Komma getrennt - z.B. "
        + "\"EnBW, Ionity\". Kein Ausschluss anderer Anbieter, nur ein "
        + "Vorteil bei der Stoppwahl." },
  ];

  /* Two groups: what everybody sets (battery, charging) is open, the
   * physics of the car sits folded behind it. Hints are not gone but behind
   * an (i); numbers stand two to a row. */
  const PHYSICS = ["curb_mass_kg", "payload_kg", "c_w", "frontal_area_m2", "c_rr",
                   "max_speed_kmh", "eta_drive", "eta_regen", "p_aux_w", "heat_pump"];

  function fieldHtml(field) {
    const id = "veh-" + field.name;
    const info = field.hint
      ? ` <button type="button" class="info" data-info="hint-${id}" aria-expanded="false" aria-label="Erklärung">i</button>` : "";
    const hint = field.hint ? `<p class="hilfe" id="hint-${id}" hidden>${field.hint}</p>` : "";
    if (field.kind === "checkbox") {
      return `<div class="schalter-zeile feld-breit"><label for="${id}">${field.title}${info}</label>`
        + `<input type="checkbox" id="${id}" class="schalter"></div>${hint}`;
    }
    let user_input;
    if (field.kind === "auswahl") {
      user_input = `<select id="${id}">`
        + field.vals.map((w) => {
          const option = typeof w === "string" ? { value: w, label: w } : w;
          return `<option value="${K.esc(option.value)}">${K.esc(option.label)}</option>`;
        }).join("")
        + `</select>`;
    } else if (field.kind === "text" || field.kind === "liste") {
      user_input = `<input type="text" id="${id}">`;
    } else {
      user_input = `<input type="number" id="${id}" step="${field.step}">`;
    }
    const wide = field.kind === "text" || field.kind === "liste" || field.wide
      ? " feld-breit" : "";
    return `<div class="feld${wide}"><label for="${id}">${field.title}${info}</label>${user_input}${hint}</div>`;
  }

  function formBuild() {
    const container = document.getElementById("vehicle-form");
    const base = FIELDS.filter((f) => !PHYSICS.includes(f.name));
    const physics = FIELDS.filter((f) => PHYSICS.includes(f.name));
    container.innerHTML = `<div class="formgitter">${base.map(fieldHtml).join("")}</div>`
      + `<details class="opt"><summary><span class="opt-titel">Fahrphysik</span>`
      + `<span class="chips"><span class="chip">Masse · cw · Wirkungsgrad</span></span></summary>`
      + `<div class="opt-inhalt"><div class="formgitter">${physics.map(fieldHtml).join("")}</div></div></details>`;
  }

  /* State of health: what the car itself reports as battery capacity,
   * against the profile value (new). Only shown once a measurement exists;
   * a curve over the odometer as soon as there are two. */
  function showHealth(vehicle) {
    const box = document.getElementById("vehicle-health");
    if (!box) return;
    if (!vehicle || typeof vehicle.soh_percent !== "number") { box.hidden = true; return; }
    const date = vehicle.capacity_measured_at
      ? new Date(vehicle.capacity_measured_at).toLocaleDateString("de-DE") : "";
    const colour = vehicle.soh_percent >= 90 ? "gut" : (vehicle.soh_percent >= 80 ? "warnung" : "schlecht");
    const history = (vehicle.capacity_history || []).filter((h) => typeof h.odometer_km === "number");
    let curve = "";
    if (history.length >= 2) {
      const xs = history.map((h) => h.odometer_km), ys = history.map((h) => h.kwh);
      const x0 = Math.min(...xs), x1 = Math.max(...xs) || 1;
      const y0 = Math.min(...ys), y1 = Math.max(...ys);
      const px = (x) => 4 + (x1 === x0 ? 0 : (x - x0) / (x1 - x0)) * 292;
      const py = (y) => 36 - (y1 === y0 ? 0.5 : (y - y0) / (y1 - y0)) * 30 - 3;
      curve = `<svg viewBox="0 0 300 40" style="width:100%;height:40px;margin-top:8px">`
        + `<polyline fill="none" stroke="#ffc93c" stroke-width="2" points="${
            history.map((h) => px(h.odometer_km).toFixed(1) + "," + py(h.kwh).toFixed(1)).join(" ")}"/></svg>`
        + `<div class="unter">kWh über Kilometerstand · ${K.num(x0)} bis ${K.num(x1)} km</div>`;
    }
    box.hidden = false;
    box.innerHTML = `<div class="werte gross">${[
      K.valueTile("Akku-Gesundheit", K.num(vehicle.soh_percent, 1) + " %", colour),
      K.valueTile("Kapazität", K.num(vehicle.measured_capacity_kwh, 1) + " kWh"),
      K.valueTile("Neu", K.num(vehicle.battery_net_kwh, 0) + " kWh"),
    ].join("")}</div>`
      + `<div class="unter" style="margin-top:6px">Vom Auto gemeldet${date ? ", zuletzt am " + date : ""}`
      + ` · ${(vehicle.capacity_history || []).length} Messungen gespeichert</div>` + curve;
  }

  /* What a manufacturer cloud last heard from the car (backend app/cloud/).
   * Only there when the server fetches it; a 404 or any failure hides the
   * card. The values are catalog signals (backend app/signals.py). */
  const CHARGE_STATES = {
    charging: "lädt", ready: "ladebereit", not_ready: "nicht ladebereit",
    purpose_reached: "Ladeziel erreicht", conservation: "Erhaltungsladung",
    error: "Fehler",
  };

  function chargeState(state) {
    return CHARGE_STATES[state] || state;
  }

  function ago(ms) {
    const min = Math.max(0, Math.round((Date.now() - ms) / 60000));
    if (min < 60) return `vor ${min} min`;
    if (min < 48 * 60) return `vor ${Math.round(min / 60)} h`;
    return `vor ${Math.round(min / 1440)} Tagen`;
  }

  function portalHtml(snap) {
    const v = snap.values || {};
    const has = (name) => v[name] !== undefined && v[name] !== null;
    const tiles = [];
    if (has("soc_pct")) tiles.push(K.valueTile("Ladestand", K.num(v.soc_pct) + " %"));
    if (has("range_km")) tiles.push(K.valueTile("Reichweite", K.num(v.range_km) + " km"));
    if (has("odometer_km")) tiles.push(K.valueTile("Kilometerstand", K.num(v.odometer_km) + " km"));
    const rows = [];
    const row = (name, text) => rows.push(`<dt>${name}</dt><dd>${K.esc(text)}</dd>`);
    if (has("energy_kwh")) {
      row("Energie", K.num(v.energy_kwh, 1)
        + (has("capacity_kwh") ? " / " + K.num(v.capacity_kwh, 1) : "") + " kWh");
    }
    if (has("batt_temp_min_c") || has("batt_temp_max_c")) {
      const low = has("batt_temp_min_c") ? v.batt_temp_min_c : v.batt_temp_max_c;
      const high = has("batt_temp_max_c") ? v.batt_temp_max_c : v.batt_temp_min_c;
      row("Batterie", low === high ? K.num(low, 1) + " °C"
        : K.num(low, 1) + "–" + K.num(high, 1) + " °C");
    }
    if (has("charge_state")) row("Ladezustand", chargeState(String(v.charge_state)));
    const at = K.timestamp(snap.captured_at);
    const as_of = at
      ? `Stand: ${at.toLocaleString("de-DE", { day: "2-digit", month: "2-digit",
          hour: "2-digit", minute: "2-digit" })}, ${ago(at.getTime())}` : "";
    return `<h2>${K.esc(snap.source_label || snap.source || "Hersteller-Cloud")}</h2>`
      + (tiles.length ? `<div class="werte gross">${tiles.join("")}</div>` : "")
      + (rows.length ? `<dl class="einst-liste" style="margin-top:10px">${rows.join("")}</dl>` : "")
      + `<div class="unter">${as_of}</div>`;
  }

  async function showPortal(vehicle) {
    const box = document.getElementById("vehicle-portal");
    if (!box) return;
    box.hidden = true;
    if (!vehicle || !vehicle.id) return;
    let snap;
    try {
      snap = await K.api(`/api/vehicles/${vehicle.id}/snapshot`, { timeout: 8000 });
    } catch (failure) {
      return;
    }
    // The choice may have moved on while the answer was under way.
    if (document.getElementById("vehicle-list").value !== String(vehicle.id)) return;
    box.innerHTML = portalHtml(snap);
    box.hidden = false;
  }

  /* Which of the signals jolt needs or uses this car has delivered, and
   * from where (backend app/signals.py, docs/data-model.md). */
  function signalsHtml(answer) {
    const rows = (answer.signals || []).filter((s) => s.tier !== "optional").map((s) => {
      const missing = !s.seen;
      const mark = missing ? "–" : "✓";
      const colour = missing ? (s.tier === "required" ? "var(--schlecht)" : "var(--gedaempft)")
        : "var(--gut)";
      const at = s.last_at ? K.timestamp(s.last_at) : null;
      const where = missing
        ? (s.tier === "required" ? "fehlt – ohne ihn keine Aufzeichnung" : "nicht gesehen")
        : s.sources.map((x) => x.label).join(", ") + (at ? ` · ${ago(at.getTime())}` : "");
      return `<dt><span style="color:${colour};display:inline-block;width:1.2em">${mark}</span>`
        + `${K.esc(s.label)}${s.tier === "required" ? " *" : ""}</dt>`
        + `<dd${missing && s.tier === "required" ? ' style="color:var(--schlecht)"' : ""}>`
        + `${K.esc(where)}</dd>`;
    });
    const optional = (answer.signals || []).filter((s) => s.tier === "optional");
    const extra = optional.filter((s) => s.seen).length;
    return `<h2>Daten</h2>`
      + `<dl class="einst-liste">${rows.join("")}</dl>`
      + `<div class="unter">* nötig, die anderen machen Planung und Fahrtenbuch genauer`
      + ` · ${extra} von ${optional.length} weiteren Werten geliefert</div>`;
  }

  async function showSignals(vehicle) {
    const box = document.getElementById("vehicle-signals");
    if (!box) return;
    box.hidden = true;
    if (!vehicle || !vehicle.id) return;
    let answer;
    try {
      answer = await K.api(`/api/vehicles/${vehicle.id}/signals`, { timeout: 8000 });
    } catch (failure) {
      return;
    }
    if (document.getElementById("vehicle-list").value !== String(vehicle.id)) return;
    box.innerHTML = signalsHtml(answer);
    box.hidden = false;
  }

  function fillForm(vehicle) {
    showHealth(vehicle);
    for (const field of FIELDS) {
      const el = document.getElementById("veh-" + field.name);
      if (!el) continue;
      if (field.kind === "checkbox") el.checked = !!vehicle[field.name];
      else if (field.kind === "liste") el.value = (vehicle[field.name] || []).join(", ");
      else {
        el.value = vehicle[field.name] !== undefined && vehicle[field.name] !== null
          ? vehicle[field.name] : "";
        // Old cached data has no platform, and a stored one the list does not
        // know (from a newer version) would leave the select empty and fail
        // the next save.
        if (field.kind === "auswahl" && field.standard && el.value === "") {
          el.value = field.standard;
        }
      }
    }
    fillCurve(vehicle.charge_curve || []);
    document.getElementById("veh-charge_curve_source").value =
      vehicle.charge_curve_source || "auto";
  }

  function readForm() {
    const form = {};
    for (const field of FIELDS) {
      const el = document.getElementById("veh-" + field.name);
      if (!el) continue;
      if (field.kind === "checkbox") form[field.name] = el.checked;
      else if (field.kind === "liste") {
        form[field.name] = el.value.split(",").map((s) => s.trim()).filter(Boolean);
      } else if (field.kind === "text" || field.kind === "auswahl") form[field.name] = el.value;
      else if (field.optional && el.value === "") form[field.name] = null;
      else form[field.name] = Number(el.value);
    }
    form.charge_curve = readCurve();
    form.charge_curve_source = document.getElementById("veh-charge_curve_source").value;
    return form;
  }

  /* ---------- Charging curve ---------- */

  function curveRow(soc, kw) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><input type="number" class="kurve-soc" min="0" max="100" step="1"
                 value="${soc}"></td>
      <td><input type="number" class="kurve-kw" min="0" step="1"
                 value="${kw}"></td>
      <td style="width:40px"><button type="button" class="kurve-weg"
          style="background:none;border:none;color:#8a97a5;cursor:pointer;
                 font-size:18px">×</button></td>`;
    row.querySelector(".kurve-weg").addEventListener("click", () => row.remove());
    return row;
  }

  /* Which curve the planner uses, and the measured one next to the profile. */
  function curveSourceHtml(info) {
    const date = info.updated ? new Date(info.updated).toLocaleDateString("de-DE") : "";
    const dc = `${info.charges} DC-Ladung${info.charges === 1 ? "" : "en"}`;
    let text;
    if (info.source === "measured") {
      text = `Plant mit der gemessenen Kurve aus ${dc}${date ? ", zuletzt am " + date : ""}.`
        + " Wo nichts gemessen ist, gelten die Profilwerte.";
    } else if (info.setting === "profile") {
      text = "Plant mit dem Profil." + (info.measured.length
        ? ` Gemessen wäre eine Kurve aus ${dc}.` : "");
    } else {
      text = `Plant mit dem Profil: im Fahrtenbuch ${info.charges ? "erst " + dc : "noch keine DC-Ladung"},`
        + " zu wenig für eine eigene Kurve.";
    }
    const lines = [[info.measured, "#ffc93c", ""], [info.profile, "#8a97a5", "4 3"]]
      .filter(([pairs]) => pairs.length >= 2);
    if (!info.measured.length) return `<p class="unter">${K.esc(text)}</p>`;
    const top = Math.max(...lines.flatMap(([pairs]) => pairs.map((p) => p[1])), 1);
    const px = (soc) => 4 + soc / 100 * 292;
    const py = (kw) => 76 - kw / top * 70;
    const svg = `<svg viewBox="0 0 300 80" style="width:100%;height:80px;margin-top:8px">`
      + lines.map(([pairs, colour, dash]) => `<polyline fill="none" stroke="${colour}" stroke-width="2"`
        + (dash ? ` stroke-dasharray="${dash}"` : "")
        + ` points="${pairs.map((p) => px(p[0]).toFixed(1) + "," + py(p[1]).toFixed(1)).join(" ")}"/>`).join("")
      + `</svg>`;
    return `<p class="unter">${K.esc(text)}</p>${svg}`
      + `<div class="karte-fuss"><span>kW über Ladestand · bis ${K.num(top)} kW</span>`
      + `<span><span style="color:#ffc93c">━</span> gemessen · <span style="color:#8a97a5">╌</span> Profil</span></div>`;
  }

  async function showCurveSource(vehicle) {
    const box = document.getElementById("curve-source");
    if (!box) return;
    box.hidden = true;
    if (!vehicle || !vehicle.id) return;
    let info;
    try {
      info = await K.api(`/api/vehicles/${vehicle.id}/charge-curve`, { timeout: 8000 });
    } catch (failure) {
      return;
    }
    if (document.getElementById("vehicle-list").value !== String(vehicle.id)) return;
    box.innerHTML = curveSourceHtml(info);
    box.hidden = false;
  }

  /* ---------- Electricity prices ---------- */

  /* Same design as the charging curve: one row per entry, freely extendable.
   * Two or three providers cover anyone who has a charging card - no more
   * operation is needed. */
  function priceRow(pattern, eurKwh) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><input type="text" class="preis-muster" value="${K.esc(pattern)}"
                 placeholder="Ionity"></td>
      <td><input type="number" class="preis-wert" step="0.01" min="0" max="5"
                 value="${eurKwh}"></td>
      <td style="width:40px"><button type="button" class="preis-weg"
          style="width:auto;padding:6px 10px">×</button></td>`;
    row.querySelector(".preis-weg").addEventListener("click", () => row.remove());
    return row;
  }

  function fillPrices(entries) {
    const content = document.getElementById("price-rows");
    if (!content) return;
    content.innerHTML = "<tr><th>Anbieter</th><th>€/kWh</th><th></th></tr>";
    for (const entry of entries || []) {
      content.appendChild(priceRow(entry.pattern, entry.eur_kwh));
    }
  }

  function readPrices() {
    const rows = document.querySelectorAll("#price-rows tr");
    const prices = [];
    for (const row of rows) {
      const pattern = row.querySelector(".preis-muster");
      const priceField = row.querySelector(".preis-wert");
      if (!pattern || !priceField || !pattern.value.trim()) continue;
      prices.push({ pattern: pattern.value.trim(), eur_kwh: Number(priceField.value) });
    }
    return prices;
  }

  function fillCurve(pairs) {
    const content = document.getElementById("curve-rows");
    content.innerHTML = `<tr><th>Ladestand %</th><th>Leistung kW</th><th></th></tr>`;
    for (const [soc, kw] of pairs) content.appendChild(curveRow(soc, kw));
  }

  function readCurve() {
    const content = document.getElementById("curve-rows");
    const pairs = [];
    for (const row of content.querySelectorAll("tr")) {
      const soc = row.querySelector(".kurve-soc");
      const kw = row.querySelector(".kurve-kw");
      if (soc && kw && soc.value !== "" && kw.value !== "") {
        pairs.push([Number(soc.value), Number(kw.value)]);
      }
    }
    return pairs.sort((a, b) => a[0] - b[0]);
  }

  /* ---------- Loading and saving ---------- */

  const CACHE_KEY = "jolt-fahrzeuge";

  /* The list from the last time the server answered. A recording needs the
   * vehicle - without this copy it could not be started without network. */
  function cachedVehicles() {
    try {
      const list = JSON.parse(localStorage.getItem(CACHE_KEY) || "null");
      return Array.isArray(list) && list.length ? list : null;
    } catch (e) { return null; }
  }

  async function load() {
    try {
      K.state.vehicles = await K.api("/api/vehicles", { timeout: 8000 });
      try { localStorage.setItem(CACHE_KEY, JSON.stringify(K.state.vehicles)); }
      catch (e) { /* without storage, simply without a copy */ }
    } catch (failure) {
      const copy = failure.status ? null : cachedVehicles();
      if (!copy) {
        K.report("Fahrzeuge: " + failure.message, "fehler");
        return;
      }
      K.state.vehicles = copy;
      K.report("Kein Netz – Fahrzeuge vom letzten Mal. Aufzeichnen geht trotzdem.",
               "hinweis");
    }

    for (const id of ["vehicle-choice", "vehicle-list", "rec-vehicle"]) {
      const selection = document.getElementById(id);
      if (!selection) continue;
      const earlier = selection.value;
      selection.innerHTML = K.state.vehicles
        .map((f) => `<option value="${f.id}">${K.esc(f.name)}</option>`).join("");
      if (earlier) selection.value = earlier;
      // The choice for recording survives the restart: whoever sits in the
      // car wants to make it once and never again. Same consideration as on
      // the /obd page.
      if (id === "rec-vehicle" && !earlier) {
        try {
          const remembered = localStorage.getItem("jolt-aufz-fahrzeug");
          if (remembered && K.state.vehicles.some(
              (f) => String(f.id) === remembered)) {
            selection.value = remembered;
          }
        } catch (e) { /* without storage, simply without memory */ }
        selection.addEventListener("change", () => {
          try { localStorage.setItem("jolt-aufz-fahrzeug", selection.value); }
          catch (e) {}
          if (window.joltTrips && window.joltTrips.reportVehicle) {
            window.joltTrips.reportVehicle();
          }
        });
      }
    }
    // CarPlay shows on the start button which vehicle is recorded with.
    if (window.joltTrips && window.joltTrips.reportVehicle) {
      window.joltTrips.reportVehicle();
    }
    // The logbook belongs to a vehicle. At startup the trips view asks for it
    // before this list has arrived, finds no vehicle and stays hidden; now
    // that the vehicles are known it can load.
    if (window.joltTrips && window.joltTrips.loadLogbook) {
      window.joltTrips.loadLogbook();
    }

    const newOption = document.createElement("option");
    newOption.value = "neu";
    newOption.textContent = "+ neues Fahrzeug";
    document.getElementById("vehicle-list").appendChild(newOption);

    showCurrent();
  }

  function showCurrent() {
    const choice = document.getElementById("vehicle-list").value;
    if (choice === "neu") {
      fillForm(Object.assign({}, templates[0] || {}, { name: "" }));
      showPortal(null);
      showSignals(null);
      showCurveSource(null);
      return;
    }
    const vehicle = K.state.vehicles.find((f) => String(f.id) === String(choice));
    if (vehicle) {
      fillForm(vehicle);
      fillPrices(vehicle.electricity_prices);
      const defaultPrice = document.getElementById("default-price");
      if (defaultPrice) defaultPrice.value = vehicle.electricity_price_eur_kwh ?? 0.59;
    }
    showLogger(vehicle);
    showPortal(vehicle);
    showSignals(vehicle);
    showCurveSource(vehicle);
  }

  /* ---------- Logger in the car ---------- */

  /* The token only appears in the response that creates it - afterwards the
   * UI no longer knows it. So otherwise only *whether* one is valid is shown. */
  function showLogger(vehicle) {
    const as_of = document.getElementById("logger-status");
    const box = document.getElementById("logger-token");
    if (!as_of || !box) return;
    box.hidden = true;
    box.textContent = "";
    if (!vehicle) {
      as_of.textContent = "Erst speichern, dann lässt sich ein Logger einrichten.";
      return;
    }
    as_of.textContent = vehicle.logger_active
      ? "Ein Logger ist eingerichtet. Ein neues Token entwertet das alte."
      : "Kein Logger eingerichtet.";
  }

  function chosenVehicle() {
    const choice = document.getElementById("vehicle-list").value;
    if (choice === "neu") return null;
    return K.state.vehicles.find((f) => String(f.id) === String(choice)) || null;
  }

  async function loggerNew() {
    const vehicle = chosenVehicle();
    if (!vehicle) {
      K.report("Erst das Fahrzeug speichern.", "fehler");
      return;
    }
    let response;
    try {
      response = await K.api(`/api/vehicles/${vehicle.id}/logger-token`,
                            { method: "POST" });
    } catch (failure) {
      K.report("Logger-Token: " + failure.message, "fehler");
      return;
    }
    await load();
    const box = document.getElementById("logger-token");
    // textContent and not innerHTML: the token is self-generated and
    // urlsafe, but a secret does not belong in an HTML parser on principle.
    box.textContent = response.logger_token;
    box.hidden = false;
    K.report("Token erzeugt – jetzt notieren, es wird nur einmal gezeigt.",
             "hinweis");
  }

  async function loggerPath() {
    const vehicle = chosenVehicle();
    if (!vehicle || !vehicle.logger_active) return;
    try {
      await K.api(`/api/vehicles/${vehicle.id}/logger-token`,
                  { method: "DELETE" });
    } catch (failure) {
      K.report("Logger abmelden: " + failure.message, "fehler");
      return;
    }
    await load();
    K.report("Logger abgemeldet.", "hinweis");
  }

  async function templatesCharging() {
    try {
      templates = await K.api("/api/vehicles/templates");
    } catch (failure) { return; }
    const selection = document.getElementById("template");
    selection.innerHTML = '<option value="">– auswählen –</option>'
      + templates.map((v, i) => `<option value="${i}">${v.name}</option>`).join("");
  }

  async function save() {
    const choice = document.getElementById("vehicle-list").value;
    const payload = readForm();
    payload.electricity_prices = readPrices();
    const defaultPrice = document.getElementById("default-price");
    payload.electricity_price_eur_kwh = defaultPrice ? Number(defaultPrice.value) : 0.59;
    if (!payload.name) { K.report("Das Fahrzeug braucht einen Namen.", "fehler"); return; }

    // Every charge level may only occur once - the database enforces that,
    // but finding out only after a failed save which charge level was a
    // duplicate is needlessly cumbersome.
    const seen = new Set();
    for (const [soc] of payload.charge_curve) {
      if (seen.has(soc)) {
        K.report(`Ladestand ${soc} % kommt in der Ladekurve mehrfach vor.`, "fehler");
        return;
      }
      seen.add(soc);
    }

    try {
      if (choice === "neu") {
        await K.api("/api/vehicles", { method: "POST", body: payload });
      } else {
        await K.api("/api/vehicles/" + choice, { method: "PUT", body: payload });
      }
      await load();
      K.report("Fahrzeug gespeichert.", "hinweis");
    } catch (failure) {
      K.report("Speichern: " + failure.message, "fehler");
    }
  }

  function set_up() {
    formBuild();
    K.at("vehicle-list", "change", showCurrent);
    K.at("template", "change", (e) => {
      const template = templates[Number(e.target.value)];
      if (!template) return;
      fillForm(template);
      K.report("Vorlage übernommen – Werte prüfen und speichern.", "hinweis");
    });
    K.at("curve-row-new", "click", () => {
      document.getElementById("curve-rows").appendChild(curveRow(50, 100));
    });
    K.at("price-row-new", "click", () => {
      document.getElementById("price-rows").appendChild(priceRow("", 0.39));
    });
    K.at("vehicle-save", "click", save);
    K.at("logger-new", "click", loggerNew);
    K.at("logger-remove", "click", loggerPath);
  }

  return { set_up, load, templatesCharging };
})();
