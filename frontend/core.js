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
    /* Das Fahrzeug der laufenden Aufzeichnung.
     *
     * Eine Aufzeichnung hat keine geplante Fahrt, also auch kein
     * `zustand.fahrt.fahrzeug` - und ohne die Akkugrösse lässt sich aus
     * einem Ladestand keine Kilowattstunde machen. */
    aufzFahrzeug: null,
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
  const SITZUNG_SCHLUESSEL = "jolt-sitzung";

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
  function sitzungMerken(id) {
    try {
      if (id) localStorage.setItem(SITZUNG_SCHLUESSEL, String(id));
      else localStorage.removeItem(SITZUNG_SCHLUESSEL);
    } catch (e) { /* ohne Speicher eben ohne Gedächtnis */ }
  }

  function gemerkteSitzung() {
    try {
      const roh = localStorage.getItem(SITZUNG_SCHLUESSEL);
      return roh ? Number(roh) : null;
    } catch (e) { return null; }
  }

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
      // Kein `status`: Wer puffert, unterscheidet damit "Netz weg" von
      // "Server hat abgelehnt".
      throw new Error("Server nicht erreichbar.");
    }

    let daten = null;
    try { daten = await antwort.json(); } catch (e) { /* leere Antwort */ }

    if (!antwort.ok) {
      const grund = (daten && (daten.detail || daten.message))
        || `HTTP ${antwort.status}`;
      const fehler = new Error(typeof grund === "string" ? grund
                                                         : JSON.stringify(grund));
      fehler.status = antwort.status;
      throw fehler;
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

  /* ---------- Zeiten vom Server ---------- */

  /* Eine Zeit vom Server als Date - oder null.
   *
   * Der Server liefert UTC mit Z. Fehlt die Zone (ältere Antwort, andere
   * Quelle), gilt UTC und **nicht** die Ortszeit: `new Date("2026-10-05T15:56:21")`
   * liest den Text als Ortszeit des Geräts. In Sommerzeit stand dann 15:56 Uhr,
   * wo es 17:56 war, und jeder Vergleich mit `Date.now()` lag zwei Stunden
   * daneben - so hielt jolt nach dem Neuladen die letzten Fahrzeugwerte für
   * zwei Stunden alt und baute die Dongle-Verbindung neu auf. */
  function zeit(text) {
    if (typeof text !== "string" || !text) return null;
    // Nur ein Datum ohne Uhrzeit liest JavaScript ohnehin als UTC.
    const hatZone = !text.includes("T") || /(Z|[+-]\d{2}:?\d{2})$/i.test(text);
    const d = new Date(hatZone ? text : text + "Z");
    return Number.isNaN(d.getTime()) ? null : d;
  }

  /* Dasselbe als Millisekunden - NaN, wenn es keine Zeit ist. */
  function zeitMs(text) {
    const d = zeit(text);
    return d === null ? NaN : d.getTime();
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
           sitzungMerken, gemerkteSitzung,
           zahl, dauer, wertKachel, zeit, zeitMs, an, reglerKoppeln };
})();
