/* Gemeinsame Grundlagen: HTTP, Meldungen, Formatierung, geteilter Zustand.
 *
 * Kein Framework und kein Build-Schritt. Die Oberfläche hat drei Ansichten und
 * eine Handvoll Formulare - dafür ein Werkzeug zu installieren, das jährlich
 * neu konfiguriert werden will, wäre mehr Aufwand als die App selbst.
 */
window.jolt = (function () {
  "use strict";

  const zustand = {
    fahrt: null,        // eine gewählte Variante aus POST /api/route
    fahrzeuge: [],
    sitzungId: null,    // laufende Live-Sitzung
    serviceWorker: null, // Registrierung, für das Push-Abo gebraucht
    /* Ob die Fahrtenliste neu geholt werden muss.
     *
     * Sie wird zwischengespeichert - wer nie auf den Reiter tippt, soll sie
     * nicht bezahlen. Nur weiss der, der eine Fahrt anlegt, nicht, dass es
     * eine Liste gibt, und der, der die Liste zeigt, nicht, wann eine Fahrt
     * entsteht. Vorher riefen deshalb zwei Module `joltFahrten.veraltet()`
     * und `fahrten.js` in beide zurück - zwei Zyklen für eine Marke.
     *
     * Hier ist sie richtig aufgehoben: Wer eine Fahrt anlegt, setzt sie;
     * wer die Liste zeigt, liest sie. Keiner muss vom anderen wissen. */
    fahrtenVeraltet: false,
  };

  const TOKEN_SCHLUESSEL = "jolt-token";

  function token() {
    try { return localStorage.getItem(TOKEN_SCHLUESSEL) || ""; }
    catch (e) { return ""; }
  }

  function tokenSetzen(wert) {
    try { localStorage.setItem(TOKEN_SCHLUESSEL, wert || ""); } catch (e) {}
  }

  /* Ein einziger Ort für alle Aufrufe - damit der Token, die Fehlerbehandlung
   * und das JSON-Auspacken nicht an zwanzig Stellen leicht verschieden sind. */
  async function api(pfad, optionen) {
    const opt = Object.assign({ headers: {} }, optionen || {});
    opt.headers = Object.assign({ "X-Token": token() }, opt.headers);
    if (opt.body !== undefined && typeof opt.body !== "string") {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(opt.body);
    }

    let antwort;
    try {
      antwort = await fetch(pfad, opt);
    } catch (e) {
      throw new Error("Server nicht erreichbar.");
    }

    let daten = null;
    try { daten = await antwort.json(); } catch (e) { /* leere Antwort */ }

    if (!antwort.ok) {
      const grund = (daten && (daten.detail || daten.message))
        || `HTTP ${antwort.status}`;
      throw new Error(typeof grund === "string" ? grund : JSON.stringify(grund));
    }
    return daten;
  }

  /* ---------- Meldungen ---------- */

  function melden(text, art) {
    const behaelter = document.getElementById("meldungen");
    if (!behaelter) return;
    const kasten = document.createElement("div");
    kasten.className = "meldung " + (art || "hinweis");
    kasten.textContent = text;
    behaelter.appendChild(kasten);
    // Fehler bleiben stehen, bis der nächste Versuch läuft - eine Meldung,
    // die nach drei Sekunden verschwindet, hat man unterwegs nie gelesen.
    if (art !== "fehler") {
      setTimeout(() => kasten.remove(), 6000);
    }
  }

  function meldungenLeeren() {
    const behaelter = document.getElementById("meldungen");
    if (behaelter) behaelter.innerHTML = "";
  }

  /* ---------- Formatierung ---------- */

  const zahl = (wert, stellen) =>
    (wert === null || wert === undefined || Number.isNaN(wert))
      ? "–"
      : Number(wert).toLocaleString("de-DE", {
          minimumFractionDigits: stellen || 0,
          maximumFractionDigits: stellen || 0 });

  function dauer(minuten) {
    if (minuten === null || minuten === undefined) return "–";
    const m = Math.round(minuten);
    if (m < 60) return m + " min";
    return Math.floor(m / 60) + " h " + String(m % 60).padStart(2, "0");
  }

  function wertKachel(name, zahlText, art) {
    return `<div class="wert ${art || ""}">
      <div class="zahl">${zahlText}</div><div class="name">${name}</div></div>`;
  }

  /* ---------- Kleinkram ---------- */

  function an(id, ereignis, funktion) {
    const el = document.getElementById(id);
    if (el) el.addEventListener(ereignis, funktion);
    return el;
  }

  function reglerKoppeln(reglerId, anzeigeId, beiAenderung) {
    const regler = document.getElementById(reglerId);
    const anzeige = document.getElementById(anzeigeId);
    if (!regler || !anzeige) return;
    const aktualisieren = () => {
      anzeige.textContent = regler.value;
      if (beiAenderung) beiAenderung(Number(regler.value));
    };
    regler.addEventListener("input", aktualisieren);
    aktualisieren();
  }

  return { zustand, api, token, tokenSetzen, melden, meldungenLeeren,
           zahl, dauer, wertKachel, an, reglerKoppeln };
})();
