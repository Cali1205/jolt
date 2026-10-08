/* Gemeinsame Grundlagen: HTTP, Meldungen, Formatierung, geteilter Zustand.
 *
 * Kein Framework und kein Build-Schritt. Die Oberfläche hat drei Ansichten und
 * eine Handvoll Formulare - dafür ein Werkzeug zu installieren, das jährlich
 * neu konfiguriert werden will, wäre mehr Aufwand als die App selbst.
 */
window.jolt = (function () {
  "use strict";

  const state = {
    trip: null,        // eine gewählte Variante aus POST /api/route
    vehicles: [],
    sessionId: null,    // laufende Live-Sitzung
    /* Das Fahrzeug der laufenden Aufzeichnung.
     *
     * Eine Aufzeichnung hat keine geplante Fahrt, also auch kein
     * `zustand.fahrt.fahrzeug` - und ohne die Akkugrösse lässt sich aus
     * einem Ladestand keine Kilowattstunde machen. */
    recVehicle: null,
    serviceWorker: null, // Registrierung, für das Push-Abo gebraucht
    /* Ob die Fahrtenliste neu geholt werden muss.
     *
     * Sie wird zwischengespeichert - wer nie auf den Reiter tippt, soll sie
     * nicht bezahlen. Nur weiss der, der eine Fahrt anlegt, nicht, dass es
     * eine Liste gibt, und der, der die Liste zeigt, nicht, wann eine Fahrt
     * entsteht. Vorher riefen deshalb zwei Module `joltFahrten.veraltet()`
     * und `trips.js` in beide zurück - zwei Zyklen für eine Marke.
     *
     * Hier ist sie richtig aufgehoben: Wer eine Fahrt anlegt, setzt sie;
     * wer die Liste zeigt, liest sie. Keiner muss vom anderen wissen. */
    tripsStale: false,
  };

  const TOKEN_KEY = "jolt-token";
  const SESSION_KEY = "jolt-sitzung";

  /* Die laufende Sitzung überlebt ein Neuladen.
   *
   * Sie stand nur im Speicher. Wer die Seite versehentlich neu lud - auf
   * dem Telefon ein Wisch zu viel -, verlor die Verbindung zur laufenden
   * Fahrt und begann eine neue Sitzung. Auf einer echten Langstrecke ist
   * das viermal passiert: 623 Messpunkte, verteilt auf vier Sitzungen.
   *
   * Die Punkte gehen dabei nicht verloren, sie hängen an der Fahrt. Aber
   * alles, was **über** die Sitzung läuft, beginnt von vorn: der laufende
   * Verbrauchsfaktor, der Zeitfaktor, die Kurve - und beim Beenden lernt
   * jolt nur aus der letzten Sitzung statt aus der ganzen Fahrt.
   */
  function sessionRemember(id) {
    try {
      if (id) localStorage.setItem(SESSION_KEY, String(id));
      else localStorage.removeItem(SESSION_KEY);
    } catch (e) { /* ohne Speicher eben ohne Gedächtnis */ }
  }

  function rememberedSession() {
    try {
      const raw = localStorage.getItem(SESSION_KEY);
      return raw ? Number(raw) : null;
    } catch (e) { return null; }
  }

  function token() {
    try { return localStorage.getItem(TOKEN_KEY) || ""; }
    catch (e) { return ""; }
  }

  function setToken(val) {
    try { localStorage.setItem(TOKEN_KEY, val || ""); } catch (e) {}
  }

  /* Ein einziger Ort für alle Aufrufe - damit der Token, die Fehlerbehandlung
   * und das JSON-Auspacken nicht an zwanzig Stellen leicht verschieden sind. */
  async function api(fs_path, options) {
    const opt = Object.assign({ headers: {} }, options || {});
    opt.headers = Object.assign({ "X-Token": token() }, opt.headers);
    if (opt.body !== undefined && typeof opt.body !== "string") {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(opt.body);
    }

    let response;
    try {
      response = await fetch(fs_path, opt);
    } catch (e) {
      // Kein `status`: Wer puffert, unterscheidet damit "Netz weg" von
      // "Server hat abgelehnt".
      throw new Error("Server nicht erreichbar.");
    }

    let records = null;
    try { records = await response.json(); } catch (e) { /* leere Antwort */ }

    if (!response.ok) {
      const reason = (records && (records.detail || records.message))
        || `HTTP ${response.status}`;
      const failure = new Error(typeof reason === "string" ? reason
                                                         : JSON.stringify(reason));
      failure.status = response.status;
      throw failure;
    }
    return records;
  }

  /* ---------- Meldungen ---------- */

  function report(text, variety) {
    const container = document.getElementById("meldungen");
    if (!container) return;
    const box = document.createElement("div");
    box.className = "meldung " + (variety || "hinweis");
    box.textContent = text;
    container.appendChild(box);
    // Fehler bleiben stehen, bis der nächste Versuch läuft - eine Meldung,
    // die nach drei Sekunden verschwindet, hat man unterwegs nie gelesen.
    if (variety !== "fehler") {
      setTimeout(() => box.remove(), 6000);
    }
  }

  function reportsClear() {
    const container = document.getElementById("meldungen");
    if (container) container.innerHTML = "";
  }

  /* ---------- Formatierung ---------- */

  const num = (val, put) =>
    (val === null || val === undefined || Number.isNaN(val))
      ? "–"
      : Number(val).toLocaleString("de-DE", {
          minimumFractionDigits: put || 0,
          maximumFractionDigits: put || 0 });

  function duration(mins) {
    if (mins === null || mins === undefined) return "–";
    const m = Math.round(mins);
    if (m < 60) return m + " min";
    return Math.floor(m / 60) + " h " + String(m % 60).padStart(2, "0");
  }

  function valueTile(name, numberText, variety) {
    return `<div class="wert ${variety || ""}">
      <div class="zahl">${numberText}</div><div class="name">${name}</div></div>`;
  }

  /* ---------- Zeiten vom Server ---------- */

  /* Eine Zeit vom Server als Date - oder null.
   *
   * Der Server liefert UTC mit Z. Fehlt die Zone (ältere Antwort, andere
   * Quelle), gilt UTC und **nicht** die Ortszeit: `new Date("2026-10-05T15:56:21")`
   * liest den Text als Ortszeit des Geräts. In Sommerzeit stand dann 15:56 Uhr,
   * wo es 17:56 war, und jeder Vergleich mit `Date.now()` lag zwei Stunden
   * daneben - so hielt jolt nach dem Neuladen die letzten Fahrzeugwerte für
   * zwei Stunden alt und baute die Dongle-Verbindung neu auf. */
  function timestamp(text) {
    if (typeof text !== "string" || !text) return null;
    // Nur ein Datum ohne Uhrzeit liest JavaScript ohnehin als UTC.
    const hasZone = !text.includes("T") || /(Z|[+-]\d{2}:?\d{2})$/i.test(text);
    const d = new Date(hasZone ? text : text + "Z");
    return Number.isNaN(d.getTime()) ? null : d;
  }

  /* Dasselbe als Millisekunden - NaN, wenn es keine Zeit ist. */
  function timeMs(text) {
    const d = timestamp(text);
    return d === null ? NaN : d.getTime();
  }

  /* ---------- Kleinkram ---------- */

  function at(id, event, fn) {
    const el = document.getElementById(id);
    if (el) el.addEventListener(event, fn);
    return el;
  }

  function sliderCouple(sliderId, displayId, atChange) {
    const slider = document.getElementById(sliderId);
    const display = document.getElementById(displayId);
    if (!slider || !display) return;
    const refresh = () => {
      display.textContent = slider.value;
      if (atChange) atChange(Number(slider.value));
    };
    slider.addEventListener("input", refresh);
    refresh();
  }

  return { state, api, token, setToken, report, reportsClear,
           sessionRemember, rememberedSession,
           num, duration, valueTile, timestamp, timeMs, at, sliderCouple };
})();
