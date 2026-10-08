/* Zusammenbau: Navigation, Start, Registrierung des Service Workers. */
window.joltApp = (function () {
  "use strict";

  const K = window.jolt;

  function showView(name) {
    for (const btn of document.querySelectorAll("nav button")) {
      const active = btn.dataset.ansicht === name;
      btn.setAttribute("aria-selected", active ? "true" : "false");
      const section = document.getElementById("ansicht-" + btn.dataset.ansicht);
      if (section) section.hidden = !active;
    }
    mapRehang(name);
    // Die Fahrtenliste holt sich ihre Daten erst, wenn jemand hinsieht.
    if (name === "fahrten" && window.joltTrips) window.joltTrips.show();
    // Die Einstellungen aktualisieren sich sekündlich - aber nur, solange man
    // sie ansieht.
    if (window.joltSettings) window.joltSettings.show(name === "einstellungen");
  }

  /* Die eine Karte wandert in die gerade sichtbare Ansicht.
   *
   * Beim Planen zeigt sie die Route, unterwegs den eigenen Standort und die
   * wandernde Reserve-Marke - und das ist unterwegs die wichtigere der
   * beiden Ansichten. Ein zweites Canvas hiesse ein zweiter Kachel-Cache
   * und zwei Zustände, die auseinanderlaufen; ein Verschieben im DOM behält
   * Kontext, Cache und Zoom. */
  function mapRehang(view) {
    const block = document.getElementById("karte-block");
    const holder = document.getElementById("karte-halter-" + view);
    if (block && holder && block.parentElement !== holder) {
      holder.appendChild(block);
    }
    if (block) block.hidden = !holder;
    // Im versteckten Abschnitt hatte das Canvas die Breite null. Nach dem
    // Einblenden muss es neu vermessen werden, sonst bleibt es ein Strich.
    if (window.joltMap) window.joltMap.drawNew();
    // Dasselbe für die Verlaufskurve der Live-Ansicht.
    if (view === "live" && window.joltLive
        && window.joltLive.drawHistory) window.joltLive.drawHistory();
  }

  async function launch() {
    for (const btn of document.querySelectorAll("nav button")) {
      btn.addEventListener("click", () => showView(btn.dataset.ansicht));
    }

    window.joltMap.create("karte");
    mapRehang("planen");
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
      document.getElementById("demo-plakette").hidden = false;
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
      // Die Registrierung wird festgehalten, weil das Abo für die
      // Benachrichtigungen daran hängt (siehe live.js). Ohne sie gäbe es
      // keinen Weg, den Push-Empfänger anzumelden.
      try {
        K.state.serviceWorker = await navigator.serviceWorker.register("/sw.js");
      } catch (failure) {
        // Ohne Service Worker läuft alles weiter, nur eben ohne Offline-Gerüst
        // und ohne Benachrichtigungen bei dunklem Bildschirm.
        K.state.serviceWorker = null;
      }
    }
  }

  /* Ein gespeichertes Token kann von einer abgelaufenen Sitzung stammen - erst
   * ein echter, geschützter Aufruf zeigt, ob es noch gilt. */
  async function signed_in() {
    if (!K.token()) return false;
    try {
      await K.api("/api/fahrzeuge/vorlagen");
      return true;
    } catch (failure) {
      return false;
    }
  }

  /* Blockiert, bis die Anmeldung sitzt - alles danach setzt ein gültiges
   * Token voraus. Nav und Inhalt bleiben bis dahin verborgen: Eine
   * Oberfläche zu zeigen, die bei jedem Klick nur 401 zurückgibt, wäre
   * schlimmer als gar keine. */
  function sign_in() {
    for (const section of document.querySelectorAll("main > section")) {
      section.hidden = section.id !== "ansicht-login";
    }
    document.querySelector("nav").hidden = true;

    return new Promise((fulfil) => {
      const form = document.getElementById("login-formular");
      const field = document.getElementById("login-passwort");
      const errorElement = document.getElementById("login-fehler");

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        errorElement.hidden = true;
        try {
          const response = await K.api("/api/login", { method: "POST", body: {
            password: field.value, device: navigator.userAgent.slice(0, 120) }});
          K.setToken(response.token);
          document.getElementById("ansicht-login").hidden = true;
          document.querySelector("nav").hidden = false;
          showView("planen");
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

  /* Die Fassung in der Kopfzeile in Ortszeit setzen.
   *
   * Der Server schickt Sekunden, weil er in UTC läuft und das Telefon in
   * seiner eigenen Zone; formatiert wird deshalb hier. Zwei Zahlen, zwei
   * Fragen: Der Code-Stand sagt, **was** läuft - steht dort nach einem
   * Deploy noch das alte Datum, ist entweder das Image nicht neu gebaut
   * oder die Seite kommt aus dem Cache. "seit" sagt, wann der Server
   * zuletzt gestartet ist. */
  function showAsOf() {
    const field = document.getElementById("stand");
    if (!field) return;
    const as_of = Number(field.dataset.stand);
    const start = Number(field.dataset.start);
    if (!as_of) return;                    // Platzhalter nicht ersetzt
    // Von Hand statt über `toLocaleString`: Das deutsche Format schiebt
    // zwischen Datum und Uhrzeit ein Komma, und die Zeile ist zu kurz, um
    // sich das leisten zu können.
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
