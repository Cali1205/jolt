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
    { name: "waermepumpe", title: "Wärmepumpe", kind: "checkbox",
      hint: "Halbiert grob den Heizbedarf – im Winter der grösste "
        + "Einzelunterschied." },
    { name: "reserve_soc", title: "Reserve (%)", step: 1 },
    { name: "target_soc", title: "Ladestand am Ziel (%)", step: 1 },
    { name: "max_charge_power_kw", title: "Max. Ladeleistung (kW)", step: 1 },
    { name: "steckertyp", title: "Steckertyp", kind: "auswahl",
      vals: ["CCS", "Typ2", "CHAdeMO"] },
    { name: "preferred_operators", title: "Bevorzugte Ladeanbieter", kind: "liste",
      hint: "Namen oder Namensteile, durch Komma getrennt - z.B. "
        + "\"EnBW, Ionity\". Kein Ausschluss anderer Anbieter, nur ein "
        + "Vorteil bei der Stoppwahl." },
  ];

  function formBuild() {
    const container = document.getElementById("vehicle-form");
    container.innerHTML = FIELDS.map((field) => {
      const id = "veh-" + field.name;
      let user_input;
      if (field.kind === "checkbox") {
        user_input = `<input type="checkbox" id="${id}" style="width:auto">`;
      } else if (field.kind === "auswahl") {
        user_input = `<select id="${id}">`
          + field.vals.map((w) => `<option value="${w}">${w}</option>`).join("")
          + `</select>`;
      } else if (field.kind === "text" || field.kind === "liste") {
        user_input = `<input type="text" id="${id}">`;
      } else {
        user_input = `<input type="number" id="${id}" step="${field.step}">`;
      }
      const hint = field.hint
        ? `<div class="unter" style="margin-top:2px">${field.hint}</div>` : "";
      return `<label for="${id}">${field.title}</label>${user_input}${hint}`;
    }).join("");
  }

  function fillForm(vehicle) {
    for (const field of FIELDS) {
      const el = document.getElementById("veh-" + field.name);
      if (!el) continue;
      if (field.kind === "checkbox") el.checked = !!vehicle[field.name];
      else if (field.kind === "liste") el.value = (vehicle[field.name] || []).join(", ");
      else el.value = vehicle[field.name] !== undefined && vehicle[field.name] !== null
        ? vehicle[field.name] : "";
    }
    fillCurve(vehicle.charge_curve || []);
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

  async function load() {
    try {
      K.state.vehicles = await K.api("/api/vehicles");
    } catch (failure) {
      K.report("Fahrzeuge: " + failure.message, "fehler");
      return;
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
