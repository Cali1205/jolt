/* Zusammenbau: Navigation, Start, Registrierung des Service Workers. */
window.joltApp = (function () {
  "use strict";

  const K = window.jolt;

  function ansichtZeigen(name) {
    for (const knopf of document.querySelectorAll("nav button")) {
      const aktiv = knopf.dataset.ansicht === name;
      knopf.setAttribute("aria-selected", aktiv ? "true" : "false");
      const abschnitt = document.getElementById("ansicht-" + knopf.dataset.ansicht);
      if (abschnitt) abschnitt.hidden = !aktiv;
    }
    karteUmhaengen(name);
    // Die Fahrtenliste holt sich ihre Daten erst, wenn jemand hinsieht.
    if (name === "fahrten" && window.joltFahrten) window.joltFahrten.anzeigen();
  }

  /* Die eine Karte wandert in die gerade sichtbare Ansicht.
   *
   * Beim Planen zeigt sie die Route, unterwegs den eigenen Standort und die
   * wandernde Reserve-Marke - und das ist unterwegs die wichtigere der
   * beiden Ansichten. Ein zweites Canvas hiesse ein zweiter Kachel-Cache
   * und zwei Zustände, die auseinanderlaufen; ein Verschieben im DOM behält
   * Kontext, Cache und Zoom. */
  function karteUmhaengen(ansicht) {
    const block = document.getElementById("karte-block");
    const halter = document.getElementById("karte-halter-" + ansicht);
    if (block && halter && block.parentElement !== halter) {
      halter.appendChild(block);
    }
    if (block) block.hidden = !halter;
    // Im versteckten Abschnitt hatte das Canvas die Breite null. Nach dem
    // Einblenden muss es neu vermessen werden, sonst bleibt es ein Strich.
    if (window.joltKarte) window.joltKarte.neuZeichnen();
    // Dasselbe für die Verlaufskurve der Live-Ansicht.
    if (ansicht === "live" && window.joltLive
        && window.joltLive.verlaufZeichnen) window.joltLive.verlaufZeichnen();
  }

  async function starten() {
    for (const knopf of document.querySelectorAll("nav button")) {
      knopf.addEventListener("click", () => ansichtZeigen(knopf.dataset.ansicht));
    }

    window.joltKarte.erstellen("karte");
    karteUmhaengen("planen");
    window.joltRoute.einrichten();
    window.joltLive.einrichten();
    window.joltFahrten.einrichten();
    window.joltFahrzeug.einrichten();

    let status;
    try {
      status = await K.api("/api/status");
    } catch (fehler) {
      K.melden("Server nicht erreichbar.", "fehler");
      return;
    }
    if (status.demo_routing) {
      document.getElementById("demo-plakette").hidden = false;
      K.melden("Ohne ORS_API_KEY rechnet jolt mit erfundenen Demo-Routen. "
        + "Ein kostenloser Schlüssel von openrouteservice.org macht daraus "
        + "echte Strecken mit Höhenprofil.", "warnung");
    }

    if (status.passwort_noetig && !(await angemeldet())) {
      await anmelden();
    }

    await window.joltFahrzeug.vorlagenLaden();
    await window.joltFahrzeug.laden();

    if ("serviceWorker" in navigator) {
      // Die Registrierung wird festgehalten, weil das Abo für die
      // Benachrichtigungen daran hängt (siehe live.js). Ohne sie gäbe es
      // keinen Weg, den Push-Empfänger anzumelden.
      try {
        K.zustand.serviceWorker = await navigator.serviceWorker.register("/sw.js");
      } catch (fehler) {
        // Ohne Service Worker läuft alles weiter, nur eben ohne Offline-Gerüst
        // und ohne Benachrichtigungen bei dunklem Bildschirm.
        K.zustand.serviceWorker = null;
      }
    }
  }

  /* Ein gespeichertes Token kann von einer abgelaufenen Sitzung stammen - erst
   * ein echter, geschützter Aufruf zeigt, ob es noch gilt. */
  async function angemeldet() {
    if (!K.token()) return false;
    try {
      await K.api("/api/fahrzeuge/vorlagen");
      return true;
    } catch (fehler) {
      return false;
    }
  }

  /* Blockiert, bis die Anmeldung sitzt - alles danach setzt ein gültiges
   * Token voraus. Nav und Inhalt bleiben bis dahin verborgen: Eine
   * Oberfläche zu zeigen, die bei jedem Klick nur 401 zurückgibt, wäre
   * schlimmer als gar keine. */
  function anmelden() {
    for (const abschnitt of document.querySelectorAll("main > section")) {
      abschnitt.hidden = abschnitt.id !== "ansicht-login";
    }
    document.querySelector("nav").hidden = true;

    return new Promise((erfuellen) => {
      const formular = document.getElementById("login-formular");
      const feld = document.getElementById("login-passwort");
      const fehlerElement = document.getElementById("login-fehler");

      formular.addEventListener("submit", async (ereignis) => {
        ereignis.preventDefault();
        fehlerElement.hidden = true;
        try {
          const antwort = await K.api("/api/login", { method: "POST", body: {
            passwort: feld.value, geraet: navigator.userAgent.slice(0, 120) }});
          K.tokenSetzen(antwort.token);
          document.getElementById("ansicht-login").hidden = true;
          document.querySelector("nav").hidden = false;
          ansichtZeigen("planen");
          erfuellen();
        } catch (fehler) {
          fehlerElement.textContent = fehler.message;
          fehlerElement.hidden = false;
          feld.value = "";
          feld.focus();
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
  function standZeigen() {
    const feld = document.getElementById("stand");
    if (!feld) return;
    const stand = Number(feld.dataset.stand);
    const start = Number(feld.dataset.start);
    if (!stand) return;                    // Platzhalter nicht ersetzt
    // Von Hand statt über `toLocaleString`: Das deutsche Format schiebt
    // zwischen Datum und Uhrzeit ein Komma, und die Zeile ist zu kurz, um
    // sich das leisten zu können.
    const zwei = (n) => String(n).padStart(2, "0");
    const datum = (s, mitTag) => {
      const d = new Date(s * 1000);
      const uhr = zwei(d.getHours()) + ":" + zwei(d.getMinutes());
      return mitTag
        ? zwei(d.getDate()) + "." + zwei(d.getMonth() + 1) + ". " + uhr : uhr;
    };
    feld.textContent = datum(stand, true)
      + (start ? " · seit " + datum(start, false) : "");
  }

  document.addEventListener("DOMContentLoaded", () => {
    starten();
    standZeigen();
  });

  return { ansichtZeigen };
})();
