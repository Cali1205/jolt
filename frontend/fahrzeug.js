/* Fahrzeugprofile pflegen: Physik-Parameter und Ladekurve.
 *
 * Die Felder sind bewusst physikalisch und nicht "Verbrauch in kWh/100 km".
 * Nur so lässt sich beantworten, was 130 statt 110 km/h kosten oder was ein
 * Pass verbraucht. Damit das niemanden am ersten Tag abschreckt, gibt es
 * Vorlagen - und danach zieht der Korrekturfaktor die Werte ohnehin an das
 * eigene Auto heran.
 */
window.joltFahrzeug = (function () {
  "use strict";

  const K = window.jolt;
  let vorlagen = [];

  const FELDER = [
    { name: "name", titel: "Name", typ: "text" },
    { name: "akku_brutto_kwh", titel: "Akku brutto (kWh)", schritt: 0.1 },
    { name: "akku_netto_kwh", titel: "Akku nutzbar (kWh)", schritt: 0.1,
      hinweis: "Gerechnet wird ausschliesslich mit netto." },
    { name: "leermasse_kg", titel: "Leergewicht (kg)", schritt: 10 },
    { name: "zuladung_kg", titel: "Zuladung (kg)", schritt: 10 },
    { name: "c_w", titel: "cw-Wert", schritt: 0.001 },
    { name: "stirnflaeche_m2", titel: "Stirnfläche (m²)", schritt: 0.01 },
    { name: "c_rr", titel: "Rollwiderstand", schritt: 0.001 },
    { name: "eta_antrieb", titel: "Wirkungsgrad Antrieb", schritt: 0.01 },
    { name: "eta_rekup", titel: "Wirkungsgrad Rekuperation", schritt: 0.01 },
    { name: "p_neben_w", titel: "Grundlast (W)", schritt: 10 },
    { name: "waermepumpe", titel: "Wärmepumpe", typ: "checkbox",
      hinweis: "Halbiert grob den Heizbedarf – im Winter der grösste "
        + "Einzelunterschied." },
    { name: "reserve_soc", titel: "Reserve (%)", schritt: 1 },
    { name: "ziel_soc", titel: "Ladestand am Ziel (%)", schritt: 1 },
    { name: "max_ladeleistung_kw", titel: "Max. Ladeleistung (kW)", schritt: 1 },
    { name: "steckertyp", titel: "Steckertyp", typ: "auswahl",
      werte: ["CCS", "Typ2", "CHAdeMO"] },
    { name: "bevorzugte_betreiber", titel: "Bevorzugte Ladeanbieter", typ: "liste",
      hinweis: "Namen oder Namensteile, durch Komma getrennt - z.B. "
        + "\"EnBW, Ionity\". Kein Ausschluss anderer Anbieter, nur ein "
        + "Vorteil bei der Stoppwahl." },
  ];

  function formularBauen() {
    const behaelter = document.getElementById("fahrzeug-formular");
    behaelter.innerHTML = FELDER.map((feld) => {
      const id = "fz-" + feld.name;
      let eingabe;
      if (feld.typ === "checkbox") {
        eingabe = `<input type="checkbox" id="${id}" style="width:auto">`;
      } else if (feld.typ === "auswahl") {
        eingabe = `<select id="${id}">`
          + feld.werte.map((w) => `<option value="${w}">${w}</option>`).join("")
          + `</select>`;
      } else if (feld.typ === "text" || feld.typ === "liste") {
        eingabe = `<input type="text" id="${id}">`;
      } else {
        eingabe = `<input type="number" id="${id}" step="${feld.schritt}">`;
      }
      const hinweis = feld.hinweis
        ? `<div class="unter" style="margin-top:2px">${feld.hinweis}</div>` : "";
      return `<label for="${id}">${feld.titel}</label>${eingabe}${hinweis}`;
    }).join("");
  }

  function formularFuellen(fahrzeug) {
    for (const feld of FELDER) {
      const el = document.getElementById("fz-" + feld.name);
      if (!el) continue;
      if (feld.typ === "checkbox") el.checked = !!fahrzeug[feld.name];
      else if (feld.typ === "liste") el.value = (fahrzeug[feld.name] || []).join(", ");
      else el.value = fahrzeug[feld.name] !== undefined ? fahrzeug[feld.name] : "";
    }
    kurveFuellen(fahrzeug.ladekurve || []);
  }

  function formularLesen() {
    const daten = {};
    for (const feld of FELDER) {
      const el = document.getElementById("fz-" + feld.name);
      if (!el) continue;
      if (feld.typ === "checkbox") daten[feld.name] = el.checked;
      else if (feld.typ === "liste") {
        daten[feld.name] = el.value.split(",").map((s) => s.trim()).filter(Boolean);
      } else if (feld.typ === "text" || feld.typ === "auswahl") daten[feld.name] = el.value;
      else daten[feld.name] = Number(el.value);
    }
    daten.ladekurve = kurveLesen();
    return daten;
  }

  /* ---------- Ladekurve ---------- */

  function kurveZeile(soc, kw) {
    const zeile = document.createElement("tr");
    zeile.innerHTML = `
      <td><input type="number" class="kurve-soc" min="0" max="100" step="1"
                 value="${soc}"></td>
      <td><input type="number" class="kurve-kw" min="0" step="1"
                 value="${kw}"></td>
      <td style="width:40px"><button type="button" class="kurve-weg"
          style="background:none;border:none;color:#8a97a5;cursor:pointer;
                 font-size:18px">×</button></td>`;
    zeile.querySelector(".kurve-weg").addEventListener("click", () => zeile.remove());
    return zeile;
  }

  /* ---------- Strompreise ---------- */

  /* Dieselbe Bauart wie die Ladekurve: eine Zeile je Eintrag, frei zu
   * ergänzen. Zwei bis drei Anbieter deckt jeder ab, der eine Ladekarte
   * hat - mehr Bedienung braucht es nicht. */
  function preisZeile(muster, eurKwh) {
    const zeile = document.createElement("tr");
    zeile.innerHTML = `
      <td><input type="text" class="preis-muster" value="${muster || ""}"
                 placeholder="Ionity"></td>
      <td><input type="number" class="preis-wert" step="0.01" min="0" max="5"
                 value="${eurKwh}"></td>
      <td style="width:40px"><button type="button" class="preis-weg"
          style="width:auto;padding:6px 10px">×</button></td>`;
    zeile.querySelector(".preis-weg").addEventListener("click", () => zeile.remove());
    return zeile;
  }

  function preiseFuellen(liste) {
    const koerper = document.getElementById("preis-zeilen");
    if (!koerper) return;
    koerper.innerHTML = "<tr><th>Anbieter</th><th>€/kWh</th><th></th></tr>";
    for (const eintrag of liste || []) {
      koerper.appendChild(preisZeile(eintrag.muster, eintrag.eur_kwh));
    }
  }

  function preiseLesen() {
    const zeilen = document.querySelectorAll("#preis-zeilen tr");
    const liste = [];
    for (const zeile of zeilen) {
      const muster = zeile.querySelector(".preis-muster");
      const wert = zeile.querySelector(".preis-wert");
      if (!muster || !wert || !muster.value.trim()) continue;
      liste.push({ muster: muster.value.trim(), eur_kwh: Number(wert.value) });
    }
    return liste;
  }

  function kurveFuellen(paare) {
    const koerper = document.getElementById("kurve-zeilen");
    koerper.innerHTML = `<tr><th>Ladestand %</th><th>Leistung kW</th><th></th></tr>`;
    for (const [soc, kw] of paare) koerper.appendChild(kurveZeile(soc, kw));
  }

  function kurveLesen() {
    const koerper = document.getElementById("kurve-zeilen");
    const paare = [];
    for (const zeile of koerper.querySelectorAll("tr")) {
      const soc = zeile.querySelector(".kurve-soc");
      const kw = zeile.querySelector(".kurve-kw");
      if (soc && kw && soc.value !== "" && kw.value !== "") {
        paare.push([Number(soc.value), Number(kw.value)]);
      }
    }
    return paare.sort((a, b) => a[0] - b[0]);
  }

  /* ---------- Laden und Speichern ---------- */

  async function laden() {
    try {
      K.zustand.fahrzeuge = await K.api("/api/fahrzeuge");
    } catch (fehler) {
      K.melden("Fahrzeuge: " + fehler.message, "fehler");
      return;
    }

    for (const id of ["fahrzeug-wahl", "fahrzeug-liste", "aufz-fahrzeug"]) {
      const auswahl = document.getElementById(id);
      if (!auswahl) continue;
      const vorher = auswahl.value;
      auswahl.innerHTML = K.zustand.fahrzeuge
        .map((f) => `<option value="${f.id}">${f.name}</option>`).join("");
      if (vorher) auswahl.value = vorher;
      // Die Wahl fuers Aufzeichnen ueberlebt den Neustart: Wer im Auto
      // sitzt, will sie einmal treffen und nie wieder. Dieselbe Ueberlegung
      // wie auf der /obd-Seite.
      if (id === "aufz-fahrzeug" && !vorher) {
        try {
          const gemerkt = localStorage.getItem("jolt-aufz-fahrzeug");
          if (gemerkt && K.zustand.fahrzeuge.some(
              (f) => String(f.id) === gemerkt)) {
            auswahl.value = gemerkt;
          }
        } catch (e) { /* ohne Speicher eben ohne Gedaechtnis */ }
        auswahl.addEventListener("change", () => {
          try { localStorage.setItem("jolt-aufz-fahrzeug", auswahl.value); }
          catch (e) {}
        });
      }
    }

    const neu = document.createElement("option");
    neu.value = "neu";
    neu.textContent = "+ neues Fahrzeug";
    document.getElementById("fahrzeug-liste").appendChild(neu);

    aktuellesZeigen();
  }

  function aktuellesZeigen() {
    const wahl = document.getElementById("fahrzeug-liste").value;
    if (wahl === "neu") {
      formularFuellen(Object.assign({}, vorlagen[0] || {}, { name: "" }));
      return;
    }
    const fahrzeug = K.zustand.fahrzeuge.find((f) => String(f.id) === String(wahl));
    if (fahrzeug) {
      formularFuellen(fahrzeug);
      preiseFuellen(fahrzeug.strompreise);
      const standard = document.getElementById("standardpreis");
      if (standard) standard.value = fahrzeug.strompreis_eur_kwh ?? 0.59;
    }
    loggerZeigen(fahrzeug);
  }

  /* ---------- Logger im Auto ---------- */

  /* Das Token steht nur in der Antwort, die es erzeugt - danach kennt es die
   * Oberfläche nicht mehr. Angezeigt wird sonst also nur, *ob* eines gilt. */
  function loggerZeigen(fahrzeug) {
    const stand = document.getElementById("logger-stand");
    const kasten = document.getElementById("logger-token");
    if (!stand || !kasten) return;
    kasten.hidden = true;
    kasten.textContent = "";
    if (!fahrzeug) {
      stand.textContent = "Erst speichern, dann lässt sich ein Logger einrichten.";
      return;
    }
    stand.textContent = fahrzeug.logger_aktiv
      ? "Ein Logger ist eingerichtet. Ein neues Token entwertet das alte."
      : "Kein Logger eingerichtet.";
  }

  function gewaehltesFahrzeug() {
    const wahl = document.getElementById("fahrzeug-liste").value;
    if (wahl === "neu") return null;
    return K.zustand.fahrzeuge.find((f) => String(f.id) === String(wahl)) || null;
  }

  async function loggerNeu() {
    const fahrzeug = gewaehltesFahrzeug();
    if (!fahrzeug) {
      K.melden("Erst das Fahrzeug speichern.", "fehler");
      return;
    }
    let antwort;
    try {
      antwort = await K.api(`/api/fahrzeuge/${fahrzeug.id}/logger-token`,
                            { method: "POST" });
    } catch (fehler) {
      K.melden("Logger-Token: " + fehler.message, "fehler");
      return;
    }
    await laden();
    const kasten = document.getElementById("logger-token");
    // textContent und nicht innerHTML: Das Token ist zwar selbst erzeugt und
    // urlsafe, aber ein Geheimnis gehört grundsätzlich nicht durch einen
    // HTML-Parser.
    kasten.textContent = antwort.logger_token;
    kasten.hidden = false;
    K.melden("Token erzeugt – jetzt notieren, es wird nur einmal gezeigt.",
             "hinweis");
  }

  async function loggerWeg() {
    const fahrzeug = gewaehltesFahrzeug();
    if (!fahrzeug || !fahrzeug.logger_aktiv) return;
    try {
      await K.api(`/api/fahrzeuge/${fahrzeug.id}/logger-token`,
                  { method: "DELETE" });
    } catch (fehler) {
      K.melden("Logger abmelden: " + fehler.message, "fehler");
      return;
    }
    await laden();
    K.melden("Logger abgemeldet.", "hinweis");
  }

  async function vorlagenLaden() {
    try {
      vorlagen = await K.api("/api/fahrzeuge/vorlagen");
    } catch (fehler) { return; }
    const auswahl = document.getElementById("vorlage");
    auswahl.innerHTML = '<option value="">– auswählen –</option>'
      + vorlagen.map((v, i) => `<option value="${i}">${v.name}</option>`).join("");
  }

  async function speichern() {
    const wahl = document.getElementById("fahrzeug-liste").value;
    const daten = formularLesen();
    daten.strompreise = preiseLesen();
    const standard = document.getElementById("standardpreis");
    daten.strompreis_eur_kwh = standard ? Number(standard.value) : 0.59;
    if (!daten.name) { K.melden("Das Fahrzeug braucht einen Namen.", "fehler"); return; }

    // Jeder Ladestand darf nur einmal vorkommen - die Datenbank erzwingt das,
    // aber erst nach einem fehlgeschlagenen Speichern zu erfahren, welcher
    // Ladestand doppelt war, ist unnötig umständlich.
    const gesehen = new Set();
    for (const [soc] of daten.ladekurve) {
      if (gesehen.has(soc)) {
        K.melden(`Ladestand ${soc} % kommt in der Ladekurve mehrfach vor.`, "fehler");
        return;
      }
      gesehen.add(soc);
    }

    try {
      if (wahl === "neu") {
        await K.api("/api/fahrzeuge", { method: "POST", body: daten });
      } else {
        await K.api("/api/fahrzeuge/" + wahl, { method: "PUT", body: daten });
      }
      await laden();
      K.melden("Fahrzeug gespeichert.", "hinweis");
    } catch (fehler) {
      K.melden("Speichern: " + fehler.message, "fehler");
    }
  }

  function einrichten() {
    formularBauen();
    K.an("fahrzeug-liste", "change", aktuellesZeigen);
    K.an("vorlage", "change", (e) => {
      const vorlage = vorlagen[Number(e.target.value)];
      if (!vorlage) return;
      formularFuellen(vorlage);
      K.melden("Vorlage übernommen – Werte prüfen und speichern.", "hinweis");
    });
    K.an("kurve-zeile-neu", "click", () => {
      document.getElementById("kurve-zeilen").appendChild(kurveZeile(50, 100));
    });
    K.an("preis-zeile-neu", "click", () => {
      document.getElementById("preis-zeilen").appendChild(preisZeile("", 0.39));
    });
    K.an("fahrzeug-speichern", "click", speichern);
    K.an("logger-neu", "click", loggerNeu);
    K.an("logger-weg", "click", loggerWeg);
  }

  return { einrichten, laden, vorlagenLaden };
})();
