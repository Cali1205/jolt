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
  }

  async function starten() {
    for (const knopf of document.querySelectorAll("nav button")) {
      knopf.addEventListener("click", () => ansichtZeigen(knopf.dataset.ansicht));
    }

    window.joltKarte.erstellen("karte");
    karteUmhaengen("planen");
    window.joltRoute.einrichten();
    window.joltLive.einrichten();
    window.joltFahrzeug.einrichten();

    try {
      const status = await K.api("/api/status");
      if (status.demo_routing) {
        document.getElementById("demo-plakette").hidden = false;
        K.melden("Ohne ORS_API_KEY rechnet jolt mit erfundenen Demo-Routen. "
          + "Ein kostenloser Schlüssel von openrouteservice.org macht daraus "
          + "echte Strecken mit Höhenprofil.", "warnung");
      }
    } catch (fehler) {
      K.melden("Server nicht erreichbar.", "fehler");
      return;
    }

    await window.joltFahrzeug.vorlagenLaden();
    await window.joltFahrzeug.laden();

    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    }
  }

  document.addEventListener("DOMContentLoaded", starten);

  return { ansichtZeigen };
})();
