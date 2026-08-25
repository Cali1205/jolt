/* Die Fahrten-Ansicht: was bisher geplant und gefahren wurde.
 *
 * Der Zweck ist nicht Buchhaltung, sondern Vergleich: Dieselbe Strecke im
 * Januar und im Juni, einmal leer und einmal beladen - erst nebeneinander
 * wird sichtbar, woran die zwei Ladestopps Unterschied lagen. Deshalb steht
 * in jeder Zeile der Verbrauch neben Temperatur, Tempo und Zuladung, und
 * nicht nur Start und Ziel.
 */
window.joltFahrten = (function () {
  "use strict";

  const K = window.jolt;
  let geladen = false;

  function datum(iso) {
    if (!iso) return "–";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "–";
    return d.toLocaleString("de-DE", { day: "2-digit", month: "2-digit",
                                       year: "numeric", hour: "2-digit",
                                       minute: "2-digit" });
  }

  /* Die Zeile unter dem Titel: nur was tatsächlich bekannt ist.
   *
   * Ein "– °C" neben einem "– kg" ist keine Information, sondern Rauschen,
   * und Fahrten aus der Zeit vor dem Zuladungsfeld haben diesen Wert
   * zwangsläufig nicht. */
  function merkmale(fahrt) {
    const teile = [];
    if (fahrt.verbrauch_kwh_100km !== null && fahrt.verbrauch_kwh_100km !== undefined) {
      teile.push(K.zahl(fahrt.verbrauch_kwh_100km, 1) + " kWh/100 km");
    }
    if (fahrt.aussentemp_c !== null && fahrt.aussentemp_c !== undefined) {
      teile.push(K.zahl(fahrt.aussentemp_c, 1) + " °C");
    }
    if (fahrt.tempo_faktor && Math.abs(fahrt.tempo_faktor - 1) > 0.001) {
      teile.push("Tempo " + Math.round(fahrt.tempo_faktor * 100) + " %");
    }
    if (fahrt.zuladung_kg !== null && fahrt.zuladung_kg !== undefined) {
      teile.push(K.zahl(fahrt.zuladung_kg, 0) + " kg zu");
    }
    return teile.join(" · ");
  }

  function zeile(fahrt) {
    const strecke = K.zahl(fahrt.strecke_km, 1) + " km";
    const zeit = K.dauer(fahrt.fahrzeit_minuten);
    const soc = (fahrt.soc_am_ziel === null || fahrt.soc_am_ziel === undefined)
      ? "" : ` · ${K.zahl(fahrt.start_soc, 0)} % → ${K.zahl(fahrt.soc_am_ziel, 0)} %`;
    // Eine Fahrt ohne Live-Sitzung ist ein Entwurf: geplant, aber nie
    // gefahren. Jede Routenberechnung legt bis zu drei davon an.
    const marke = fahrt.gefahren
      ? '<span class="kw">gefahren</span>' : "";

    return `<li data-id="${fahrt.id}">
      <div class="haupt">
        <div class="titel">${fahrt.start || "?"} → ${fahrt.ziel || "?"}</div>
        <div class="unter">${datum(fahrt.angelegt)} · ${fahrt.fahrzeug}
          · ${strecke} · ${zeit}${soc}</div>
        <div class="unter">${merkmale(fahrt)}</div>
      </div>
      ${marke}
      <button class="tat neben" data-oeffnen="${fahrt.id}">Öffnen</button>
      <button class="tat neben" data-loeschen="${fahrt.id}">×</button>
    </li>`;
  }

  async function laden() {
    const halter = document.getElementById("fahrten-liste");
    if (!halter) return;
    halter.innerHTML = '<p class="leer">lädt …</p>';
    try {
      const fahrten = await K.api("/api/fahrten");
      if (!fahrten.length) {
        halter.innerHTML = '<p class="leer">Noch keine Fahrt geplant.</p>';
        return;
      }
      halter.innerHTML = `<ul class="liste">${fahrten.map(zeile).join("")}</ul>`;
      geladen = true;
    } catch (fehler) {
      halter.innerHTML = "";
      K.melden("Fahrten: " + fehler.message, "fehler");
    }
  }

  /* Eine alte Fahrt wieder auf die Karte holen.
   *
   * Bewusst über joltRoute und nicht mit eigener Zeichenlogik: Es ist
   * dieselbe Ansicht wie nach einer frischen Berechnung, und zwei Wege,
   * dasselbe zu zeichnen, laufen unweigerlich auseinander. */
  async function oeffnen(id) {
    try {
      await window.joltRoute.fahrtLaden(Number(id));
      window.joltApp.ansichtZeigen("planen");
    } catch (fehler) {
      K.melden("Fahrt öffnen: " + fehler.message, "fehler");
    }
  }

  async function loeschen(id) {
    try {
      await K.api("/api/fahrten/" + id, { method: "DELETE" });
      await laden();
    } catch (fehler) {
      K.melden("Löschen: " + fehler.message, "fehler");
    }
  }

  function einrichten() {
    const halter = document.getElementById("fahrten-liste");
    if (!halter) return;
    // Ein Zuhörer am Halter statt einer je Zeile: Die Liste wird nach jedem
    // Löschen neu gebaut, einzeln gebundene Zuhörer wären dann tot.
    halter.addEventListener("click", (ereignis) => {
      const auf = ereignis.target.closest("[data-oeffnen]");
      if (auf) { oeffnen(auf.dataset.oeffnen); return; }
      const weg = ereignis.target.closest("[data-loeschen]");
      if (weg) loeschen(weg.dataset.loeschen);
    });
  }

  // Beim Wechsel in die Ansicht laden, nicht beim Start: Wer nie auf den
  // Reiter tippt, soll die Liste auch nicht bezahlen.
  function anzeigen() {
    if (!geladen) laden();
  }

  return { einrichten, laden, anzeigen };
})();
