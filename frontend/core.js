/* Common foundations: HTTP, messages, formatting, shared state.
 *
 * No framework and no build step. The UI has three views and a handful of
 * forms - installing a tool for that which wants to be reconfigured every
 * year would be more effort than the app itself.
 */
window.jolt = (function () {
  "use strict";

  const state = {
    trip: null,        // a chosen variant from POST /api/route
    vehicles: [],
    sessionId: null,    // running live session
    /* The vehicle of the running recording.
     *
     * A recording has no planned trip, hence no `zustand.fahrt.fahrzeug`
     * either - and without the battery size, a charge level cannot be turned
     * into a kilowatt hour. */
    recVehicle: null,
    serviceWorker: null, // registration, needed for the push subscription
    /* Whether the trips list has to be fetched anew.
     *
     * It is cached - whoever never taps the tab should not pay for it. But
     * whoever creates a trip does not know that a list exists, and whoever
     * shows the list does not know when a trip comes into being. Before, two
     * modules therefore called `joltTrips.veraltet()` and `trips.js` back
     * into both - two cycles for one mark.
     *
     * Here it is in the right place: whoever creates a trip sets it;
     * whoever shows the list reads it. Neither needs to know about the
     * other. */
    tripsStale: false,
  };

  const TOKEN_KEY = "jolt-token";
  const SESSION_KEY = "jolt-sitzung";

  /* The running session survives a reload.
   *
   * It used to exist only in memory. Whoever reloaded the page by accident -
   * one swipe too many on the phone - lost the connection to the running
   * trip and began a new session. On a real long-distance trip this happened
   * four times: 623 measurement points, spread over four sessions.
   *
   * The points are not lost, they are attached to the trip. But everything
   * that runs **across** the session starts over: the running consumption
   * factor, the time factor, the curve - and when ending, jolt only learns
   * from the last session instead of from the whole trip.
   */
  function sessionRemember(id) {
    try {
      if (id) localStorage.setItem(SESSION_KEY, String(id));
      else localStorage.removeItem(SESSION_KEY);
    } catch (e) { /* without storage, simply without memory */ }
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

  function setToken(value) {
    try { localStorage.setItem(TOKEN_KEY, value || ""); } catch (e) {}
  }

  /* A single place for all calls - so that the token, the error handling
   * and the JSON unpacking are not slightly different in twenty places. */
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
      // No `status`: whoever buffers uses it to tell "network gone" from
      // "server rejected".
      throw new Error("Server nicht erreichbar.");
    }

    let data = null;
    try { data = await response.json(); } catch (e) { /* empty response */ }

    if (!response.ok) {
      const reason = (data && (data.detail || data.message))
        || `HTTP ${response.status}`;
      const failure = new Error(typeof reason === "string" ? reason
                                                         : JSON.stringify(reason));
      failure.status = response.status;
      throw failure;
    }
    return data;
  }

  /* ---------- Meldungen ---------- */

  function report(text, variety) {
    const container = document.getElementById("reports");
    if (!container) return;
    const box = document.createElement("div");
    box.className = "meldung " + (variety || "hinweis");
    box.textContent = text;
    container.appendChild(box);
    // Errors stay until the next attempt runs - a message that disappears
    // after three seconds was never read on the road.
    if (variety !== "fehler") {
      setTimeout(() => box.remove(), 6000);
    }
  }

  function reportsClear() {
    const container = document.getElementById("reports");
    if (container) container.innerHTML = "";
  }

  /* ---------- Formatierung ---------- */

  const num = (value, digits) =>
    (value === null || value === undefined || Number.isNaN(value))
      ? "–"
      : Number(value).toLocaleString("de-DE", {
          minimumFractionDigits: digits || 0,
          maximumFractionDigits: digits || 0 });

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

  /* A time from the server as a Date - or null.
   *
   * The server delivers UTC with Z. If the zone is missing (older response,
   * other source), UTC applies and **not** local time: `new Date("2026-10-05T15:56:21")`
   * reads the text as the device's local time. In summer time that gave
   * 15:56 where it was 17:56, and every comparison with `Date.now()` was two
   * hours off - that is how jolt, after a reload, took the last vehicle
   * values to be two hours old and rebuilt the dongle connection. */
  function timestamp(text) {
    if (typeof text !== "string" || !text) return null;
    // JavaScript reads only a date without a time as UTC anyway.
    const hasZone = !text.includes("T") || /(Z|[+-]\d{2}:?\d{2})$/i.test(text);
    const d = new Date(hasZone ? text : text + "Z");
    return Number.isNaN(d.getTime()) ? null : d;
  }

  /* The same as milliseconds - NaN if it is not a time. */
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

  /* Names come from outside (vehicle names, price patterns): never put
   * them into innerHTML or an attribute as they are. */
  function esc(text) {
    return String(text === null || text === undefined ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  return { state, esc, api, token, setToken, report, reportsClear,
           sessionRemember, rememberedSession,
           num, duration, valueTile, timestamp, timeMs, at, sliderCouple };
})();
