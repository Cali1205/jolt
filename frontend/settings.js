/* Einstellungen und Dongle-Diagnose.
 *
 * Was man selten braucht und trotzdem finden will: Konto, Benachrichtigungen,
 * Dongle, Speicher - und die Diagnose des OBD2-Dongles. Die Diagnose steht
 * hier, weil man sie ansieht, wenn etwas nicht stimmt ("warum fehlt der
 * Strom?"), nicht beim Fahren.
 *
 * Alles an der Dongle-Diagnose kommt aus `joltObd.diagnose()` und
 * `joltObd.protokoll()`. Das Modul führt selbst Buch; diese Datei zeigt nur an
 * und fasst nichts an, was die Messung verändert - mit einer Ausnahme, der
 * Befehlskonsole, und die warnt, wenn gerade eine Fahrt läuft.
 *
 * Aktualisiert wird nur, solange die Ansicht offen ist: Eine Tabelle, die im
 * Hintergrund jede Sekunde neu gebaut wird, kostet im Auto Akku für nichts.
 */
window.joltSettings = (function () {
  "use strict";

  const K = window.jolt;
  const O = window.joltObd;
  const el = (id) => document.getElementById(id);

  let clock = null;
  let status = null;          // Antwort von /api/status, einmal je Öffnen

  /* ---------- Hilfen ---------- */

  /* Namen von Geräten und Texte aus dem Protokoll stammen von aussen. */
  function esc(text) {
    const helper = document.createElement("div");
    helper.textContent = text === null || text === undefined ? "" : String(text);
    return helper.innerHTML;
  }

  /* "vor 12 s", "vor 3 min" - für das Alter eines Werts. */
  function age(timestamp) {
    if (!timestamp) return "–";
    const s = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
    if (s < 90) return `vor ${s} s`;
    if (s < 5400) return `vor ${Math.round(s / 60)} min`;
    return `vor ${(s / 3600).toFixed(1).replace(".", ",")} h`;
  }

  function duration(ms) {
    return ms === null || ms === undefined ? "–"
      : (ms >= 1000 ? K.num(ms / 1000, 1) + " s" : Math.round(ms) + " ms");
  }

  function row(name, val, variety) {
    return `<dt>${esc(name)}</dt><dd${variety ? ` class="${variety}"` : ""}>${val}</dd>`;
  }

  function time_of_day(ms) {
    const d = new Date(ms);
    const two = (n) => String(n).padStart(2, "0");
    return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
  }

  function tripRunning() {
    return !!K.state.sessionId;
  }

  /* ---------- Konto und Server ---------- */

  async function showServer() {
    const as_of = el("stand");
    const rows = [];
    rows.push(row("Code-Stand", esc(as_of ? as_of.textContent : "–")));
    try {
      status = await K.api("/api/status");
      rows.push(row("Routing",
        status.demo_routing ? "Demo – erfundene Routen" : "echt (OpenRouteService)",
        status.demo_routing ? "warnung" : "gut"));
      rows.push(row("Passwortschutz",
        status.password_required ? "an" : "aus – jeder im Netz darf herein",
        status.password_required ? "gut" : "warnung"));
    } catch (failure) {
      rows.push(row("Server", "nicht erreichbar", "schlecht"));
    }
    rows.push(row("Angemeldet", K.token() ? "ja, auf diesem Gerät" : "nein"));
    rows.push(row("Oberfläche", esc(location.origin)));
    el("einst-server").innerHTML = rows.join("");
    el("einst-abmelden").hidden = !K.token();
  }

  async function sign_out() {
    try { await K.api("/api/logout", { method: "POST" }); }
    catch (failure) { /* Token ist ohnehin weg oder der Server nicht erreichbar */ }
    K.setToken("");
    // Neu laden: Der Start prüft die Anmeldung und zeigt dann das Passwortfeld.
    location.reload();
  }

  /* ---------- Benachrichtigungen ---------- */

  async function subscription() {
    const registrierung = K.state.serviceWorker
      || (navigator.serviceWorker && await navigator.serviceWorker.ready.catch(() => null));
    if (!registrierung || !registrierung.pushManager) return null;
    return registrierung.pushManager.getSubscription();
  }

  async function showPush() {
    const rows = [];
    const supported = "Notification" in window && "PushManager" in window;
    rows.push(row("Dieses Gerät",
      supported ? "kann Benachrichtigungen"
        : "kann keine Benachrichtigungen (in Safari nur als Home-Bildschirm-App)",
      supported ? "gut" : "warnung"));
    let serverReady = false;
    try {
      serverReady = (await K.api("/api/push/schluessel")).configured;
    } catch (failure) { /* unten als "nicht erreichbar" */ }
    rows.push(row("Server", serverReady ? "bereit"
      : "ohne VAPID-Schlüssel – Benachrichtigungen aus", serverReady ? "gut" : "warnung"));
    if (supported) {
      const grant = Notification.permission;
      rows.push(row("Erlaubnis",
        { granted: "erteilt", denied: "abgelehnt (in den Browser-Einstellungen ändern)",
          default: "noch nicht gefragt" }[grant] || grant,
        grant === "granted" ? "gut" : (grant === "denied" ? "schlecht" : "")));
      let signed_in = false;
      try { signed_in = !!(await subscription()); } catch (failure) { /* unbekannt */ }
      rows.push(row("Abo", signed_in ? "angemeldet" : "keines",
        signed_in ? "gut" : ""));
      el("einst-push-aus").hidden = !signed_in;
    }
    el("einst-push").innerHTML = rows.join("");
    el("einst-push-an").disabled = !supported || !serverReady;
    el("einst-push-probe").disabled = !serverReady;
  }

  async function activatePush() {
    if (!window.joltLive || !window.joltLive.notificationsSetUp) return;
    await window.joltLive.notificationsSetUp();
    await showPush();
  }

  async function signOutPush() {
    try {
      const latest = await subscription();
      if (latest) {
        const endpoint = latest.endpoint;
        await latest.unsubscribe();
        await K.api("/api/push/abo", { method: "DELETE", body: { endpoint } });
      }
      K.report("Dieses Gerät erhält keine Benachrichtigungen mehr.", "hinweis");
    } catch (failure) {
      K.report("Abmelden: " + failure.message, "fehler");
    }
    await showPush();
  }

  async function pushProbe() {
    try {
      const response = await K.api("/api/push/probe", { method: "POST" });
      K.report("Testnachricht verschickt: " + JSON.stringify(response), "hinweis");
    } catch (failure) {
      K.report("Testnachricht: " + failure.message, "fehler");
    }
  }

  /* ---------- Dongle-Einstellungen ---------- */

  /* Dieselbe Einstellung wie das Häkchen in der Live-Ansicht: Dessen
   * Änderungs-Handler setzt Zustand und Speicher, also wird er ausgelöst,
   * statt die Logik hier ein zweites Mal zu führen. */
  function autoAdopt() {
    const live = el("dongle-auto");
    if (!live) return;
    live.checked = el("einst-auto").checked;
    live.dispatchEvent(new Event("change"));
  }

  function showDongleSettings() {
    const live = el("dongle-auto");
    if (live) el("einst-auto").checked = live.checked;
    el("einst-trennen").disabled = !O.linked();
  }

  /* Den Dongle verbinden - erst ohne Dialog (bekanntes Gerät), dann mit.
   *
   * Nach dem Parken ist er getrennt (jolt fragt im Stand nichts, damit die
   * Alarmanlage ruhig bleibt), und hier fehlte bisher jede Möglichkeit, ihn
   * wieder zu verbinden - die Einstellungen verwiesen auf andere Ansichten.
   * Wer von hier aus mithören oder einen Befehl senden wollte, stand vor
   * "Kein Dongle verbunden". Es wird nur verbunden; gelesen wird nichts. */
  async function connectDongle() {
    if (!O.obtainable()) {
      throw new Error("Dieser Browser kann kein Bluetooth. In der iOS-App "
        + "oder in Bluefy geht es.");
    }
    if (O.linked()) return;
    await O.attach();
    if (!O.linked()) throw new Error("keine Verbindung zum Dongle");
  }

  async function connectButton() {
    const btn = el("einst-verbinden");
    const as_of = el("einst-verbinden-stand");
    btn.disabled = true;
    as_of.textContent = "verbinde …";
    try {
      await connectDongle();
      as_of.textContent = "Verbunden: " + (O.diagnose().connection.device || "Dongle")
        + ". Es wird nichts gelesen, solange keine Fahrt läuft.";
    } catch (failure) {
      // Im Text und nicht als Meldung: Eine Meldung verschwindet nach sechs
      // Sekunden, und wer sie nicht gesehen hat, weiss nicht, warum nichts geschah.
      as_of.textContent = "Nicht verbunden: " + failure.message;
    } finally {
      btn.disabled = false;
      refresh();
    }
  }

  function detach() {
    // Die Live-Ansicht hält ihren eigenen Zustand (Pause, Auto-Modus); über
    // ihren Knopf läuft das sauber, direkt am Modul bliebe sie im Glauben.
    const pause = el("dongle-pause");
    if (pause && !pause.hidden) { pause.click(); return; }
    O.detach();
  }

  function forget() {
    if (!window.confirm("Das gemerkte Gerät vergessen? Beim nächsten Verbinden "
        + "fragt jolt wieder, welcher Dongle es sein soll.")) return;
    O.forget();
    K.report("Gerät vergessen.", "hinweis");
  }

  /* ---------- Diagnose: Verbindung ---------- */

  function showConnection(d) {
    const v = d.connection;
    const z = [];
    z.push(row("Zugang", { native: "nativ (iOS-App, CoreBluetooth)",
      web: "Web Bluetooth (Browser)", none: "keiner – dieser Browser kann es nicht" }[d.transport],
      d.transport === "keiner" ? "schlecht" : "gut"));
    z.push(row("Zustand", d.linked ? "verbunden" : "getrennt",
      d.linked ? "gut" : "warnung"));
    z.push(row("Gerät", esc(v.device || d.rememberedDevice || "–")));
    z.push(row("Verbunden seit", v.since ? `${age(v.since)} (${time_of_day(v.since)})` : "–"));
    z.push(row("Letzte Antwort", age(d.commands.lastReception),
      d.linked && d.commands.lastReception
        && Date.now() - d.commands.lastReception > 120000 ? "warnung" : ""));
    z.push(row("Abrisse",
      `${v.dropouts}` + (v.retries ? `, ${v.retries} Wiederverbindungs­versuche` : ""),
      v.dropouts > 2 ? "warnung" : ""));
    if (d.changeFailed.length) {
      z.push(row("Protokollwechsel gescheitert",
        esc(d.changeFailed.join(", ")) + " – in dieser Sitzung nicht mehr versucht",
        "schlecht"));
    }
    if (d.tablesError.length) {
      z.push(row("Fehler in readings.js", esc(d.tablesError.join(" · ")), "schlecht"));
    }
    el("diag-verbindung").innerHTML = z.join("");

    const short = !d.linked ? "getrennt"
      : (d.rounds.n ? `${d.rounds.n} Runden` : "verbunden");
    el("einst-diag-kurz").textContent = short;
  }

  /* ---------- Diagnose: Messwerte ---------- */

  /* Eine Zeile je Messgrösse der Tabelle (readings.js), samt der Werte, die
   * aus derselben Antwort mitkommen. Die Zähler führt das Modul je
   * Hauptwert; Mitläufer zeigen nur ihren letzten Wert. */
  function showReadings(d) {
    const record = d.lastRecord;
    const header = "<tr><th>Messgrösse</th><th>Wert</th><th>Alter</th>"
      + "<th>ok</th><th>leer</th><th>aus</th><th>Dauer</th></tr>";
    const rows = [];
    for (const field of O.FIELDS) {
      const count = d.readings[field.name];
      const val = record && record.vals[field.name] !== undefined
        ? record.vals[field.name] : (count ? count.val : null);
      const unit = field.unit ? " " + field.unit : "";
      const text = val === null || val === undefined ? "–"
        : K.num(val, field.put) + unit;
      const as_of = count ? count.timestamp : (record && val !== null && val !== undefined ? record.timestamp : null);
      const required = field.required ? " <small>(Pflicht)</small>" : "";
      if (!count) {
        // Mitläufer oder noch nie gelesen.
        rows.push(`<tr><td>${esc(field.title)}${required}</td><td class="still">${text}</td>`
          + `<td class="still">${age(as_of)}</td>`
          + `<td class="still">·</td><td class="still">·</td><td class="still">·</td>`
          + `<td class="still">·</td></tr>`);
        continue;
      }
      const total = count.ok + count.empty + count.failure;
      const origin_of = count.failure;
      rows.push(`<tr><td>${esc(field.title)}${required}</td><td>${text}</td>`
        + `<td>${age(as_of)}</td>`
        + `<td>${count.ok}</td>`
        + `<td class="${count.empty ? "warnung" : "still"}">${count.empty}</td>`
        + `<td class="${origin_of && origin_of * 4 > total ? "schlecht" : (origin_of ? "warnung" : "still")}">${origin_of}</td>`
        + `<td>${duration(total ? count.sumMs / total : null)}</td></tr>`);
    }
    el("diag-messwerte").innerHTML = header + rows.join("");
  }

  function showRounds(d) {
    const r = d.rounds;
    const b = d.commands;
    const z = [];
    z.push(row("Runden", r.n ? `${r.n}, davon ${r.failure} gescheitert` : "noch keine",
      r.failure && r.failure * 4 > r.n ? "schlecht" : ""));
    z.push(row("Dauer je Runde", r.n
      ? `Ø ${duration(r.sumMs / r.n)}, zuletzt ${duration(r.latestMs)}` : "–"));
    z.push(row("Letzte Runde", age(r.timestamp)));
    z.push(row("Befehle", `${b.sent} gesendet, ${b.answered} beantwortet`));
    z.push(row("Antwortzeit", b.answered
      ? `Ø ${duration(b.sumMs / b.answered)}, zuletzt ${duration(b.latestMs)}` : "–"));
    z.push(row("Ohne Antwort", `${b.timeout}`
      + (b.delayed ? `, ${b.delayed} verspätet eingetroffen` : ""),
      b.timeout ? "warnung" : ""));
    if (d.commandRunning) z.push(row("Gerade", "ein Befehl wartet auf Antwort"));
    el("diag-runden").innerHTML = z.join("");
  }

  /* ---------- Diagnose: Protokoll ---------- */

  const CONSPICUOUS = /FEHLER|Zeitüberschreitung|keine Antwort|verspätet|fehlgeschlagen|NO DATA|ERROR|UNABLE|BUS/i;

  function showLog() {
    const container = el("diag-protokoll");
    const rows = O.trace_log(el("diag-auffaellig").checked).slice(-250);
    // Nur ans Ende springen, wenn man schon unten war - wer hochgescrollt hat,
    // um etwas zu lesen, soll nicht jede Sekunde zurückgerissen werden.
    const bottom = container.scrollHeight - container.scrollTop - container.clientHeight < 30;
    container.innerHTML = rows.map((z) => {
      const category = CONSPICUOUS.test(z.text) ? "auff" : (z.variety === "rein" ? "rein"
        : (z.variety === "raus" ? "raus" : ""));
      const arrow = z.variety === "raus" ? "→ " : (z.variety === "rein" ? "← " : "  ");
      return `<span class="zeit">${time_of_day(z.timestamp)}</span> `
        + `<span class="${category}">${arrow}${esc(z.text)}</span>`;
    }).join("\n") || "(noch nichts protokolliert – erst verbinden)";
    if (bottom) container.scrollTop = container.scrollHeight;
  }

  /* ---------- Diagnose: Bericht ---------- */

  function writeup() {
    const d = O.diagnose();
    const as_of = el("stand");
    const header = [
      "jolt-Diagnosebericht",
      "Stand: " + (as_of ? as_of.textContent : "?"),
      "Zeit: " + new Date().toISOString(),
      "Gerät: " + navigator.userAgent,
      "Zugang: " + d.transport + ", " + (d.linked ? "verbunden" : "getrennt"),
      "Fahrt läuft: " + (tripRunning() ? "ja" : "nein"),
    ];
    const readings = O.FIELDS.map((f) => {
      const z = d.readings[f.name];
      if (!z) return `  ${f.name}: nicht gelesen`;
      return `  ${f.name}: ok ${z.ok}, leer ${z.empty}, aus ${z.failure}, `
        + `Ø ${z.ok + z.empty + z.failure ? Math.round(z.sumMs / (z.ok + z.empty + z.failure)) : "–"} ms, `
        + `letzter Wert ${z.val}`;
    });
    const log = O.trace_log(false).slice(-150)
      .map((z) => `${time_of_day(z.timestamp)} ${z.variety === "raus" ? "->" : (z.variety === "rein" ? "<-" : "  ")} ${z.text}`);
    return [...header, "", "Verbindung / Zähler:", JSON.stringify({
      connection: d.connection, commands: d.commands, rounds: d.rounds,
      changeFailed: d.changeFailed, tablesError: d.tablesError,
      latestAddress: d.latestAddress }, null, 1),
      "", "Messwerte:", ...readings, "", "Protokoll (letzte 150 Zeilen):", ...log].join("\n");
  }

  async function copyWriteup() {
    const text = writeup();
    try {
      await navigator.clipboard.writeText(text);
      K.report("Bericht in die Zwischenablage kopiert.", "hinweis");
    } catch (failure) {
      // Ohne Zwischenablage-Erlaubnis (unsicherer Kontext, ältere WebViews):
      // in das Protokollfeld schreiben, markiert, zum Händisch-Kopieren.
      const field = el("diag-protokoll");
      field.textContent = text;
      const selection = window.getSelection();
      const zone = document.createRange();
      zone.selectNodeContents(field);
      selection.removeAllRanges();
      selection.addRange(zone);
      K.report("Kopieren nicht erlaubt – der Bericht steht markiert im "
        + "Protokollfeld, bitte von Hand kopieren.", "warnung");
    }
  }

  /* ---------- Diagnose: Befehl senden ---------- */

  async function sendCommand() {
    const field = el("diag-befehl");
    const text = field.value.trim().toUpperCase();
    if (!text) return;
    if (!O.linked()) {
      K.report("Kein Dongle verbunden.", "warnung");
      return;
    }
    if (tripRunning() && !el("diag-fahrt-ok").checked) {
      K.report("Eine Fahrt läuft. Zum Senden das Häkchen „Trotz laufender "
        + "Fahrt senden“ setzen – der Befehl stört die Messung.", "warnung");
      return;
    }
    el("diag-senden").disabled = true;
    try {
      await O.cli(text);        // die Antwort steht im Protokoll
    } catch (failure) {
      K.report("Befehl: " + failure.message, "fehler");
    } finally {
      el("diag-senden").disabled = false;
      field.value = "";
      refresh();
    }
  }

  /* ---------- Diagnose: Mithören ---------- */

  let listenText = "";

  function listenFormat(e) {
    const header = `Protokoll ${e.trace_log}, ${e.duration_ms / 1000} s: ${e.total} Frames, `
      + `${e.ids.length} Kennungen`;
    if (!e.total) {
      return header + "\n\nNichts angekommen. Der Bus war still, oder dieser Anschluss "
        + "führt keine Broadcast-Daten (das Gateway filtert)."
        + (e.hints.length ? "\n\nHinweise:\n  " + e.hints.join("\n  ") : "");
    }
    const rows = e.ids.slice(0, 40).map((i) =>
      `${i.id.padEnd(9)} ${String(i.n).padStart(6)}x  ${String(i.perSec).padStart(6)}/s  `
      + `${String(i.variants >= 200 ? "200+" : i.variants).padStart(4)} Werte  ${i.tail}`);
    return [header, "", "Kennung   Anzahl     pro s  Werte   letzte Daten", ...rows,
            e.ids.length > 40 ? `… und ${e.ids.length - 40} weitere` : "",
            e.hints.length ? "\nHinweise:\n  " + e.hints.join("\n  ") : ""]
      .filter((z) => z !== "").join("\n");
  }

  async function startListen() {
    const btn = el("lausch-start");
    const destination = el("lausch-ergebnis");
    // Jede Rückmeldung steht im Ergebnisfeld - es ist das, worauf man schaut.
    destination.hidden = false;
    if (tripRunning()) {
      destination.textContent = "Eine Fahrt läuft – Mithören unterbricht die Messung. "
        + "Erst die Aufzeichnung beenden.";
      return;
    }
    btn.disabled = true;
    try {
      if (!O.linked()) {
        destination.textContent = "verbinde mit dem Dongle …";
        await connectDongle();
      }
      destination.textContent = "höre zu … (" + el("lausch-dauer").value / 1000 + " s)";
      const result = await O.listen({
        trace_log: el("lausch-protokoll").value,
        duration_ms: Number(el("lausch-dauer").value) });
      listenText = listenFormat(result);
      destination.textContent = listenText;
      el("lausch-kopieren").hidden = false;
    } catch (failure) {
      destination.textContent = "Mithören: " + failure.message;
    } finally {
      btn.disabled = false;
      refresh();
    }
  }

  async function copyListen() {
    try {
      await navigator.clipboard.writeText(listenText);
      K.report("Ergebnis kopiert.", "hinweis");
    } catch (failure) {
      K.report("Kopieren ging nicht: " + failure.message, "warnung");
    }
  }

  /* ---------- Speicher ---------- */

  function bufferKey() {
    const hit = [];
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.indexOf("jolt-puffer-") === 0) hit.push(k);
      }
    } catch (failure) { /* kein Speicher */ }
    return hit;
  }

  function showStorage() {
    const z = [];
    let points = 0;
    for (const k of bufferKey()) {
      try { points += (JSON.parse(localStorage.getItem(k)) || []).length; }
      catch (failure) { /* beschädigt */ }
    }
    z.push(row("Wartende Messpunkte", points
      ? `${points} – noch nicht an den Server gegangen` : "keine",
      points ? "warnung" : "gut"));
    el("einst-puffer-leeren").hidden = !points;
    z.push(row("Service Worker", "serviceWorker" in navigator
      ? (K.state.serviceWorker ? "aktiv" : "nicht registriert") : "nicht unterstützt"));
    el("einst-speicher").innerHTML = z.join("");
    if (navigator.storage && navigator.storage.estimate) {
      navigator.storage.estimate().then((e) => {
        if (!e || !e.usage) return;
        el("einst-speicher").insertAdjacentHTML("beforeend",
          row("Belegt", K.num(e.usage / 1048576, 1) + " MB"));
      }).catch(() => {});
    }
  }

  function discardBuffer() {
    if (!window.confirm("Die wartenden Messpunkte unwiderruflich verwerfen? "
        + "Sie sind dann für die Auswertung verloren.")) return;
    for (const k of bufferKey()) {
      try { localStorage.removeItem(k); } catch (failure) { /* egal */ }
    }
    showStorage();
  }

  async function cacheClear() {
    try {
      if (window.caches) {
        for (const name of await caches.keys()) await caches.delete(name);
      }
      if (navigator.serviceWorker) {
        for (const r of await navigator.serviceWorker.getRegistrations()) {
          await r.unregister();
        }
      }
    } catch (failure) {
      K.report("Cache zurücksetzen: " + failure.message, "fehler");
      return;
    }
    // Neu laden mit frischem Gerüst; Anmeldung und Daten liegen nicht im Cache.
    location.reload();
  }

  /* ---------- Aktualisieren ---------- */

  /* Jede Sekunde, solange die Ansicht offen ist - nur das, was sich bewegt. */
  function refresh() {
    const d = O.diagnose();
    showConnection(d);
    showReadings(d);
    showRounds(d);
    showLog();
    el("einst-trennen").disabled = !d.linked;
    el("einst-verbinden").disabled = d.linked;
    el("diag-fahrt-zeile").hidden = !tripRunning();
  }

  /* ---------- CarPlay-Darstellung ---------- */

  /* Die Kacheln beider Stile mit Probewerten - dieselben Zeichenfunktionen
   * wie im Auto, nur ohne den Umweg über CarPlay. */
  function showCarplay() {
    const tiles = window.joltTiles;
    const display = window.joltDisplay;
    if (!tiles || !display) return;
    const choice = el("einst-carplay-stil");
    if (!choice.options.length) {
      for (const look of tiles.STYLES) {
        const o = document.createElement("option");
        o.value = look.id; o.textContent = look.name;
        choice.appendChild(o);
      }
    }
    choice.value = display.look();
    const names = { classic: "klassisch", a: "A – Instrument", b: "B – Telemetrie" };
    el("einst-carplay-kurz").textContent = names[display.look()] || display.look();

    const probe = tiles.probe();
    const vals = tiles.records(probe.m, probe.series_list);
    for (const look of ["a", "b"]) {
      const container = el("vorschau-" + look);
      if (!container || container.childElementCount) continue;     // einmal genügt
      for (const slot of tiles.SLOTS) {
        const canvas = tiles.browserCanvas(tiles.PAGE * 2);
        const c = canvas.getContext("2d");
        c.scale(2, 2);
        tiles.draw(look, slot, vals[slot] || null, c);
        canvas.title = slot;
        container.appendChild(canvas);
      }
    }
  }

  function carplayStyleChoose() {
    const look = el("einst-carplay-stil").value;
    if (window.joltDisplay && window.joltDisplay.setStyle(look)) {
      K.report("CarPlay-Darstellung: " + el("einst-carplay-stil").selectedOptions[0].textContent
        + ". Gilt ab der nächsten Meldung im Auto.", "hinweis");
      showCarplay();
    }
  }

  function open_it() {
    showCarplay();
    showServer();
    showPush();
    showDongleSettings();
    showStorage();
    refresh();
    if (!clock) clock = setInterval(refresh, 1000);
  }

  function close() {
    if (clock) { clearInterval(clock); clock = null; }
  }

  /* Von app.js gerufen, wenn die Ansicht gewechselt wird. */
  function show(visible) {
    if (visible) open_it(); else close();
  }

  function set_up() {
    K.at("einst-carplay-stil", "change", carplayStyleChoose);
    K.at("einst-abmelden", "click", sign_out);
    K.at("einst-push-an", "click", activatePush);
    K.at("einst-push-aus", "click", signOutPush);
    K.at("einst-push-probe", "click", pushProbe);
    K.at("einst-auto", "change", autoAdopt);
    K.at("einst-verbinden", "click", connectButton);
    K.at("einst-trennen", "click", detach);
    K.at("einst-vergessen", "click", forget);
    K.at("diag-auffaellig", "change", showLog);
    K.at("diag-kopieren", "click", copyWriteup);
    K.at("diag-protokoll-leeren", "click", () => { O.logClear(); showLog(); });
    K.at("diag-zuruecksetzen", "click", () => { O.resetCounter(); refresh(); });
    K.at("diag-senden", "click", sendCommand);
    K.at("diag-befehl", "keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); sendCommand(); }
    });
    K.at("lausch-start", "click", startListen);
    K.at("lausch-kopieren", "click", copyListen);
    K.at("einst-puffer-leeren", "click", discardBuffer);
    K.at("einst-cache-leeren", "click", cacheClear);
  }

  return { set_up, show, writeup };
})();
