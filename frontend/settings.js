/* Settings and dongle diagnostics.
 *
 * Things you rarely need but still want to find: account, notifications,
 * dongle, storage - and the diagnostics of the OBD2 dongle. The diagnostics
 * live here because you look at them when something is wrong ("why is the
 * current missing?"), not while driving.
 *
 * Everything in the dongle diagnostics comes from `joltObd.diagnose()` and
 * `joltObd.trace_log()`. The module keeps its own books; this file only shows
 * and touches nothing that changes the measurement - with one exception, the
 * command console, which warns when a trip is in progress.
 *
 * Updates happen only while the view is open: a table rebuilt every second in
 * the background drains the battery in the car for nothing.
 */
window.joltSettings = (function () {
  "use strict";

  const K = window.jolt;
  const O = window.joltObd;
  const el = (id) => document.getElementById(id);

  let clock = null;
  let status = null;          // response from /api/status, once per opening

  /* ---------- Helpers ---------- */

  /* Device names and texts from the log come from outside. */
  function esc(text) {
    const helper = document.createElement("div");
    helper.textContent = text === null || text === undefined ? "" : String(text);
    return helper.innerHTML;
  }

  /* "vor 12 s", "vor 3 min" - for the age of a value. */
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

  function row(name, value, kind) {
    return `<dt>${esc(name)}</dt><dd${kind ? ` class="${kind}"` : ""}>${value}</dd>`;
  }

  function time_of_day(ms) {
    const d = new Date(ms);
    const two = (n) => String(n).padStart(2, "0");
    return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
  }

  function tripRunning() {
    return !!K.state.sessionId;
  }

  /* ---------- Account and server ---------- */

  async function showServer() {
    const as_of = el("status");
    const rows = [];
    rows.push(row("Code-Stand", esc(as_of ? as_of.textContent : "–")));
    try {
      status = await K.api("/api/status");
      rows.push(row("Version", esc("v" + status.version + " · Build " + status.build)));
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
    el("settings-server").innerHTML = rows.join("");
    el("settings-sign-out").hidden = !K.token();
  }

  async function sign_out() {
    try { await K.api("/api/logout", { method: "POST" }); }
    catch (failure) { /* token is gone anyway or the server is unreachable */ }
    K.setToken("");
    // Reload: startup checks the sign-in and then shows the password field.
    location.reload();
  }

  /* ---------- Notifications ---------- */

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
      serverReady = (await K.api("/api/push/key")).configured;
    } catch (failure) { /* shown below as "nicht erreichbar" */ }
    rows.push(row("Server", serverReady ? "bereit"
      : "ohne VAPID-Schlüssel – Benachrichtigungen aus", serverReady ? "gut" : "warnung"));
    if (supported) {
      const grant = Notification.permission;
      rows.push(row("Erlaubnis",
        { granted: "erteilt", denied: "abgelehnt (in den Browser-Einstellungen ändern)",
          default: "noch nicht gefragt" }[grant] || grant,
        grant === "granted" ? "gut" : (grant === "denied" ? "schlecht" : "")));
      let signed_in = false;
      try { signed_in = !!(await subscription()); } catch (failure) { /* unknown */ }
      rows.push(row("Abo", signed_in ? "angemeldet" : "keines",
        signed_in ? "gut" : ""));
      el("settings-push-off").hidden = !signed_in;
    }
    el("settings-push").innerHTML = rows.join("");
    el("settings-push-on").disabled = !supported || !serverReady;
    el("settings-push-probe").disabled = !serverReady;
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
        await K.api("/api/push/subscription", { method: "DELETE", body: { endpoint } });
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

  /* ---------- Dongle settings ---------- */

  /* Same setting as the checkbox in the live view: its change handler
   * sets state and storage, so it is triggered instead of keeping the logic
   * here a second time. */
  function autoAdopt() {
    const live = el("dongle-car");
    if (!live) return;
    live.checked = el("settings-car").checked;
    live.dispatchEvent(new Event("change"));
  }

  function showDongleSettings() {
    const live = el("dongle-car");
    if (live) el("settings-car").checked = live.checked;
    el("settings-disconnect").disabled = !O.linked();
  }

  /* Connect the dongle - first without a dialog (known device), then with one.
   *
   * After parking it is disconnected (jolt asks nothing while stationary so
   * the alarm system stays quiet), and here there was no way to reconnect it
   * - the settings pointed to other views. Anyone who wanted to listen in or
   * send a command from here faced "Kein Dongle verbunden". It only
   * connects; nothing is read. */
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
    const btn = el("settings-connect");
    const as_of = el("settings-connect-status");
    btn.disabled = true;
    as_of.textContent = "verbinde …";
    try {
      await connectDongle();
      as_of.textContent = "Verbunden: " + (O.diagnose().connection.device || "Dongle")
        + ". Es wird nichts gelesen, solange keine Fahrt läuft.";
    } catch (failure) {
      // In the text and not as a message: a message disappears after six
      // seconds, and anyone who has not seen it does not know why nothing happened.
      as_of.textContent = "Nicht verbunden: " + failure.message;
    } finally {
      btn.disabled = false;
      refresh();
    }
  }

  function detach() {
    // The live view keeps its own state (pause, auto mode); going through
    // its button is clean, directly on the module it would stay unaware.
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

  /* ---------- Diagnostics: connection ---------- */

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
    el("diag-connection").innerHTML = z.join("");

    const short = !d.linked ? "getrennt"
      : (d.rounds.n ? `${d.rounds.n} Runden` : "verbunden");
    el("settings-diag-short").textContent = short;
  }

  /* ---------- Diagnostics: readings ---------- */

  /* One row per measured quantity of the table (readings.js), with the
   * values that come along in the same response. The module keeps counters
   * per main value; companions only show their last value. */
  function showReadings(d) {
    const record = d.lastRecord;
    const header = "<tr><th>Messgrösse</th><th>Wert</th><th>Alter</th>"
      + "<th>ok</th><th>leer</th><th>aus</th><th>Dauer</th></tr>";
    const rows = [];
    for (const field of O.FIELDS) {
      const count = d.readings[field.name];
      const value = record && record.vals[field.name] !== undefined
        ? record.vals[field.name] : (count ? count.val : null);
      const unit = field.unit ? " " + field.unit : "";
      const text = value === null || value === undefined ? "–"
        : K.num(value, field.put) + unit;
      const as_of = count ? count.timestamp : (record && value !== null && value !== undefined ? record.timestamp : null);
      const required = field.required ? " <small>(Pflicht)</small>" : "";
      if (!count) {
        // Companion or never read.
        rows.push(`<tr><td>${esc(field.title)}${required}</td><td class="still">${text}</td>`
          + `<td class="still">${age(as_of)}</td>`
          + `<td class="still">·</td><td class="still">·</td><td class="still">·</td>`
          + `<td class="still">·</td></tr>`);
        continue;
      }
      const total = count.ok + count.empty + count.failure;
      const failures = count.failure;
      rows.push(`<tr><td>${esc(field.title)}${required}</td><td>${text}</td>`
        + `<td>${age(as_of)}</td>`
        + `<td>${count.ok}</td>`
        + `<td class="${count.empty ? "warnung" : "still"}">${count.empty}</td>`
        + `<td class="${failures && failures * 4 > total ? "schlecht" : (failures ? "warnung" : "still")}">${failures}</td>`
        + `<td>${duration(total ? count.sumMs / total : null)}</td></tr>`);
    }
    el("diag-readings").innerHTML = header + rows.join("");
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
    el("diag-rounds").innerHTML = z.join("");
  }

  /* ---------- Diagnostics: log ---------- */

  const CONSPICUOUS = /FEHLER|Zeitüberschreitung|keine Antwort|verspätet|fehlgeschlagen|NO DATA|ERROR|UNABLE|BUS/i;

  function showLog() {
    const container = el("diag-log");
    const rows = O.trace_log(el("diag-flagged").checked).slice(-250);
    // Only jump to the end if you were already at the bottom - someone who
    // scrolled up to read something should not be yanked back every second.
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

  /* ---------- Diagnostics: report ---------- */

  function writeup() {
    const d = O.diagnose();
    const as_of = el("status");
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
      // Without clipboard permission (insecure context, older WebViews):
      // write into the log field, selected, for copying by hand.
      const field = el("diag-log");
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

  /* ---------- Diagnostics: send command ---------- */

  async function sendCommand() {
    const field = el("diag-command");
    const text = field.value.trim().toUpperCase();
    if (!text) return;
    if (!O.linked()) {
      K.report("Kein Dongle verbunden.", "warnung");
      return;
    }
    if (tripRunning() && !el("diag-trip-ok").checked) {
      K.report("Eine Fahrt läuft. Zum Senden das Häkchen „Trotz laufender "
        + "Fahrt senden“ setzen – der Befehl stört die Messung.", "warnung");
      return;
    }
    el("diag-send").disabled = true;
    try {
      await O.cli(text);        // the response appears in the log
    } catch (failure) {
      K.report("Befehl: " + failure.message, "fehler");
    } finally {
      el("diag-send").disabled = false;
      field.value = "";
      refresh();
    }
  }

  /* ---------- Diagnostics: listen in ---------- */

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
    const btn = el("listen-start");
    const destination = el("listen-result");
    // Every feedback appears in the result field - it is what you look at.
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
      destination.textContent = "höre zu … (" + el("listen-duration").value / 1000 + " s)";
      const result = await O.listen({
        trace_log: el("listen-log").value,
        duration_ms: Number(el("listen-duration").value) });
      listenText = listenFormat(result);
      destination.textContent = listenText;
      el("listen-copy").hidden = false;
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

  /* ---------- Storage ---------- */

  function bufferKey() {
    const hit = [];
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.indexOf("jolt-puffer-") === 0) hit.push(k);
      }
    } catch (failure) { /* no storage */ }
    return hit;
  }

  function showStorage() {
    const z = [];
    let points = 0;
    for (const k of bufferKey()) {
      try { points += (JSON.parse(localStorage.getItem(k)) || []).length; }
      catch (failure) { /* corrupted */ }
    }
    z.push(row("Wartende Messpunkte", points
      ? `${points} – noch nicht an den Server gegangen` : "keine",
      points ? "warnung" : "gut"));
    el("settings-buffer-clear").hidden = !points;
    z.push(row("Service Worker", "serviceWorker" in navigator
      ? (K.state.serviceWorker ? "aktiv" : "nicht registriert") : "nicht unterstützt"));
    el("settings-storage").innerHTML = z.join("");
    if (navigator.storage && navigator.storage.estimate) {
      navigator.storage.estimate().then((e) => {
        if (!e || !e.usage) return;
        el("settings-storage").insertAdjacentHTML("beforeend",
          row("Belegt", K.num(e.usage / 1048576, 1) + " MB"));
      }).catch(() => {});
    }
  }

  function discardBuffer() {
    if (!window.confirm("Die wartenden Messpunkte unwiderruflich verwerfen? "
        + "Sie sind dann für die Auswertung verloren.")) return;
    for (const k of bufferKey()) {
      try { localStorage.removeItem(k); } catch (failure) { /* never mind */ }
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
    // Reload with a fresh shell; sign-in and data are not in the cache.
    location.reload();
  }

  /* ---------- Refresh ---------- */

  /* Every second while the view is open - only what moves. */
  function refresh() {
    const d = O.diagnose();
    showConnection(d);
    showReadings(d);
    showRounds(d);
    showLog();
    el("settings-disconnect").disabled = !d.linked;
    el("settings-connect").disabled = d.linked;
    el("diag-trip-row").hidden = !tripRunning();
  }

  /* ---------- CarPlay display ---------- */

  /* The tiles of both styles with sample values - the same drawing functions
   * as in the car, just without the detour via CarPlay. */
  function showCarplay() {
    const tiles = window.joltTiles;
    const display = window.joltDisplay;
    if (!tiles || !display) return;
    const choice = el("settings-carplay-style");
    if (!choice.options.length) {
      for (const look of tiles.STYLES) {
        const o = document.createElement("option");
        o.value = look.id; o.textContent = look.name;
        choice.appendChild(o);
      }
    }
    choice.value = display.look();
    const names = { classic: "klassisch", a: "A – Instrument", b: "B – Telemetrie" };
    el("settings-carplay-short").textContent = names[display.look()] || display.look();

    const probe = tiles.probe();
    const vals = tiles.records(probe.m, probe.series_list);
    for (const look of ["a", "b"]) {
      const container = el("preview-" + look);
      if (!container || container.childElementCount) continue;     // once is enough
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
    const look = el("settings-carplay-style").value;
    if (window.joltDisplay && window.joltDisplay.setStyle(look)) {
      K.report("CarPlay-Darstellung: " + el("settings-carplay-style").selectedOptions[0].textContent
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

  /* Called by app.js when the view is switched. */
  function show(visible) {
    if (visible) open_it(); else close();
  }

  function set_up() {
    K.at("settings-carplay-style", "change", carplayStyleChoose);
    K.at("settings-sign-out", "click", sign_out);
    K.at("settings-push-on", "click", activatePush);
    K.at("settings-push-off", "click", signOutPush);
    K.at("settings-push-probe", "click", pushProbe);
    K.at("settings-car", "change", autoAdopt);
    K.at("settings-connect", "click", connectButton);
    K.at("settings-disconnect", "click", detach);
    K.at("settings-forget", "click", forget);
    K.at("diag-flagged", "change", showLog);
    K.at("diag-copy", "click", copyWriteup);
    K.at("diag-log-clear", "click", () => { O.logClear(); showLog(); });
    K.at("diag-reset", "click", () => { O.resetCounter(); refresh(); });
    K.at("diag-send", "click", sendCommand);
    K.at("diag-command", "keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); sendCommand(); }
    });
    K.at("listen-start", "click", startListen);
    K.at("listen-copy", "click", copyListen);
    K.at("settings-buffer-clear", "click", discardBuffer);
    K.at("settings-cache-clear", "click", cacheClear);
  }

  return { set_up, show, writeup };
})();
