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
window.joltEinstellungen = (function () {
  "use strict";

  const K = window.jolt;
  const O = window.joltObd;
  const el = (id) => document.getElementById(id);

  let uhr = null;
  let status = null;          // Antwort von /api/status, einmal je Öffnen

  /* ---------- Hilfen ---------- */

  /* Namen von Geräten und Texte aus dem Protokoll stammen von aussen. */
  function esc(text) {
    const hilfe = document.createElement("div");
    hilfe.textContent = text === null || text === undefined ? "" : String(text);
    return hilfe.innerHTML;
  }

  /* "vor 12 s", "vor 3 min" - für das Alter eines Werts. */
  function alter(zeit) {
    if (!zeit) return "–";
    const s = Math.max(0, Math.round((Date.now() - zeit) / 1000));
    if (s < 90) return `vor ${s} s`;
    if (s < 5400) return `vor ${Math.round(s / 60)} min`;
    return `vor ${(s / 3600).toFixed(1).replace(".", ",")} h`;
  }

  function dauer(ms) {
    return ms === null || ms === undefined ? "–"
      : (ms >= 1000 ? K.zahl(ms / 1000, 1) + " s" : Math.round(ms) + " ms");
  }

  function zeile(name, wert, art) {
    return `<dt>${esc(name)}</dt><dd${art ? ` class="${art}"` : ""}>${wert}</dd>`;
  }

  function uhrzeit(ms) {
    const d = new Date(ms);
    const zwei = (n) => String(n).padStart(2, "0");
    return `${zwei(d.getHours())}:${zwei(d.getMinutes())}:${zwei(d.getSeconds())}`;
  }

  function fahrtLaeuft() {
    return !!K.zustand.sitzungId;
  }

  /* ---------- Konto und Server ---------- */

  async function serverZeigen() {
    const stand = el("stand");
    const zeilen = [];
    zeilen.push(zeile("Code-Stand", esc(stand ? stand.textContent : "–")));
    try {
      status = await K.api("/api/status");
      zeilen.push(zeile("Routing",
        status.demo_routing ? "Demo – erfundene Routen" : "echt (OpenRouteService)",
        status.demo_routing ? "warnung" : "gut"));
      zeilen.push(zeile("Passwortschutz",
        status.passwort_noetig ? "an" : "aus – jeder im Netz darf herein",
        status.passwort_noetig ? "gut" : "warnung"));
    } catch (fehler) {
      zeilen.push(zeile("Server", "nicht erreichbar", "schlecht"));
    }
    zeilen.push(zeile("Angemeldet", K.token() ? "ja, auf diesem Gerät" : "nein"));
    zeilen.push(zeile("Oberfläche", esc(location.origin)));
    el("einst-server").innerHTML = zeilen.join("");
    el("einst-abmelden").hidden = !K.token();
  }

  async function abmelden() {
    try { await K.api("/api/logout", { method: "POST" }); }
    catch (fehler) { /* Token ist ohnehin weg oder der Server nicht erreichbar */ }
    K.tokenSetzen("");
    // Neu laden: Der Start prüft die Anmeldung und zeigt dann das Passwortfeld.
    location.reload();
  }

  /* ---------- Benachrichtigungen ---------- */

  async function abo() {
    const registrierung = K.zustand.serviceWorker
      || (navigator.serviceWorker && await navigator.serviceWorker.ready.catch(() => null));
    if (!registrierung || !registrierung.pushManager) return null;
    return registrierung.pushManager.getSubscription();
  }

  async function pushZeigen() {
    const zeilen = [];
    const unterstuetzt = "Notification" in window && "PushManager" in window;
    zeilen.push(zeile("Dieses Gerät",
      unterstuetzt ? "kann Benachrichtigungen"
        : "kann keine Benachrichtigungen (in Safari nur als Home-Bildschirm-App)",
      unterstuetzt ? "gut" : "warnung"));
    let serverBereit = false;
    try {
      serverBereit = (await K.api("/api/push/schluessel")).eingerichtet;
    } catch (fehler) { /* unten als "nicht erreichbar" */ }
    zeilen.push(zeile("Server", serverBereit ? "bereit"
      : "ohne VAPID-Schlüssel – Benachrichtigungen aus", serverBereit ? "gut" : "warnung"));
    if (unterstuetzt) {
      const erlaubnis = Notification.permission;
      zeilen.push(zeile("Erlaubnis",
        { granted: "erteilt", denied: "abgelehnt (in den Browser-Einstellungen ändern)",
          default: "noch nicht gefragt" }[erlaubnis] || erlaubnis,
        erlaubnis === "granted" ? "gut" : (erlaubnis === "denied" ? "schlecht" : "")));
      let angemeldet = false;
      try { angemeldet = !!(await abo()); } catch (fehler) { /* unbekannt */ }
      zeilen.push(zeile("Abo", angemeldet ? "angemeldet" : "keines",
        angemeldet ? "gut" : ""));
      el("einst-push-aus").hidden = !angemeldet;
    }
    el("einst-push").innerHTML = zeilen.join("");
    el("einst-push-an").disabled = !unterstuetzt || !serverBereit;
    el("einst-push-probe").disabled = !serverBereit;
  }

  async function pushAktivieren() {
    if (!window.joltLive || !window.joltLive.benachrichtigungenEinrichten) return;
    await window.joltLive.benachrichtigungenEinrichten();
    await pushZeigen();
  }

  async function pushAbmelden() {
    try {
      const aktuell = await abo();
      if (aktuell) {
        const endpoint = aktuell.endpoint;
        await aktuell.unsubscribe();
        await K.api("/api/push/abo", { method: "DELETE", body: { endpoint } });
      }
      K.melden("Dieses Gerät erhält keine Benachrichtigungen mehr.", "hinweis");
    } catch (fehler) {
      K.melden("Abmelden: " + fehler.message, "fehler");
    }
    await pushZeigen();
  }

  async function pushProbe() {
    try {
      const antwort = await K.api("/api/push/probe", { method: "POST" });
      K.melden("Testnachricht verschickt: " + JSON.stringify(antwort), "hinweis");
    } catch (fehler) {
      K.melden("Testnachricht: " + fehler.message, "fehler");
    }
  }

  /* ---------- Dongle-Einstellungen ---------- */

  /* Dieselbe Einstellung wie das Häkchen in der Live-Ansicht: Dessen
   * Änderungs-Handler setzt Zustand und Speicher, also wird er ausgelöst,
   * statt die Logik hier ein zweites Mal zu führen. */
  function autoUebernehmen() {
    const live = el("dongle-auto");
    if (!live) return;
    live.checked = el("einst-auto").checked;
    live.dispatchEvent(new Event("change"));
  }

  function dongleEinstellungenZeigen() {
    const live = el("dongle-auto");
    if (live) el("einst-auto").checked = live.checked;
    el("einst-trennen").disabled = !O.verbunden();
  }

  function trennen() {
    // Die Live-Ansicht hält ihren eigenen Zustand (Pause, Auto-Modus); über
    // ihren Knopf läuft das sauber, direkt am Modul bliebe sie im Glauben.
    const pause = el("dongle-pause");
    if (pause && !pause.hidden) { pause.click(); return; }
    O.trennen();
  }

  function vergessen() {
    if (!window.confirm("Das gemerkte Gerät vergessen? Beim nächsten Verbinden "
        + "fragt jolt wieder, welcher Dongle es sein soll.")) return;
    O.vergessen();
    K.melden("Gerät vergessen.", "hinweis");
  }

  /* ---------- Diagnose: Verbindung ---------- */

  function verbindungZeigen(d) {
    const v = d.verbindung;
    const z = [];
    z.push(zeile("Zugang", { nativ: "nativ (iOS-App, CoreBluetooth)",
      web: "Web Bluetooth (Browser)", keiner: "keiner – dieser Browser kann es nicht" }[d.transport],
      d.transport === "keiner" ? "schlecht" : "gut"));
    z.push(zeile("Zustand", d.verbunden ? "verbunden" : "getrennt",
      d.verbunden ? "gut" : "warnung"));
    z.push(zeile("Gerät", esc(v.geraet || d.gemerktesGeraet || "–")));
    z.push(zeile("Verbunden seit", v.seit ? `${alter(v.seit)} (${uhrzeit(v.seit)})` : "–"));
    z.push(zeile("Letzte Antwort", alter(d.befehle.letzterEmpfang),
      d.verbunden && d.befehle.letzterEmpfang
        && Date.now() - d.befehle.letzterEmpfang > 120000 ? "warnung" : ""));
    z.push(zeile("Abrisse",
      `${v.abrisse}` + (v.wiederversuche ? `, ${v.wiederversuche} Wiederverbindungs­versuche` : ""),
      v.abrisse > 2 ? "warnung" : ""));
    if (d.wechselGescheitert.length) {
      z.push(zeile("Protokollwechsel gescheitert",
        esc(d.wechselGescheitert.join(", ")) + " – in dieser Sitzung nicht mehr versucht",
        "schlecht"));
    }
    if (d.tabellenFehler.length) {
      z.push(zeile("Fehler in messwerte.js", esc(d.tabellenFehler.join(" · ")), "schlecht"));
    }
    el("diag-verbindung").innerHTML = z.join("");

    const kurz = !d.verbunden ? "getrennt"
      : (d.runden.n ? `${d.runden.n} Runden` : "verbunden");
    el("einst-diag-kurz").textContent = kurz;
  }

  /* ---------- Diagnose: Messwerte ---------- */

  /* Eine Zeile je Messgrösse der Tabelle (messwerte.js), samt der Werte, die
   * aus derselben Antwort mitkommen. Die Zähler führt das Modul je
   * Hauptwert; Mitläufer zeigen nur ihren letzten Wert. */
  function messwerteZeigen(d) {
    const satz = d.letzterSatz;
    const kopf = "<tr><th>Messgrösse</th><th>Wert</th><th>Alter</th>"
      + "<th>ok</th><th>leer</th><th>aus</th><th>Dauer</th></tr>";
    const zeilen = [];
    for (const feld of O.FELDER) {
      const zaehl = d.messwerte[feld.name];
      const wert = satz && satz.werte[feld.name] !== undefined
        ? satz.werte[feld.name] : (zaehl ? zaehl.wert : null);
      const einheit = feld.einheit ? " " + feld.einheit : "";
      const text = wert === null || wert === undefined ? "–"
        : K.zahl(wert, feld.stellen) + einheit;
      const stand = zaehl ? zaehl.zeit : (satz && wert !== null && wert !== undefined ? satz.zeit : null);
      const pflicht = feld.pflicht ? " <small>(Pflicht)</small>" : "";
      if (!zaehl) {
        // Mitläufer oder noch nie gelesen.
        zeilen.push(`<tr><td>${esc(feld.titel)}${pflicht}</td><td class="still">${text}</td>`
          + `<td class="still">${alter(stand)}</td>`
          + `<td class="still">·</td><td class="still">·</td><td class="still">·</td>`
          + `<td class="still">·</td></tr>`);
        continue;
      }
      const gesamt = zaehl.ok + zaehl.leer + zaehl.fehler;
      const aus = zaehl.fehler;
      zeilen.push(`<tr><td>${esc(feld.titel)}${pflicht}</td><td>${text}</td>`
        + `<td>${alter(stand)}</td>`
        + `<td>${zaehl.ok}</td>`
        + `<td class="${zaehl.leer ? "warnung" : "still"}">${zaehl.leer}</td>`
        + `<td class="${aus && aus * 4 > gesamt ? "schlecht" : (aus ? "warnung" : "still")}">${aus}</td>`
        + `<td>${dauer(gesamt ? zaehl.summeMs / gesamt : null)}</td></tr>`);
    }
    el("diag-messwerte").innerHTML = kopf + zeilen.join("");
  }

  function rundenZeigen(d) {
    const r = d.runden;
    const b = d.befehle;
    const z = [];
    z.push(zeile("Runden", r.n ? `${r.n}, davon ${r.fehler} gescheitert` : "noch keine",
      r.fehler && r.fehler * 4 > r.n ? "schlecht" : ""));
    z.push(zeile("Dauer je Runde", r.n
      ? `Ø ${dauer(r.summeMs / r.n)}, zuletzt ${dauer(r.letzteMs)}` : "–"));
    z.push(zeile("Letzte Runde", alter(r.zeit)));
    z.push(zeile("Befehle", `${b.gesendet} gesendet, ${b.beantwortet} beantwortet`));
    z.push(zeile("Antwortzeit", b.beantwortet
      ? `Ø ${dauer(b.summeMs / b.beantwortet)}, zuletzt ${dauer(b.letzteMs)}` : "–"));
    z.push(zeile("Ohne Antwort", `${b.zeitablauf}`
      + (b.verspaetet ? `, ${b.verspaetet} verspätet eingetroffen` : ""),
      b.zeitablauf ? "warnung" : ""));
    if (d.befehlLaeuft) z.push(zeile("Gerade", "ein Befehl wartet auf Antwort"));
    el("diag-runden").innerHTML = z.join("");
  }

  /* ---------- Diagnose: Protokoll ---------- */

  const AUFFAELLIG = /FEHLER|Zeitüberschreitung|keine Antwort|verspätet|fehlgeschlagen|NO DATA|ERROR|UNABLE|BUS/i;

  function protokollZeigen() {
    const behaelter = el("diag-protokoll");
    const zeilen = O.protokoll(el("diag-auffaellig").checked).slice(-250);
    // Nur ans Ende springen, wenn man schon unten war - wer hochgescrollt hat,
    // um etwas zu lesen, soll nicht jede Sekunde zurückgerissen werden.
    const unten = behaelter.scrollHeight - behaelter.scrollTop - behaelter.clientHeight < 30;
    behaelter.innerHTML = zeilen.map((z) => {
      const klasse = AUFFAELLIG.test(z.text) ? "auff" : (z.art === "rein" ? "rein"
        : (z.art === "raus" ? "raus" : ""));
      const pfeil = z.art === "raus" ? "→ " : (z.art === "rein" ? "← " : "  ");
      return `<span class="zeit">${uhrzeit(z.zeit)}</span> `
        + `<span class="${klasse}">${pfeil}${esc(z.text)}</span>`;
    }).join("\n") || "(noch nichts protokolliert – erst verbinden)";
    if (unten) behaelter.scrollTop = behaelter.scrollHeight;
  }

  /* ---------- Diagnose: Bericht ---------- */

  function bericht() {
    const d = O.diagnose();
    const stand = el("stand");
    const kopf = [
      "jolt-Diagnosebericht",
      "Stand: " + (stand ? stand.textContent : "?"),
      "Zeit: " + new Date().toISOString(),
      "Gerät: " + navigator.userAgent,
      "Zugang: " + d.transport + ", " + (d.verbunden ? "verbunden" : "getrennt"),
      "Fahrt läuft: " + (fahrtLaeuft() ? "ja" : "nein"),
    ];
    const messwerte = O.FELDER.map((f) => {
      const z = d.messwerte[f.name];
      if (!z) return `  ${f.name}: nicht gelesen`;
      return `  ${f.name}: ok ${z.ok}, leer ${z.leer}, aus ${z.fehler}, `
        + `Ø ${z.ok + z.leer + z.fehler ? Math.round(z.summeMs / (z.ok + z.leer + z.fehler)) : "–"} ms, `
        + `letzter Wert ${z.wert}`;
    });
    const log = O.protokoll(false).slice(-150)
      .map((z) => `${uhrzeit(z.zeit)} ${z.art === "raus" ? "->" : (z.art === "rein" ? "<-" : "  ")} ${z.text}`);
    return [...kopf, "", "Verbindung / Zähler:", JSON.stringify({
      verbindung: d.verbindung, befehle: d.befehle, runden: d.runden,
      wechselGescheitert: d.wechselGescheitert, tabellenFehler: d.tabellenFehler,
      letzteAdresse: d.letzteAdresse }, null, 1),
      "", "Messwerte:", ...messwerte, "", "Protokoll (letzte 150 Zeilen):", ...log].join("\n");
  }

  async function berichtKopieren() {
    const text = bericht();
    try {
      await navigator.clipboard.writeText(text);
      K.melden("Bericht in die Zwischenablage kopiert.", "hinweis");
    } catch (fehler) {
      // Ohne Zwischenablage-Erlaubnis (unsicherer Kontext, ältere WebViews):
      // in das Protokollfeld schreiben, markiert, zum Händisch-Kopieren.
      const feld = el("diag-protokoll");
      feld.textContent = text;
      const auswahl = window.getSelection();
      const bereich = document.createRange();
      bereich.selectNodeContents(feld);
      auswahl.removeAllRanges();
      auswahl.addRange(bereich);
      K.melden("Kopieren nicht erlaubt – der Bericht steht markiert im "
        + "Protokollfeld, bitte von Hand kopieren.", "warnung");
    }
  }

  /* ---------- Diagnose: Befehl senden ---------- */

  async function befehlSenden() {
    const feld = el("diag-befehl");
    const text = feld.value.trim().toUpperCase();
    if (!text) return;
    if (!O.verbunden()) {
      K.melden("Kein Dongle verbunden.", "warnung");
      return;
    }
    if (fahrtLaeuft() && !el("diag-fahrt-ok").checked) {
      K.melden("Eine Fahrt läuft. Zum Senden das Häkchen „Trotz laufender "
        + "Fahrt senden“ setzen – der Befehl stört die Messung.", "warnung");
      return;
    }
    el("diag-senden").disabled = true;
    try {
      await O.konsole(text);        // die Antwort steht im Protokoll
    } catch (fehler) {
      K.melden("Befehl: " + fehler.message, "fehler");
    } finally {
      el("diag-senden").disabled = false;
      feld.value = "";
      aktualisieren();
    }
  }

  /* ---------- Diagnose: Mithören ---------- */

  let lauschText = "";

  function lauschenFormat(e) {
    const kopf = `Protokoll ${e.protokoll}, ${e.dauer_ms / 1000} s: ${e.gesamt} Frames, `
      + `${e.ids.length} Kennungen`;
    if (!e.gesamt) {
      return kopf + "\n\nNichts angekommen. Der Bus war still, oder dieser Anschluss "
        + "führt keine Broadcast-Daten (das Gateway filtert)."
        + (e.hinweise.length ? "\n\nHinweise:\n  " + e.hinweise.join("\n  ") : "");
    }
    const zeilen = e.ids.slice(0, 40).map((i) =>
      `${i.id.padEnd(9)} ${String(i.n).padStart(6)}x  ${String(i.proSek).padStart(6)}/s  `
      + `${String(i.varianten >= 200 ? "200+" : i.varianten).padStart(4)} Werte  ${i.letzte}`);
    return [kopf, "", "Kennung   Anzahl     pro s  Werte   letzte Daten", ...zeilen,
            e.ids.length > 40 ? `… und ${e.ids.length - 40} weitere` : "",
            e.hinweise.length ? "\nHinweise:\n  " + e.hinweise.join("\n  ") : ""]
      .filter((z) => z !== "").join("\n");
  }

  async function lauschenStarten() {
    if (!O.verbunden()) {
      K.melden("Kein Dongle verbunden.", "warnung");
      return;
    }
    if (fahrtLaeuft()) {
      K.melden("Eine Fahrt läuft – Mithören unterbricht die Messung. Erst "
        + "beenden.", "warnung");
      return;
    }
    const knopf = el("lausch-start");
    const ziel = el("lausch-ergebnis");
    knopf.disabled = true;
    ziel.hidden = false;
    ziel.textContent = "höre zu …";
    try {
      const ergebnis = await O.lauschen({
        protokoll: el("lausch-protokoll").value,
        dauer_ms: Number(el("lausch-dauer").value) });
      lauschText = lauschenFormat(ergebnis);
      ziel.textContent = lauschText;
      el("lausch-kopieren").hidden = false;
    } catch (fehler) {
      ziel.textContent = "Mithören: " + fehler.message;
    } finally {
      knopf.disabled = false;
      aktualisieren();
    }
  }

  async function lauschenKopieren() {
    try {
      await navigator.clipboard.writeText(lauschText);
      K.melden("Ergebnis kopiert.", "hinweis");
    } catch (fehler) {
      K.melden("Kopieren ging nicht: " + fehler.message, "warnung");
    }
  }

  /* ---------- Speicher ---------- */

  function pufferSchluessel() {
    const treffer = [];
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.indexOf("jolt-puffer-") === 0) treffer.push(k);
      }
    } catch (fehler) { /* kein Speicher */ }
    return treffer;
  }

  function speicherZeigen() {
    const z = [];
    let punkte = 0;
    for (const k of pufferSchluessel()) {
      try { punkte += (JSON.parse(localStorage.getItem(k)) || []).length; }
      catch (fehler) { /* beschädigt */ }
    }
    z.push(zeile("Wartende Messpunkte", punkte
      ? `${punkte} – noch nicht an den Server gegangen` : "keine",
      punkte ? "warnung" : "gut"));
    el("einst-puffer-leeren").hidden = !punkte;
    z.push(zeile("Service Worker", "serviceWorker" in navigator
      ? (K.zustand.serviceWorker ? "aktiv" : "nicht registriert") : "nicht unterstützt"));
    el("einst-speicher").innerHTML = z.join("");
    if (navigator.storage && navigator.storage.estimate) {
      navigator.storage.estimate().then((e) => {
        if (!e || !e.usage) return;
        el("einst-speicher").insertAdjacentHTML("beforeend",
          zeile("Belegt", K.zahl(e.usage / 1048576, 1) + " MB"));
      }).catch(() => {});
    }
  }

  function pufferVerwerfen() {
    if (!window.confirm("Die wartenden Messpunkte unwiderruflich verwerfen? "
        + "Sie sind dann für die Auswertung verloren.")) return;
    for (const k of pufferSchluessel()) {
      try { localStorage.removeItem(k); } catch (fehler) { /* egal */ }
    }
    speicherZeigen();
  }

  async function cacheLeeren() {
    try {
      if (window.caches) {
        for (const name of await caches.keys()) await caches.delete(name);
      }
      if (navigator.serviceWorker) {
        for (const r of await navigator.serviceWorker.getRegistrations()) {
          await r.unregister();
        }
      }
    } catch (fehler) {
      K.melden("Cache zurücksetzen: " + fehler.message, "fehler");
      return;
    }
    // Neu laden mit frischem Gerüst; Anmeldung und Daten liegen nicht im Cache.
    location.reload();
  }

  /* ---------- Aktualisieren ---------- */

  /* Jede Sekunde, solange die Ansicht offen ist - nur das, was sich bewegt. */
  function aktualisieren() {
    const d = O.diagnose();
    verbindungZeigen(d);
    messwerteZeigen(d);
    rundenZeigen(d);
    protokollZeigen();
    el("einst-trennen").disabled = !d.verbunden;
    el("diag-fahrt-zeile").hidden = !fahrtLaeuft();
  }

  function oeffnen() {
    serverZeigen();
    pushZeigen();
    dongleEinstellungenZeigen();
    speicherZeigen();
    aktualisieren();
    if (!uhr) uhr = setInterval(aktualisieren, 1000);
  }

  function schliessen() {
    if (uhr) { clearInterval(uhr); uhr = null; }
  }

  /* Von app.js gerufen, wenn die Ansicht gewechselt wird. */
  function anzeigen(sichtbar) {
    if (sichtbar) oeffnen(); else schliessen();
  }

  function einrichten() {
    K.an("einst-abmelden", "click", abmelden);
    K.an("einst-push-an", "click", pushAktivieren);
    K.an("einst-push-aus", "click", pushAbmelden);
    K.an("einst-push-probe", "click", pushProbe);
    K.an("einst-auto", "change", autoUebernehmen);
    K.an("einst-trennen", "click", trennen);
    K.an("einst-vergessen", "click", vergessen);
    K.an("diag-auffaellig", "change", protokollZeigen);
    K.an("diag-kopieren", "click", berichtKopieren);
    K.an("diag-protokoll-leeren", "click", () => { O.protokollLeeren(); protokollZeigen(); });
    K.an("diag-zuruecksetzen", "click", () => { O.zaehlerZuruecksetzen(); aktualisieren(); });
    K.an("diag-senden", "click", befehlSenden);
    K.an("diag-befehl", "keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); befehlSenden(); }
    });
    K.an("lausch-start", "click", lauschenStarten);
    K.an("lausch-kopieren", "click", lauschenKopieren);
    K.an("einst-puffer-leeren", "click", pufferVerwerfen);
    K.an("einst-cache-leeren", "click", cacheLeeren);
  }

  return { einrichten, anzeigen, bericht };
})();
