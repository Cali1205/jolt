/* Assembly: navigation, start, registration of the service worker. */
window.joltApp = (function () {
  "use strict";

  const K = window.jolt;

  function showView(name) {
    for (const btn of document.querySelectorAll("nav button")) {
      const active = btn.dataset.view === name;
      btn.setAttribute("aria-selected", active ? "true" : "false");
      const section = document.getElementById("view-" + btn.dataset.view);
      if (section) section.hidden = !active;
    }
    mapRehang(name);
    // The trips list only fetches its data when someone is looking.
    if (name === "trips" && window.joltTrips) window.joltTrips.show();
    // The settings update every second - but only while they are being
    // looked at.
    if (window.joltSettings) window.joltSettings.show(name === "settings");
  }

  /* The one map moves into the currently visible view.
   *
   * When planning it shows the route, on the road the own location and the
   * moving reserve marker - and on the road that is the more important of
   * the two views. A second canvas would mean a second tile cache and two
   * states that drift apart; moving it in the DOM keeps context, cache and
   * zoom. */
  function mapRehang(view) {
    const block = document.getElementById("map-block");
    const holder = document.getElementById("map-holder-" + view);
    if (block && holder && block.parentElement !== holder) {
      holder.appendChild(block);
    }
    if (block) block.hidden = !holder;
    // In the hidden section the canvas had width zero. After showing it, it
    // has to be measured anew, otherwise it stays a line.
    if (window.joltMap) window.joltMap.drawNew();
    // The same for the history curve of the live view.
    if (view === "live" && window.joltLive
        && window.joltLive.drawHistory) window.joltLive.drawHistory();
  }

  async function launch() {
    for (const btn of document.querySelectorAll("nav button")) {
      btn.addEventListener("click", () => showView(btn.dataset.view));
    }

    window.joltMap.create("map");
    mapRehang("plan");
    window.joltRoute.set_up();
    window.joltLive.set_up();
    window.joltTrips.set_up();
    window.joltVehicle.set_up();
    window.joltSettings.set_up();

    let status;
    try {
      status = await K.api("/api/status");
    } catch (failure) {
      K.report("Server nicht erreichbar.", "fehler");
      return;
    }
    if (status.demo_routing) {
      document.getElementById("demo-badge").hidden = false;
      K.report("Ohne ORS_API_KEY rechnet jolt mit erfundenen Demo-Routen. "
        + "Ein kostenloser Schlüssel von openrouteservice.org macht daraus "
        + "echte Strecken mit Höhenprofil.", "warnung");
    }

    if (status.password_required && !(await signed_in())) {
      await sign_in();
    }

    await window.joltVehicle.templatesCharging();
    await window.joltVehicle.load();

    if ("serviceWorker" in navigator) {
      // The registration is kept because the subscription for the
      // notifications depends on it (see live.js). Without it there would be
      // no way to register the push receiver.
      try {
        K.state.serviceWorker = await navigator.serviceWorker.register("/sw.js");
      } catch (failure) {
        // Without a service worker everything keeps running, just without the
        // offline shell and without notifications when the screen is dark.
        K.state.serviceWorker = null;
      }
    }
  }

  /* A stored token may come from an expired session - only a real, protected
   * call shows whether it is still valid. */
  async function signed_in() {
    if (!K.token()) return false;
    try {
      await K.api("/api/vehicles/templates");
      return true;
    } catch (failure) {
      return false;
    }
  }

  /* Blocks until the login is in place - everything after it presupposes a
   * valid token. Nav and content stay hidden until then: showing a UI that
   * only returns 401 on every click would be worse than none at all. */
  function sign_in() {
    for (const section of document.querySelectorAll("main > section")) {
      section.hidden = section.id !== "view-login";
    }
    document.querySelector("nav").hidden = true;

    return new Promise((fulfil) => {
      const form = document.getElementById("login-form");
      const field = document.getElementById("login-password");
      const errorElement = document.getElementById("login-error");

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        errorElement.hidden = true;
        try {
          const response = await K.api("/api/login", { method: "POST", body: {
            password: field.value, device: navigator.userAgent.slice(0, 120) }});
          K.setToken(response.token);
          document.getElementById("view-login").hidden = true;
          document.querySelector("nav").hidden = false;
          showView("plan");
          fulfil();
        } catch (failure) {
          errorElement.textContent = failure.message;
          errorElement.hidden = false;
          field.value = "";
          field.focus();
        }
      });
    });
  }

  /* Set the version in the header line in local time.
   *
   * The server sends seconds because it runs in UTC and the phone in its own
   * zone; formatting is therefore done here. Two numbers, two questions: the
   * code version says **what** is running - if the old date is still there
   * after a deploy, either the image was not rebuilt or the page comes from
   * the cache. "seit" (since) says when the server last started. */
  function showAsOf() {
    const field = document.getElementById("status");
    if (!field) return;
    const as_of = Number(field.dataset.status);
    const start = Number(field.dataset.start);
    if (!as_of) return;                    // placeholder not replaced
    // By hand instead of via `toLocaleString`: the German format inserts a
    // comma between date and time, and the line is too short to afford
    // that.
    const two = (n) => String(n).padStart(2, "0");
    const date = (s, withDay) => {
      const d = new Date(s * 1000);
      const clock = two(d.getHours()) + ":" + two(d.getMinutes());
      return withDay
        ? two(d.getDate()) + "." + two(d.getMonth() + 1) + ". " + clock : clock;
    };
    field.textContent = date(as_of, true)
      + (start ? " · seit " + date(start, false) : "");
  }

  document.addEventListener("DOMContentLoaded", () => {
    launch();
    showAsOf();
  });

  return { showView };
})();
