/* Web Bluetooth gegen einen ELM327-Dongle.
 *
 * Der Dongle ist eine serielle Schnittstelle in BLE-Verkleidung: Man schreibt
 * ASCII-Befehle auf eine Charakteristik und bekommt die Antwort als
 * Notifications auf einer zweiten zurück, abgeschlossen von einem '>' als
 * Eingabeaufforderung. Mehr Protokoll gibt es nicht.
 *
 * Diese Seite rät bewusst wenig und zeigt viel: Jeder Befehl und jede Antwort
 * stehen im Protokoll. Ob die Annahmen über die PIDs des ID.Buzz stimmen,
 * entscheidet sich an dem, was das Auto zurückschickt - nicht an dem, was
 * hier steht.
 */
(function () {
  "use strict";

  /* Welchen GATT-Dienst ein ELM327-Klon anbietet, ist nicht genormt. Web
   * Bluetooth verlangt aber, dass man alle Dienste, die man anfassen will,
   * **vorher** anmeldet - man kann nicht erst verbinden und dann nachsehen.
   * Deshalb die Liste der gebräuchlichen; der Vgate iCar Pro nutzt nach
   * verbreiteter Auskunft 0xFFF0, die anderen kosten nichts.
   *
   * Ausgeschrieben als 128-bit-UUID und nicht als Kurzform `0xfff0`: Die
   * Spezifikation erlaubt beides, aber Bluefy reicht die Optionen an eine
   * native Schicht weiter, und die stolperte über die Zahl - `RequestDevice:
   * Request payload could not be parsed`, noch bevor ein Geräte-Dialog
   * erschien. An der ausgeschriebenen Form gibt es nichts zu deuten, und
   * Chrome nimmt sie ebenso. */
  const el = (id) => document.getElementById(id);
  const O = window.joltObd;   // Verbindung, ELM327, Messwerte
  let lastSoc = null;

  /* ---------- Protokoll ---------- */

  function log(text, variety) {
    const timestamp = new Date().toLocaleTimeString("de-DE");
    const character = variety === "raus" ? "→" : (variety === "rein" ? "←" : " ");
    el("log").textContent += `${timestamp} ${character} ${text}\n`;
    el("log").scrollTop = el("log").scrollHeight;
  }

  function as_of(text, variety) {
    const k = el("verbindung");
    k.textContent = text;
    k.className = "stand " + (variety || "");
  }

  function buttons(at) {
    for (const id of ["init", "soc", "senden", "melden", "fahrt-start", "pruefen"]) el(id).disabled = !at;
  }

  /* ---------- Ladestand ---------- */

  async function readSoc() {
    el("soc-wert").textContent = "…";
    try {
      // Adresse und Filter stehen seit dem Handshake; sie hier erneut zu
      // setzen würde ATCP17 und ATCAF1 nicht wiederholen und damit gerade
      // das zerstören, worauf es ankommt.
      const response = await O.command("22028C");
      const val = O.socFromResponse(response);
      if (val === null) {
        el("soc-wert").textContent = "?";
        log("Antwort enthält kein 62028C - siehe oben. Entweder ist die "
            + "Datenkennung eine andere, oder das Steuergerät antwortet "
            + "nicht auf dieser Kennung.");
        return;
      }
      lastSoc = Math.round(val.hmi * 10) / 10;
      // Beide Zahlen anzeigen: Die grosse ist die, die im Auto steht und die
      // jolt bekommt; die kleine daneben macht nachvollziehbar, woraus sie
      // entstanden ist.
      el("soc-wert").textContent = lastSoc + " %";
      el("soc-herkunft").textContent =
        `Rohwert 0x${val.raw.toString(16).toUpperCase()} = ${val.raw}`
        + ` → brutto ${val.bms.toFixed(1)} % → Anzeige ${val.hmi.toFixed(1)} %`;
      log(`Ladestand: brutto ${val.bms.toFixed(1)} %, `
          + `Anzeige ${val.hmi.toFixed(1)} % (Rohwert ${val.raw})`);
    } catch (failure) {
      el("soc-wert").textContent = "–";
      log("FEHLER " + failure.message);
    }
  }


  /* ---------- Der übliche Weg: alles in einem Zug ---------- */

  /* Ein Knopf statt fünf. Vollautomatisch geht es nicht - `requestDevice`
   * verlangt zwingend eine Nutzergeste, eine Seite darf sich beim Laden
   * nicht von selbst mit einem Gerät verbinden. Aber eine Geste genügt für
   * die ganze Kette, und das ist der Unterschied zwischen "im Auto machbar"
   * und "im Auto zu umständlich".
   *
   * Die Fahrt wird hier gleich mit angelegt: Ohne laufende Sitzung nimmt
   * jolt die Messpunkte zwar entgegen, legt sie aber nirgends ab - und
   * das merkt man erst hinterher. */
  function goAsOf(text, variety) {
    const k = el("los-stand");
    k.textContent = text;
    k.className = "stand " + (variety || "");
  }

  function joltToken() {
    // Dieselbe Anmeldung wie die Haupt-App: Wer sich dort angemeldet hat,
    // muss es hier nicht noch einmal tun.
    try { return localStorage.getItem("jolt-token") || ""; }
    catch (e) { return ""; }
  }

  /* Wie schnell das Auto sein muss, damit es als "fährt" gilt. Zehn km/h
   * liegen sicher über GPS-Rauschen und über dem Rangieren auf dem Hof, und
   * sicher unter allem, was eine Fahrt ist. */
  const DRIVES_FROM_KMH = 10;
  // Zwei Messungen hintereinander, damit ein einzelner Ausreisser keine
  // Fahrt anlegt.
  const DRIVES_ROUNDS = 2;
  let moved = 0;

  /* Ein Name, den niemand tippen muss.
   *
   * Das Namensfeld war ein Handgriff zu viel: Wer im Auto sitzt, tippt
   * nichts. Datum und Uhrzeit sind ohnehin die Angabe, nach der man später
   * sucht - und Start und Ziel trägt jolt beim Abschliessen selbst nach,
   * aus dem ersten und letzten Messpunkt. */
  function tripName() {
    const own = el("fahrt-name").value.trim();
    if (own) return own;
    return new Date().toLocaleString("de-DE", {
      weekday: "short", day: "2-digit", month: "2-digit",
      hour: "2-digit", minute: "2-digit" });
  }

  async function start_driving() {
    const btn = el("los");
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

      // Erst prüfen, ob überhaupt etwas ankommt. Eine Aufzeichnung zu
      // starten, die dann nur Positionen ohne Ladestand sammelt, wäre eine
      // verlorene Fahrt - und das fiele erst am Ziel auf.
      goAsOf("Ladestand lesen …");
      const probe = O.socFromResponse(await O.command("22028C"));
      if (!probe) throw new Error("Das Auto liefert keinen Ladestand");
      el("soc-wert").textContent = Math.round(probe.hmi * 10) / 10 + " %";

      if (el("automatik").checked) {
        // Dieselbe Prüfung wie beim Start von Hand. Ohne sie liefe die
        // Schleife los und scheiterte bei jedem Anlegen der Fahrt an einem
        // 401 - sichtbar nur im Protokoll, während oben "Bereit" steht.
        if (!el("token").value.trim() && !joltToken()) {
          throw new Error("Erst in jolt anmelden oder ein Logger-Token "
                          + "eintragen");
        }
        // Nicht sofort anlegen: Wer im Stand verbindet, bekäme sonst eine
        // Fahrt, die an der Auffahrt beginnt und eine halbe Stunde
        // Parkplatz enthält. Die Seite wartet, bis sich etwas bewegt.
        goAsOf("Bereit – wartet, bis das Auto fährt.", "gut");
        running = true;
        moved = 0;
        // `runde` steuert, welche selten gelesenen Messwerte drankommen
        // (`satzLesen`). Ohne Rücksetzen zählt die zweite Fahrt einer
        // Sitzung dort weiter, wo die erste aufhörte.
        lap = 0;
        el("fahrt-start").hidden = true;
        el("fahrt-stop").hidden = false;
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

  /* Die Fahrt in jolt anlegen. Getrennt vom Verbinden, weil sie bei
   * eingeschalteter Automatik erst entsteht, wenn das Auto losfährt. */
  async function createTrip(soc) {
    goAsOf("Fahrt anlegen …");
    const wo = await city();
    const response = await fetch("/api/live/aufzeichnung", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Token": joltToken() },
      body: JSON.stringify({
        vehicle_id: vehicleId(),
        lat: wo.lat, lon: wo.lon,
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

  /* Welches Fahrzeug - gefragt, nicht geraten.
   *
   * Hier stand `fahrzeuge[0]`. Die Liste kommt nach ID sortiert, und die
   * erste ist das beim ersten Start angelegte "Allgemeine E-Auto" - nicht
   * das, in dem man sitzt. Die Aufzeichnung wäre dem falschen Fahrzeug
   * zugeschrieben worden, und schlimmer: Die Kalibrierung hätte den
   * Korrekturfaktor eines Autos verstellt, mit dem niemand gefahren ist.
   *
   * Die Wahl bleibt im Browser stehen. Wer im Auto sitzt, will sie einmal
   * treffen und nie wieder. */
  async function vehiclesCharging() {
    const selection = el("fahrzeug-wahl-obd");
    if (!selection) return;
    try {
      const response = await fetch("/api/fahrzeuge",
                                  { headers: { "X-Token": joltToken() } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const vehicles = await response.json();
      let remembered = null;
      try { remembered = localStorage.getItem("jolt-obd-fahrzeug"); } catch (e) {}
      selection.innerHTML = vehicles
        .map((f) => `<option value="${f.id}">${f.name}</option>`).join("");
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
    const selection = el("fahrzeug-wahl-obd");
    if (!selection || !selection.value) {
      throw new Error("Kein Fahrzeug gewählt - erst in jolt anmelden, "
                      + "dann hier neu laden.");
    }
    return Number(selection.value);
  }

  /* ---------- Aufzeichnung ---------- */

  let running = false;
  let lap = 0;
  let wake_lock = null;   // WakeLockSentinel
  let sessionId = null;    // gesetzt, wenn diese Seite die Fahrt anlegte

  /* Den Bildschirm wach halten. Ohne das schaltet iOS ihn nach einer Minute
   * aus, und mit dem Bildschirm schläft der Seiteninhalt - die Verbindung
   * übersteht zwar den Sperrbildschirm, die Schleife aber nicht.
   *
   * Die Sperre geht verloren, wenn die Seite in den Hintergrund gerät, und
   * kommt nicht von selbst zurück; deshalb wird sie beim Zurückkommen neu
   * geholt. Kennt der Browser die Schnittstelle nicht, läuft die
   * Aufzeichnung trotzdem - dann muss man den Bildschirm eben in den
   * Einstellungen an lassen. */
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
    el("fahrt-werte").innerHTML = vals.map(([name, num]) =>
      `<div class="wert"><div class="zahl">${num}</div>`
      + `<div class="name">${name}</div></div>`).join("");
  }

  async function aRound() {
    const raw = await O.readRecord(lap);
    lap += 1;

    const soc = O.socFromRaw(raw.soc_raw);
    const wo = await city().catch((f) => {
      log("Standort: " + f.message);
      return null;
    });
    if (!wo) return null;

    if (typeof wo.elevation_m === "number") raw.elevation_m = Math.round(wo.elevation_m);

    /* Automatik: warten, bis das Auto wirklich fährt.
     *
     * Gemessen wird am Tempo des Fahrzeugs, nicht am GPS - das Auto weiss
     * es genauer und liefert es ohnehin mit. Fehlt der Wert, gilt das GPS
     * als Rückfall; fehlt auch das, wird nicht gewartet, sondern gleich
     * aufgezeichnet. Eine Automatik, die mangels Messwert gar nichts tut,
     * wäre die schlechteste Sorte Automatik.
     *
     * Zwei Runden hintereinander, damit ein einzelner Ausreisser keine
     * Fahrt anlegt - und keine Fahrt entsteht, während das Auto auf dem Hof
     * rangiert. */
    if (!sessionId && el("automatik").checked && !el("token").value.trim()) {
      const velocity = typeof raw.speed_kmh === "number" ? raw.speed_kmh
        : (typeof wo.speed_kmh === "number" && !Number.isNaN(wo.speed_kmh)
           ? wo.speed_kmh : null);
      if (velocity !== null && velocity < DRIVES_FROM_KMH) {
        moved = 0;
        return { soc, raw, waits: true,
                 records: { reason: `steht (${Math.round(velocity)} km/h)` } };
      }
      moved += 1;
      if (velocity !== null && moved < DRIVES_ROUNDS) {
        return { soc, raw, waits: true,
                 records: { reason: `fährt an (${Math.round(velocity)} km/h)` } };
      }
      log(`Bewegung erkannt${velocity === null ? " (kein Tempo messbar)"
                                            : ` (${Math.round(velocity)} km/h)`}`
          + " - Fahrt wird angelegt.");
      try {
        await createTrip(soc);
      } catch (failure) {
        // Nicht aufgeben: Die nächste Runde versucht es erneut. Ein
        // Funkloch beim Losfahren ist der Normalfall, nicht die Ausnahme.
        log("Fahrt anlegen: " + failure.message + " - nächste Runde erneut");
        moved = 0;
        return { soc, raw, waits: true,
                 records: { reason: "jolt nicht erreichbar" } };
      }
    }

    const payload = {
      lat: wo.lat, lon: wo.lon,
      soc: Math.round(soc.hmi * 10) / 10,
      raw_values: raw,
    };
    // Was das Auto selbst misst, schlägt jede Vorhersage: Die
    // Aussentemperatur ging bisher aus Open-Meteo ins Verbrauchsmodell.
    if (typeof raw.speed_kmh === "number") payload.speed_kmh = raw.speed_kmh;
    if (typeof raw.outside_temp_c === "number") {
      payload.outside_temp_c = raw.outside_temp_c;
    }

    /* Zwei Wege hinein, und welcher gilt, hängt daran, wer die Fahrt
     * angelegt hat. Hat diese Seite es getan, kennt sie die Sitzung und
     * meldet direkt dorthin. Läuft die Fahrt dagegen in der jolt-App auf
     * einem anderen Gerät, weiss diese Seite die Sitzung nicht - dann
     * weist sie sich mit dem Logger-Token des Fahrzeugs aus, und jolt
     * sucht die laufende Sitzung selbst. */
    const destination = sessionId
      ? `/api/live/${sessionId}/punkt`
      : "/api/live/melden";
    if (!sessionId) payload.token = el("token").value.trim();

    // `/punkt` verlangt die Anmeldung; `/melden` weist sich mit dem
    // Logger-Token im Rumpf aus und braucht den Header nicht, schadet er
    // aber auch nicht.
    const response = await fetch(destination, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Token": joltToken() },
      body: JSON.stringify(payload),
    });
    const records = await response.json().catch(() => ({}));
    // Der Sitzungsweg antwortet mit dem Zustand und kennt kein
    // "aufgenommen" - wenn er 200 gibt, ist der Punkt drin.
    if (sessionId && response.ok) records.recorded = true;
    return { soc, raw, records, status: response.status };
  }

  async function tripLoop() {
    while (running) {
      const onset = Date.now();
      try {
        const result = await aRound();
        if (result && result.waits) {
          // Im Wartezustand wird gemessen, aber nichts gemeldet. Angezeigt
          // wird trotzdem, was gelesen wurde - sonst sähe die Seite aus,
          // als täte sie nichts.
          tiles([
            ["Ladestand", Math.round(result.soc.hmi * 10) / 10 + " %"],
            ["Zustand", "wartet auf Fahrt"],
          ]);
          stand2("Bereit – " + (result.records.reason || "wartet"), "gut");
        } else if (result) {
          const { soc, raw, records } = result;
          lastSoc = Math.round(soc.hmi * 10) / 10;
          el("soc-wert").textContent = lastSoc + " %";
          const power = (typeof raw.voltage_v === "number"
                            && typeof raw.current_a === "number")
            ? (raw.voltage_v * raw.current_a / 1000).toFixed(1) + " kW" : "–";
          tiles([
            ["Ladestand", lastSoc + " %"],
            ["brutto", soc.bms.toFixed(1) + " %"],
            ["Leistung", power],
            ["Spannung", (raw.voltage_v ?? "–") + " V"],
            ["aufgenommen", records.recorded ? "ja" : "nein"],
            ["Runde", String(lap)],
          ]);
          stand2(records.recorded
            ? `läuft – zuletzt ${new Date().toLocaleTimeString("de-DE")}`
            : `läuft – jolt: ${records.reason || "nicht aufgenommen"}`,
            records.recorded ? "gut" : "");
          log(`Runde ${lap}: ${lastSoc} % (roh ${raw.soc_raw})`
              + `${raw._missing ? ", ohne " + raw._missing.join("/") : ""}`
              + ` → jolt ${records.recorded ? "ok" : (records.reason || "?")}`);
        }
      } catch (failure) {
        // Ein Aussetzer beendet die Fahrt nicht. Tunnel, Funkloch, ein
        // Steuergerät das gerade nicht mag - das nächste Mal klappt es
        // wieder, und eine abgebrochene Aufzeichnung merkt man erst hinterher.
        stand2("Aussetzer: " + failure.message, "schlecht");
        log("Runde übersprungen: " + failure.message);
      }
      const rest = Number(el("takt").value) * 1000 - (Date.now() - onset);
      await new Promise((w) => setTimeout(w, Math.max(1000, rest)));
    }
  }

  function stand2(text, variety) {
    const k = el("fahrt-stand");
    k.textContent = text;
    k.className = "stand " + (variety || "");
  }

  async function startTrip() {
    if (!el("token").value.trim() && !joltToken()) {
      stand2("Erst in jolt anmelden oder ein Logger-Token eintragen.",
             "schlecht");
      return;
    }
    running = true;
    lap = 0;
    el("fahrt-start").hidden = true;
    el("fahrt-stop").hidden = false;
    await screenAwakeHold();
    log("Aufzeichnung gestartet.");
    tripLoop();
  }

  async function endTrip() {
    running = false;
    // Die Fahrt in jolt abschliessen, wenn diese Seite sie angelegt hat.
    // Ohne das bleibt die Aufzeichnung offen, und aus den Messpunkten
    // entsteht nie eine Strecke - der ganze Zweck wäre verfehlt.
    if (sessionId) {
      try {
        const response = await fetch(`/api/live/${sessionId}/ende`, {
          method: "POST", headers: { "X-Token": joltToken() } });
        const records = await response.json().catch(() => ({}));
        const built = records.recording || {};
        if (built.ok) {
          log(`Fahrt abgeschlossen: ${built.distance_km} km, `
              + `${built.consumption_kwh} kWh gerechnet, Höhen aus `
              + `${built.elevations}.`);
        } else if (built.reason) {
          log("Fahrt nicht auswertbar: " + built.reason);
        }
        if (records.learned) {
          log(`Gelernt: Faktor ${records.learned.earlier} → `
              + `${records.learned.after} (Fahrt ×${records.learned.raw_factor})`);
        } else if (records.not_learned) {
          log("Nichts gelernt: " + records.not_learned);
        }
      } catch (failure) {
        log("Fahrt beenden: " + failure.message);
      }
      sessionId = null;
    }
    el("fahrt-start").hidden = false;
    el("fahrt-stop").hidden = true;
    stand2("beendet");
    if (wake_lock) {
      try { await wake_lock.release(); } catch (e) {}
      wake_lock = null;
    }
    log("Aufzeichnung beendet.");
  }

  /* ---------- Werte prüfen, ohne zu fahren ---------- */

  /* Was plausibel waere. Zwei Sorten Pruefung:
   *
   *  - **Bereich**: Liegt der Wert dort, wo er physikalisch liegen muss?
   *    Faengt Formelfehler ab - eine Akkukapazitaet von 3 kWh oder 8000.
   *  - **Kreuzvergleich**: Passen zwei unabhaengig gelesene Werte
   *    zueinander? Das ist der schaerfere Test. Entladezaehler geteilt
   *    durch Kilometerstand muss den Lebensdauerverbrauch ergeben - trifft
   *    er 15 bis 35 kWh/100 km, stimmen **beide** Formeln, und zwar ohne
   *    dass man je gefahren waere.
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
    /* Lebensdauerzaehler. Die Schranke war [1, 999999] und liess damit
     * 482 961 kWh durch - genau den Wert, den die fehlende
     * Vorzeichenbehandlung erzeugte. Eine Schranke, die den Fehler nicht
     * faengt, den sie fangen soll, ist keine. 100 000 kWh entsprechen bei
     * 20 kWh/100 km einer halben Million Kilometer. */
    discharge_kwh: [100, 100000, "Lebensdauerzähler"],
    charged_kwh: [100, 100000, "Lebensdauerzähler"],
  };

  function check_row(title, val, verdict, note) {
    const colour = verdict === "ok" ? "gut"
      : (verdict === "fehlt" ? "" : "schlecht");
    return `<tr class="${colour}"><th>${title}</th><td>${val}</td>`
      + `<td>${note || ""}</td></tr>`;
  }

  async function valuesCall() {
    const btn = el("pruefen");
    btn.disabled = true;
    const destination = el("pruef-ergebnis");
    destination.innerHTML = "<p>lese …</p>";
    try {
      if (!O.linked()) {
        await O.attach();
        if (!O.linked()) throw new Error("keine Verbindung zum Dongle");
        if (!(await O.handshake())) throw new Error("Handshake unvollständig");
      }
      // Runde 0 - damit auch die selten gelesenen Werte drankommen.
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

      /* Der Kreuzvergleich. Er braucht keine Fahrt und prueft zwei Formeln
       * auf einmal: Wenn Entladezaehler und Kilometerstand zusammen einen
       * sinnvollen Lebensdauerverbrauch ergeben, koennen beide kaum falsch
       * sein - ein Fehler in einer der beiden Byte-Lagen wuerde das
       * Ergebnis um Zehnerpotenzen verschieben. */
      if (typeof raw.discharge_kwh === "number"
          && typeof raw.odometer_km === "number" && raw.odometer_km > 100) {
        const net = raw.discharge_kwh
          - (typeof raw.charged_kwh === "number" ? raw.charged_kwh : 0);
        const je100 = raw.discharge_kwh / raw.odometer_km * 100;
        const inside = je100 >= 12 && je100 <= 40;
        // Mitzaehlen. Vorher stand er zwar rot in der Tabelle, aber die
        // Zeile darueber meldete trotzdem "0 auffaellig" - und die liest
        // man zuerst.
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

      destination.innerHTML =
        `<p><b>${good}</b> plausibel, <b>${bad}</b> auffällig, `
        + `<b>${without}</b> ohne Wert</p>`
        + `<table class="pruef"><tbody>${rows.join("")}</tbody></table>`;
      log(`Prüfung: ${good} plausibel, ${bad} auffällig, ${without} ohne Wert`);
    } catch (failure) {
      destination.innerHTML = `<p class="stand schlecht">${failure.message}</p>`;
      log("Prüfung: " + failure.message);
    } finally {
      btn.disabled = false;
    }
  }

  /* ---------- Klimakompressor eingrenzen ---------- */

  /* Die Antwort auf 220800 traegt vier 16-Bit-Zahlen, und keine der drei
   * Quellen (spot2000, WiCAN, codingABI) nennt eine Umrechnung. Raten
   * waere hier besonders verlockend und besonders falsch - vier Kandidaten,
   * alle im plausiblen Wattbereich.
   *
   * Eine Differenzmessung entscheidet es ohne jede Annahme: zweimal lesen,
   * einmal mit laufendem Kompressor und einmal ohne. Was sich um
   * Hunderte aendert, ist die Leistung; was gleich bleibt, ist etwas
   * anderes. */
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

  /* Beide Messungen nebeneinander.
   *
   * Die erste Fassung schob ein Zwei-Byte-Fenster **byteweise** durch die
   * Antwort und zeigte damit lauter ueberlappende Scheinwerte: "Byte 1-2 =
   * 9408" neben "Byte 2-3 = 49188", wobei nur der erste eine Groesse ist.
   * Eine Mehrbyte-Zahl faengt nicht an jedem Byte an.
   *
   * Jetzt beides getrennt: erst jedes Byte einzeln, dann die
   * **ausgerichteten** Paare ab Byte 1 - so, wie das Steuergeraet sie
   * meint. Was sich in beiden Spalten deutlich unterscheidet, ist der
   * Kandidat. */
  function showClimate() {
    const destination = el("klima-ergebnis");
    if (!climateA || !climateA.b) { destination.innerHTML = ""; return; }
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

    destination.innerHTML =
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

  /* ---------- An jolt melden ---------- */

  function city() {
    return new Promise((fulfil, reject) => {
      if (!navigator.geolocation) { reject(new Error("kein GPS")); return; }
      navigator.geolocation.getCurrentPosition(
        // Die GPS-Höhe wird mitgeschrieben, obwohl sie für die Steigung zu
        // ungenau ist (sie streut um zehn bis zwanzig Meter). Sie kostet
        // nichts und ist der Rückfall, wenn beim Abschliessen keine
        // Kartendaten zu bekommen sind.
        // `speed` kommt in m/s und ist oft null (kalter Fix, Standlauf).
        // Ohne diese Umrechnung war der GPS-Rueckfall der Bewegungserkennung
        // weiter unten toter Code: `wo.tempo_kmh` gab es schlicht nicht, und
        // ohne Tempo vom Auto legte die Automatik die Fahrt sofort an -
        // mitsamt dem Parkplatz davor.
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
      const wo = await city();
      const response = await fetch("/api/live/melden", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, soc: lastSoc,
                               lat: wo.lat, lon: wo.lon }),
      });
      const records = await response.json();
      log(`jolt: HTTP ${response.status} ${JSON.stringify(records).slice(0, 200)}`);
    } catch (failure) {
      log("FEHLER " + failure.message);
    }
  }

  /* ---------- Aufbau ---------- */

  // `O.verfuegbar()` statt `navigator.bluetooth`: In der iOS-App gibt es die
  // Web-API nicht, wohl aber Bluetooth - es kommt dort ueber CoreBluetooth
  // (siehe obd-ble-native.js). Wer hier direkt auf die Web-API prueft,
  // erklaert die Seite ausgerechnet dort fuer untauglich, wo sie am besten
  // funktioniert.
  if (!O.obtainable()) {
    el("untauglich").hidden = false;
    el("verbinden").disabled = true;
  }

  /* Fehler in readings.js gehoeren auf die Seite, nicht nur in die
   * Konsole. Ein vertippter Adressname sieht am Auto aus wie ein
   * schweigendes Steuergeraet - und danach sucht man an der falschen
   * Stelle. */
  if (O.TABLE_ERROR && O.TABLE_ERROR.length) {
    const box = el("untauglich");
    box.hidden = false;
    box.innerHTML = "<strong>Fehler in der Messwert-Tabelle "
      + "(readings.js):</strong><ul><li>"
      + O.TABLE_ERROR.map((t) => t.replace(/[<&]/g, "")).join("</li><li>")
      + "</li></ul>";
  }
  // Der Baustein meldet alles hierher, und ein Abriss ist während einer
  // Aufzeichnung ein Grund zum Wiederverbinden - sonst nicht.
  O.set_up(log, () => { if (running) O.reconnect(1, () => running); });
  el("verbinden").addEventListener("click", async () => {
    as_of("verbinde …");
    await O.attach();
    if (O.linked()) { as_of("verbunden", "gut"); buttons(true); }
    else { as_of("nicht verbunden", "schlecht"); }
  });
  el("init").addEventListener("click", async () => {
    if (await O.handshake()) log("Handshake durch.");
  });
  el("soc").addEventListener("click", readSoc);
  /* Der Knopf sperrt sich, solange die Reihe laeuft.
   *
   * Eine Mehrrahmen-Antwort braucht ueber eine Sekunde. Wer in der Zeit noch
   * einmal tippt, startet eine zweite Reihe, und die faellt dem ersten
   * Befehl in den Ruecken: "es laeuft noch ein Befehl". Im Protokoll sah es
   * danach aus, als haette das Steuergeraet nicht geantwortet - dabei war es
   * die Oberflaeche. */
  el("senden").addEventListener("click", async () => {
    const btn = el("senden");
    btn.disabled = true;
    const earlier = btn.textContent;
    btn.textContent = "läuft …";
    try {
      await O.series(el("frei").value.split("\n"));
    } finally {
      btn.disabled = false;
      btn.textContent = earlier;
    }
  });
  el("melden").addEventListener("click", report);
  el("pruefen").addEventListener("click", valuesCall);
  el("klima-a").addEventListener("click", async () => {
    const k = el("klima-a");
    k.disabled = true;
    try {
      climateA = { a: await readClimate(), b: null };
      log("Klima-Messung 1 (aus): " + climateA.a.join(" "));
      el("klima-ergebnis").innerHTML =
        "<p>Erste Messung steht. Jetzt die Klimaanlage <strong>kräftig "
        + "einschalten</strong> (kalt, hohe Gebläsestufe), eine halbe Minute "
        + "warten und dann die zweite Messung.</p>";
      el("klima-b").disabled = false;
    } catch (failure) {
      el("klima-ergebnis").innerHTML =
        `<p class="stand schlecht">${failure.message}</p>`;
      log("Klima-Messung 1: " + failure.message);
    } finally {
      k.disabled = false;
    }
  });
  el("klima-b").addEventListener("click", async () => {
    const k = el("klima-b");
    k.disabled = true;
    try {
      climateA.b = await readClimate();
      log("Klima-Messung 2 (an): " + climateA.b.join(" "));
      showClimate();
    } catch (failure) {
      el("klima-ergebnis").innerHTML =
        `<p class="stand schlecht">${failure.message}</p>`;
      log("Klima-Messung 2: " + failure.message);
    } finally {
      k.disabled = false;
    }
  });
  el("log-leeren").addEventListener("click", () => { el("log").textContent = ""; });
  /* Auf dem Telefon ist das Markieren in einem Kasten mit Bildlauf fummelig,
   * und ein Bildschirmfoto verliert genau das, worauf es ankommt: die
   * Hex-Antworten Zeichen für Zeichen. */
  el("los").addEventListener("click", start_driving);
  el("fahrt-start").addEventListener("click", startTrip);
  el("fahrt-stop").addEventListener("click", endTrip);
  el("takt").addEventListener("input", (e) => {
    el("takt-wert").textContent = e.target.value;
  });
  el("log-kopieren").addEventListener("click", async () => {
    const text = el("log").textContent;
    try {
      await navigator.clipboard.writeText(text);
      el("log-kopieren").textContent = "kopiert";
      setTimeout(() => { el("log-kopieren").textContent = "Protokoll kopieren"; }, 2000);
    } catch (failure) {
      // Ohne Zwischenablage (älterer Browser, fehlende Erlaubnis) bleibt das
      // Markieren von Hand - dann wenigstens alles auf einmal auswählen.
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
