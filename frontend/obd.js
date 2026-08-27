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
  let letzterSoc = null;

  /* ---------- Protokoll ---------- */

  function log(text, art) {
    const zeit = new Date().toLocaleTimeString("de-DE");
    const zeichen = art === "raus" ? "→" : (art === "rein" ? "←" : " ");
    el("log").textContent += `${zeit} ${zeichen} ${text}\n`;
    el("log").scrollTop = el("log").scrollHeight;
  }

  function stand(text, art) {
    const k = el("verbindung");
    k.textContent = text;
    k.className = "stand " + (art || "");
  }

  function knoepfe(an) {
    for (const id of ["init", "soc", "senden", "melden", "fahrt-start"]) el(id).disabled = !an;
  }

  /* ---------- Ladestand ---------- */

  async function socLesen() {
    el("soc-wert").textContent = "…";
    try {
      // Adresse und Filter stehen seit dem Handshake; sie hier erneut zu
      // setzen würde ATCP17 und ATCAF1 nicht wiederholen und damit gerade
      // das zerstören, worauf es ankommt.
      const antwort = await O.befehl("22028C");
      const wert = O.socAusAntwort(antwort);
      if (wert === null) {
        el("soc-wert").textContent = "?";
        log("Antwort enthält kein 62028C - siehe oben. Entweder ist die "
            + "Datenkennung eine andere, oder das Steuergerät antwortet "
            + "nicht auf dieser Kennung.");
        return;
      }
      letzterSoc = Math.round(wert.hmi * 10) / 10;
      // Beide Zahlen anzeigen: Die grosse ist die, die im Auto steht und die
      // jolt bekommt; die kleine daneben macht nachvollziehbar, woraus sie
      // entstanden ist.
      el("soc-wert").textContent = letzterSoc + " %";
      el("soc-herkunft").textContent =
        `Rohwert 0x${wert.roh.toString(16).toUpperCase()} = ${wert.roh}`
        + ` → brutto ${wert.bms.toFixed(1)} % → Anzeige ${wert.hmi.toFixed(1)} %`;
      log(`Ladestand: brutto ${wert.bms.toFixed(1)} %, `
          + `Anzeige ${wert.hmi.toFixed(1)} % (Rohwert ${wert.roh})`);
    } catch (fehler) {
      el("soc-wert").textContent = "–";
      log("FEHLER " + fehler.message);
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
  function losStand(text, art) {
    const k = el("los-stand");
    k.textContent = text;
    k.className = "stand " + (art || "");
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
  const FAEHRT_AB_KMH = 10;
  // Zwei Messungen hintereinander, damit ein einzelner Ausreisser keine
  // Fahrt anlegt.
  const FAEHRT_RUNDEN = 2;
  let bewegt = 0;

  /* Ein Name, den niemand tippen muss.
   *
   * Das Namensfeld war ein Handgriff zu viel: Wer im Auto sitzt, tippt
   * nichts. Datum und Uhrzeit sind ohnehin die Angabe, nach der man später
   * sucht - und Start und Ziel trägt jolt beim Abschliessen selbst nach,
   * aus dem ersten und letzten Messpunkt. */
  function fahrtName() {
    const eigener = el("fahrt-name").value.trim();
    if (eigener) return eigener;
    return new Date().toLocaleString("de-DE", {
      weekday: "short", day: "2-digit", month: "2-digit",
      hour: "2-digit", minute: "2-digit" });
  }

  async function losfahren() {
    const knopf = el("los");
    knopf.disabled = true;
    try {
      if (!O.verbunden()) {
        losStand("Dongle suchen …");
        await O.anschliessen();
        if (!O.verbunden()) throw new Error("keine Verbindung zum Dongle");
      }

      losStand("Steuergerät vorbereiten …");
      if (!(await O.handshake())) {
        throw new Error("Handshake unvollständig – siehe Protokoll");
      }

      // Erst prüfen, ob überhaupt etwas ankommt. Eine Aufzeichnung zu
      // starten, die dann nur Positionen ohne Ladestand sammelt, wäre eine
      // verlorene Fahrt - und das fiele erst am Ziel auf.
      losStand("Ladestand lesen …");
      const probe = O.socAusAntwort(await O.befehl("22028C"));
      if (!probe) throw new Error("Das Auto liefert keinen Ladestand");
      el("soc-wert").textContent = Math.round(probe.hmi * 10) / 10 + " %";

      if (el("automatik").checked) {
        // Nicht sofort anlegen: Wer im Stand verbindet, bekäme sonst eine
        // Fahrt, die an der Auffahrt beginnt und eine halbe Stunde
        // Parkplatz enthält. Die Seite wartet, bis sich etwas bewegt.
        losStand("Bereit – wartet, bis das Auto fährt.", "gut");
        laeuft = true;
        bewegt = 0;
        el("fahrt-start").hidden = true;
        el("fahrt-stop").hidden = false;
        await bildschirmWachHalten();
        log("Automatik: warte auf Bewegung.");
        fahrtSchleife();
        return;
      }

      await fahrtAnlegen(probe);
      await fahrtStarten();
    } catch (fehler) {
      losStand("Ging nicht: " + fehler.message, "schlecht");
      log("FEHLER " + fehler.message);
    } finally {
      knopf.disabled = false;
    }
  }

  /* Die Fahrt in jolt anlegen. Getrennt vom Verbinden, weil sie bei
   * eingeschalteter Automatik erst entsteht, wenn das Auto losfährt. */
  async function fahrtAnlegen(soc) {
    losStand("Fahrt anlegen …");
    const wo = await ort();
    const antwort = await fetch("/api/live/aufzeichnung", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Token": joltToken() },
      body: JSON.stringify({
        fahrzeug_id: fahrzeugId(),
        lat: wo.lat, lon: wo.lon,
        soc: soc ? Math.round(soc.hmi * 10) / 10 : null,
        name: fahrtName() }),
    });
    if (!antwort.ok) {
      const fehler = await antwort.json().catch(() => ({}));
      throw new Error(fehler.detail || `jolt antwortet HTTP ${antwort.status}`);
    }
    const fahrt = await antwort.json();
    sitzungId = fahrt.sitzung_id;
    log(`Aufzeichnung ${fahrt.fahrt_id} läuft (Sitzung ${fahrt.sitzung_id}).`);
    losStand(`Aufzeichnung läuft – Fahrt ${fahrt.fahrt_id}`, "gut");
    return fahrt;
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
  async function fahrzeugeLaden() {
    const auswahl = el("fahrzeug-wahl-obd");
    if (!auswahl) return;
    try {
      const antwort = await fetch("/api/fahrzeuge",
                                  { headers: { "X-Token": joltToken() } });
      if (!antwort.ok) throw new Error(`HTTP ${antwort.status}`);
      const fahrzeuge = await antwort.json();
      let gemerkt = null;
      try { gemerkt = localStorage.getItem("jolt-obd-fahrzeug"); } catch (e) {}
      auswahl.innerHTML = fahrzeuge
        .map((f) => `<option value="${f.id}">${f.name}</option>`).join("");
      if (gemerkt && fahrzeuge.some((f) => String(f.id) === gemerkt)) {
        auswahl.value = gemerkt;
      }
      auswahl.addEventListener("change", () => {
        try { localStorage.setItem("jolt-obd-fahrzeug", auswahl.value); }
        catch (e) {}
      });
    } catch (fehler) {
      log("Fahrzeugliste: " + fehler.message
          + " - erst in jolt anmelden, dann hier neu laden.");
    }
  }

  function fahrzeugId() {
    const auswahl = el("fahrzeug-wahl-obd");
    if (!auswahl || !auswahl.value) {
      throw new Error("Kein Fahrzeug gewählt - erst in jolt anmelden, "
                      + "dann hier neu laden.");
    }
    return Number(auswahl.value);
  }

  /* ---------- Aufzeichnung ---------- */

  let laeuft = false;
  let runde = 0;
  let wachhalter = null;   // WakeLockSentinel
  let sitzungId = null;    // gesetzt, wenn diese Seite die Fahrt anlegte

  /* Den Bildschirm wach halten. Ohne das schaltet iOS ihn nach einer Minute
   * aus, und mit dem Bildschirm schläft der Seiteninhalt - die Verbindung
   * übersteht zwar den Sperrbildschirm, die Schleife aber nicht.
   *
   * Die Sperre geht verloren, wenn die Seite in den Hintergrund gerät, und
   * kommt nicht von selbst zurück; deshalb wird sie beim Zurückkommen neu
   * geholt. Kennt der Browser die Schnittstelle nicht, läuft die
   * Aufzeichnung trotzdem - dann muss man den Bildschirm eben in den
   * Einstellungen an lassen. */
  async function bildschirmWachHalten() {
    if (!("wakeLock" in navigator)) {
      log("Dieser Browser kennt keine Bildschirmsperre-Verhinderung. "
          + "Automatische Sperre bitte in den iOS-Einstellungen auf 'Nie'.");
      return;
    }
    try {
      wachhalter = await navigator.wakeLock.request("screen");
      log("Bildschirm wird wachgehalten.");
    } catch (fehler) {
      log("Bildschirm wachhalten ging nicht: " + fehler.message);
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (laeuft && document.visibilityState === "visible" && !wachhalter) {
      bildschirmWachHalten();
    }
  });

  function kacheln(werte) {
    el("fahrt-werte").innerHTML = werte.map(([name, zahl]) =>
      `<div class="wert"><div class="zahl">${zahl}</div>`
      + `<div class="name">${name}</div></div>`).join("");
  }

  async function eineRunde() {
    const roh = await O.satzLesen(runde);
    runde += 1;

    const soc = O.socAusRoh(roh.soc_roh);
    const wo = await ort().catch((f) => {
      log("Standort: " + f.message);
      return null;
    });
    if (!wo) return null;

    if (typeof wo.hoehe_m === "number") roh.hoehe_m = Math.round(wo.hoehe_m);

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
    if (!sitzungId && el("automatik").checked && !el("token").value.trim()) {
      const tempo = typeof roh.tempo_kmh === "number" ? roh.tempo_kmh
        : (typeof wo.tempo_kmh === "number" ? wo.tempo_kmh : null);
      if (tempo !== null && tempo < FAEHRT_AB_KMH) {
        bewegt = 0;
        return { soc, roh, wartet: true,
                 daten: { grund: `steht (${Math.round(tempo)} km/h)` } };
      }
      bewegt += 1;
      if (tempo !== null && bewegt < FAEHRT_RUNDEN) {
        return { soc, roh, wartet: true,
                 daten: { grund: `fährt an (${Math.round(tempo)} km/h)` } };
      }
      log(`Bewegung erkannt${tempo === null ? " (kein Tempo messbar)"
                                            : ` (${Math.round(tempo)} km/h)`}`
          + " - Fahrt wird angelegt.");
      try {
        await fahrtAnlegen(soc);
      } catch (fehler) {
        // Nicht aufgeben: Die nächste Runde versucht es erneut. Ein
        // Funkloch beim Losfahren ist der Normalfall, nicht die Ausnahme.
        log("Fahrt anlegen: " + fehler.message + " - nächste Runde erneut");
        bewegt = 0;
        return { soc, roh, wartet: true,
                 daten: { grund: "jolt nicht erreichbar" } };
      }
    }

    const nutzlast = {
      lat: wo.lat, lon: wo.lon,
      soc: Math.round(soc.hmi * 10) / 10,
      rohwerte: roh,
    };
    // Was das Auto selbst misst, schlägt jede Vorhersage: Die
    // Aussentemperatur ging bisher aus Open-Meteo ins Verbrauchsmodell.
    if (typeof roh.tempo_kmh === "number") nutzlast.tempo_kmh = roh.tempo_kmh;
    if (typeof roh.aussentemp_c === "number") {
      nutzlast.aussentemp_c = roh.aussentemp_c;
    }

    /* Zwei Wege hinein, und welcher gilt, hängt daran, wer die Fahrt
     * angelegt hat. Hat diese Seite es getan, kennt sie die Sitzung und
     * meldet direkt dorthin. Läuft die Fahrt dagegen in der jolt-App auf
     * einem anderen Gerät, weiss diese Seite die Sitzung nicht - dann
     * weist sie sich mit dem Logger-Token des Fahrzeugs aus, und jolt
     * sucht die laufende Sitzung selbst. */
    const ziel = sitzungId
      ? `/api/live/${sitzungId}/punkt`
      : "/api/live/melden";
    if (!sitzungId) nutzlast.token = el("token").value.trim();

    const antwort = await fetch(ziel, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(nutzlast),
    });
    const daten = await antwort.json().catch(() => ({}));
    // Der Sitzungsweg antwortet mit dem Zustand und kennt kein
    // "aufgenommen" - wenn er 200 gibt, ist der Punkt drin.
    if (sitzungId && antwort.ok) daten.aufgenommen = true;
    return { soc, roh, daten, status: antwort.status };
  }

  async function fahrtSchleife() {
    while (laeuft) {
      const beginn = Date.now();
      try {
        const ergebnis = await eineRunde();
        if (ergebnis && ergebnis.wartet) {
          // Im Wartezustand wird gemessen, aber nichts gemeldet. Angezeigt
          // wird trotzdem, was gelesen wurde - sonst sähe die Seite aus,
          // als täte sie nichts.
          kacheln([
            ["Ladestand", Math.round(ergebnis.soc.hmi * 10) / 10 + " %"],
            ["Zustand", "wartet auf Fahrt"],
          ]);
          stand2("Bereit – " + (ergebnis.daten.grund || "wartet"), "gut");
        } else if (ergebnis) {
          const { soc, roh, daten } = ergebnis;
          letzterSoc = Math.round(soc.hmi * 10) / 10;
          el("soc-wert").textContent = letzterSoc + " %";
          const leistung = (typeof roh.spannung_v === "number"
                            && typeof roh.strom_a === "number")
            ? (roh.spannung_v * roh.strom_a / 1000).toFixed(1) + " kW" : "–";
          kacheln([
            ["Ladestand", letzterSoc + " %"],
            ["brutto", soc.bms.toFixed(1) + " %"],
            ["Leistung", leistung],
            ["Spannung", (roh.spannung_v ?? "–") + " V"],
            ["aufgenommen", daten.aufgenommen ? "ja" : "nein"],
            ["Runde", String(runde)],
          ]);
          stand2(daten.aufgenommen
            ? `läuft – zuletzt ${new Date().toLocaleTimeString("de-DE")}`
            : `läuft – jolt: ${daten.grund || "nicht aufgenommen"}`,
            daten.aufgenommen ? "gut" : "");
          log(`Runde ${runde}: ${letzterSoc} % (roh ${roh.soc_roh})`
              + `${roh._fehlend ? ", ohne " + roh._fehlend.join("/") : ""}`
              + ` → jolt ${daten.aufgenommen ? "ok" : (daten.grund || "?")}`);
        }
      } catch (fehler) {
        // Ein Aussetzer beendet die Fahrt nicht. Tunnel, Funkloch, ein
        // Steuergerät das gerade nicht mag - das nächste Mal klappt es
        // wieder, und eine abgebrochene Aufzeichnung merkt man erst hinterher.
        stand2("Aussetzer: " + fehler.message, "schlecht");
        log("Runde übersprungen: " + fehler.message);
              // Adresse neu setzen, sicher ist sicher
      }
      const rest = Number(el("takt").value) * 1000 - (Date.now() - beginn);
      await new Promise((w) => setTimeout(w, Math.max(1000, rest)));
    }
  }

  function stand2(text, art) {
    const k = el("fahrt-stand");
    k.textContent = text;
    k.className = "stand " + (art || "");
  }

  async function fahrtStarten() {
    if (!el("token").value.trim() && !joltToken()) {
      stand2("Erst in jolt anmelden oder ein Logger-Token eintragen.",
             "schlecht");
      return;
    }
    laeuft = true;
    runde = 0;
    el("fahrt-start").hidden = true;
    el("fahrt-stop").hidden = false;
    await bildschirmWachHalten();
    log("Aufzeichnung gestartet.");
    fahrtSchleife();
  }

  async function fahrtBeenden() {
    laeuft = false;
    // Die Fahrt in jolt abschliessen, wenn diese Seite sie angelegt hat.
    // Ohne das bleibt die Aufzeichnung offen, und aus den Messpunkten
    // entsteht nie eine Strecke - der ganze Zweck wäre verfehlt.
    if (sitzungId) {
      try {
        const antwort = await fetch(`/api/live/${sitzungId}/ende`, {
          method: "POST", headers: { "X-Token": joltToken() } });
        const daten = await antwort.json().catch(() => ({}));
        const gebaut = daten.aufzeichnung || {};
        if (gebaut.ok) {
          log(`Fahrt abgeschlossen: ${gebaut.strecke_km} km, `
              + `${gebaut.verbrauch_kwh} kWh gerechnet, Höhen aus `
              + `${gebaut.hoehen}.`);
        } else if (gebaut.grund) {
          log("Fahrt nicht auswertbar: " + gebaut.grund);
        }
        if (daten.gelernt) {
          log(`Gelernt: Faktor ${daten.gelernt.vorher} → `
              + `${daten.gelernt.nachher} (Fahrt ×${daten.gelernt.rohfaktor})`);
        } else if (daten.nicht_gelernt) {
          log("Nichts gelernt: " + daten.nicht_gelernt);
        }
      } catch (fehler) {
        log("Fahrt beenden: " + fehler.message);
      }
      sitzungId = null;
    }
    el("fahrt-start").hidden = false;
    el("fahrt-stop").hidden = true;
    stand2("beendet");
    if (wachhalter) {
      try { await wachhalter.release(); } catch (e) {}
      wachhalter = null;
    }
    log("Aufzeichnung beendet.");
  }

  /* ---------- An jolt melden ---------- */

  function ort() {
    return new Promise((erfuellen, ablehnen) => {
      if (!navigator.geolocation) { ablehnen(new Error("kein GPS")); return; }
      navigator.geolocation.getCurrentPosition(
        // Die GPS-Höhe wird mitgeschrieben, obwohl sie für die Steigung zu
        // ungenau ist (sie streut um zehn bis zwanzig Meter). Sie kostet
        // nichts und ist der Rückfall, wenn beim Abschliessen keine
        // Kartendaten zu bekommen sind.
        (p) => erfuellen({ lat: p.coords.latitude, lon: p.coords.longitude,
                           hoehe_m: p.coords.altitude }),
        (f) => ablehnen(new Error("Standort: " + f.message)),
        { enableHighAccuracy: true, timeout: 10000 });
    });
  }

  async function melden() {
    const token = el("token").value.trim();
    if (!token) { log("Kein Logger-Token eingetragen."); return; }
    if (letzterSoc === null) { log("Erst den Ladestand abfragen."); return; }
    try {
      const wo = await ort();
      const antwort = await fetch("/api/live/melden", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, soc: letzterSoc,
                               lat: wo.lat, lon: wo.lon }),
      });
      const daten = await antwort.json();
      log(`jolt: HTTP ${antwort.status} ${JSON.stringify(daten).slice(0, 200)}`);
    } catch (fehler) {
      log("FEHLER " + fehler.message);
    }
  }

  /* ---------- Aufbau ---------- */

  if (!navigator.bluetooth) {
    el("untauglich").hidden = false;
    el("verbinden").disabled = true;
  }
  // Der Baustein meldet alles hierher, und ein Abriss ist während einer
  // Aufzeichnung ein Grund zum Wiederverbinden - sonst nicht.
  O.einrichten(log, () => { if (laeuft) O.wiederverbinden(1, () => laeuft); });
  el("verbinden").addEventListener("click", async () => {
    stand("verbinde …");
    await O.anschliessen();
    if (O.verbunden()) { stand("verbunden", "gut"); knoepfe(true); }
    else { stand("nicht verbunden", "schlecht"); }
  });
  el("init").addEventListener("click", async () => {
    if (await O.handshake()) log("Handshake durch.");
  });
  el("soc").addEventListener("click", socLesen);
  el("senden").addEventListener("click", () => O.reihe(el("frei").value.split("\n")));
  el("melden").addEventListener("click", melden);
  el("log-leeren").addEventListener("click", () => { el("log").textContent = ""; });
  /* Auf dem Telefon ist das Markieren in einem Kasten mit Bildlauf fummelig,
   * und ein Bildschirmfoto verliert genau das, worauf es ankommt: die
   * Hex-Antworten Zeichen für Zeichen. */
  el("los").addEventListener("click", losfahren);
  el("fahrt-start").addEventListener("click", fahrtStarten);
  el("fahrt-stop").addEventListener("click", fahrtBeenden);
  el("takt").addEventListener("input", (e) => {
    el("takt-wert").textContent = e.target.value;
  });
  el("log-kopieren").addEventListener("click", async () => {
    const text = el("log").textContent;
    try {
      await navigator.clipboard.writeText(text);
      el("log-kopieren").textContent = "kopiert";
      setTimeout(() => { el("log-kopieren").textContent = "Protokoll kopieren"; }, 2000);
    } catch (fehler) {
      // Ohne Zwischenablage (älterer Browser, fehlende Erlaubnis) bleibt das
      // Markieren von Hand - dann wenigstens alles auf einmal auswählen.
      const bereich = document.createRange();
      bereich.selectNodeContents(el("log"));
      const auswahl = window.getSelection();
      auswahl.removeAllRanges();
      auswahl.addRange(bereich);
      log("Zwischenablage nicht verfügbar - Protokoll ist markiert, bitte "
          + "von Hand kopieren.");
    }
  });
  fahrzeugeLaden();
  log("Bereit. Dongle einstecken, Zündung an, dann „Fahrt starten“.");
})();
