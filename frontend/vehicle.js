/* Fahrzeugprofile pflegen: Physik-Parameter und Ladekurve.
 *
 * Die Felder sind bewusst physikalisch und nicht "Verbrauch in kWh/100 km".
 * Nur so lässt sich beantworten, was 130 statt 110 km/h kosten oder was ein
 * Pass verbraucht. Damit das niemanden am ersten Tag abschreckt, gibt es
 * Vorlagen - und danach zieht der Korrekturfaktor die Werte ohnehin an das
 * eigene Auto heran.
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
    const container = document.getElementById("fahrzeug-formular");
    container.innerHTML = FIELDS.map((field) => {
      const id = "fz-" + field.name;
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
      const el = document.getElementById("fz-" + field.name);
      if (!el) continue;
      if (field.kind === "checkbox") el.checked = !!vehicle[field.name];
      else if (field.kind === "liste") el.value = (vehicle[field.name] || []).join(", ");
      else el.value = vehicle[field.name] !== undefined && vehicle[field.name] !== null
        ? vehicle[field.name] : "";
    }
    fillCurve(vehicle.charge_curve || []);
  }

  function readForm() {
    const records = {};
    for (const field of FIELDS) {
      const el = document.getElementById("fz-" + field.name);
      if (!el) continue;
      if (field.kind === "checkbox") records[field.name] = el.checked;
      else if (field.kind === "liste") {
        records[field.name] = el.value.split(",").map((s) => s.trim()).filter(Boolean);
      } else if (field.kind === "text" || field.kind === "auswahl") records[field.name] = el.value;
      else if (field.optional && el.value === "") records[field.name] = null;
      else records[field.name] = Number(el.value);
    }
    records.charge_curve = readCurve();
    return records;
  }

  /* ---------- Ladekurve ---------- */

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

  /* ---------- Strompreise ---------- */

  /* Dieselbe Bauart wie die Ladekurve: eine Zeile je Eintrag, frei zu
   * ergänzen. Zwei bis drei Anbieter deckt jeder ab, der eine Ladekarte
   * hat - mehr Bedienung braucht es nicht. */
  function priceRow(pattern, eurKwh) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><input type="text" class="preis-muster" value="${pattern || ""}"
                 placeholder="Ionity"></td>
      <td><input type="number" class="preis-wert" step="0.01" min="0" max="5"
                 value="${eurKwh}"></td>
      <td style="width:40px"><button type="button" class="preis-weg"
          style="width:auto;padding:6px 10px">×</button></td>`;
    row.querySelector(".preis-weg").addEventListener("click", () => row.remove());
    return row;
  }

  function fillPrices(lst) {
    const content = document.getElementById("preis-zeilen");
    if (!content) return;
    content.innerHTML = "<tr><th>Anbieter</th><th>€/kWh</th><th></th></tr>";
    for (const entry of lst || []) {
      content.appendChild(priceRow(entry.pattern, entry.eur_kwh));
    }
  }

  function readPrices() {
    const rows = document.querySelectorAll("#preis-zeilen tr");
    const lst = [];
    for (const row of rows) {
      const pattern = row.querySelector(".preis-muster");
      const val = row.querySelector(".preis-wert");
      if (!pattern || !val || !pattern.value.trim()) continue;
      lst.push({ pattern: pattern.value.trim(), eur_kwh: Number(val.value) });
    }
    return lst;
  }

  function fillCurve(pairs) {
    const content = document.getElementById("kurve-zeilen");
    content.innerHTML = `<tr><th>Ladestand %</th><th>Leistung kW</th><th></th></tr>`;
    for (const [soc, kw] of pairs) content.appendChild(curveRow(soc, kw));
  }

  function readCurve() {
    const content = document.getElementById("kurve-zeilen");
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

  /* ---------- Laden und Speichern ---------- */

  async function load() {
    try {
      K.state.vehicles = await K.api("/api/fahrzeuge");
    } catch (failure) {
      K.report("Fahrzeuge: " + failure.message, "fehler");
      return;
    }

    for (const id of ["fahrzeug-wahl", "fahrzeug-liste", "aufz-fahrzeug"]) {
      const selection = document.getElementById(id);
      if (!selection) continue;
      const earlier = selection.value;
      selection.innerHTML = K.state.vehicles
        .map((f) => `<option value="${f.id}">${f.name}</option>`).join("");
      if (earlier) selection.value = earlier;
      // Die Wahl fuers Aufzeichnen ueberlebt den Neustart: Wer im Auto
      // sitzt, will sie einmal treffen und nie wieder. Dieselbe Ueberlegung
      // wie auf der /obd-Seite.
      if (id === "aufz-fahrzeug" && !earlier) {
        try {
          const remembered = localStorage.getItem("jolt-aufz-fahrzeug");
          if (remembered && K.state.vehicles.some(
              (f) => String(f.id) === remembered)) {
            selection.value = remembered;
          }
        } catch (e) { /* ohne Speicher eben ohne Gedaechtnis */ }
        selection.addEventListener("change", () => {
          try { localStorage.setItem("jolt-aufz-fahrzeug", selection.value); }
          catch (e) {}
          if (window.joltTrips && window.joltTrips.reportVehicle) {
            window.joltTrips.reportVehicle();
          }
        });
      }
    }
    // CarPlay zeigt am Start-Knopf, mit welchem Fahrzeug aufgezeichnet wird.
    if (window.joltTrips && window.joltTrips.reportVehicle) {
      window.joltTrips.reportVehicle();
    }

    const fresh = document.createElement("option");
    fresh.value = "neu";
    fresh.textContent = "+ neues Fahrzeug";
    document.getElementById("fahrzeug-liste").appendChild(fresh);

    showCurrent();
  }

  function showCurrent() {
    const choice = document.getElementById("fahrzeug-liste").value;
    if (choice === "neu") {
      fillForm(Object.assign({}, templates[0] || {}, { name: "" }));
      return;
    }
    const vehicle = K.state.vehicles.find((f) => String(f.id) === String(choice));
    if (vehicle) {
      fillForm(vehicle);
      fillPrices(vehicle.electricity_prices);
      const std_default = document.getElementById("standardpreis");
      if (std_default) std_default.value = vehicle.electricity_price_eur_kwh ?? 0.59;
    }
    showLogger(vehicle);
  }

  /* ---------- Logger im Auto ---------- */

  /* Das Token steht nur in der Antwort, die es erzeugt - danach kennt es die
   * Oberfläche nicht mehr. Angezeigt wird sonst also nur, *ob* eines gilt. */
  function showLogger(vehicle) {
    const as_of = document.getElementById("logger-stand");
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
    const choice = document.getElementById("fahrzeug-liste").value;
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
      response = await K.api(`/api/fahrzeuge/${vehicle.id}/logger-token`,
                            { method: "POST" });
    } catch (failure) {
      K.report("Logger-Token: " + failure.message, "fehler");
      return;
    }
    await load();
    const box = document.getElementById("logger-token");
    // textContent und nicht innerHTML: Das Token ist zwar selbst erzeugt und
    // urlsafe, aber ein Geheimnis gehört grundsätzlich nicht durch einen
    // HTML-Parser.
    box.textContent = response.logger_token;
    box.hidden = false;
    K.report("Token erzeugt – jetzt notieren, es wird nur einmal gezeigt.",
             "hinweis");
  }

  async function loggerPath() {
    const vehicle = chosenVehicle();
    if (!vehicle || !vehicle.logger_active) return;
    try {
      await K.api(`/api/fahrzeuge/${vehicle.id}/logger-token`,
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
      templates = await K.api("/api/fahrzeuge/vorlagen");
    } catch (failure) { return; }
    const selection = document.getElementById("vorlage");
    selection.innerHTML = '<option value="">– auswählen –</option>'
      + templates.map((v, i) => `<option value="${i}">${v.name}</option>`).join("");
  }

  async function save() {
    const choice = document.getElementById("fahrzeug-liste").value;
    const records = readForm();
    records.electricity_prices = readPrices();
    const std_default = document.getElementById("standardpreis");
    records.electricity_price_eur_kwh = std_default ? Number(std_default.value) : 0.59;
    if (!records.name) { K.report("Das Fahrzeug braucht einen Namen.", "fehler"); return; }

    // Jeder Ladestand darf nur einmal vorkommen - die Datenbank erzwingt das,
    // aber erst nach einem fehlgeschlagenen Speichern zu erfahren, welcher
    // Ladestand doppelt war, ist unnötig umständlich.
    const seen = new Set();
    for (const [soc] of records.charge_curve) {
      if (seen.has(soc)) {
        K.report(`Ladestand ${soc} % kommt in der Ladekurve mehrfach vor.`, "fehler");
        return;
      }
      seen.add(soc);
    }

    try {
      if (choice === "neu") {
        await K.api("/api/fahrzeuge", { method: "POST", body: records });
      } else {
        await K.api("/api/fahrzeuge/" + choice, { method: "PUT", body: records });
      }
      await load();
      K.report("Fahrzeug gespeichert.", "hinweis");
    } catch (failure) {
      K.report("Speichern: " + failure.message, "fehler");
    }
  }

  function set_up() {
    formBuild();
    K.at("fahrzeug-liste", "change", showCurrent);
    K.at("vorlage", "change", (e) => {
      const template = templates[Number(e.target.value)];
      if (!template) return;
      fillForm(template);
      K.report("Vorlage übernommen – Werte prüfen und speichern.", "hinweis");
    });
    K.at("kurve-zeile-neu", "click", () => {
      document.getElementById("kurve-zeilen").appendChild(curveRow(50, 100));
    });
    K.at("preis-zeile-neu", "click", () => {
      document.getElementById("preis-zeilen").appendChild(priceRow("", 0.39));
    });
    K.at("fahrzeug-speichern", "click", save);
    K.at("logger-neu", "click", loggerNew);
    K.at("logger-weg", "click", loggerPath);
  }

  return { set_up, load, templatesCharging };
})();
