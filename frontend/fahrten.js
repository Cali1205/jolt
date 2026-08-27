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

  /* Eine Aufzeichnung starten: Position holen, Fahrt anlegen, in die
   * Live-Ansicht wechseln. Von da an ist es eine Live-Fahrt wie jede
   * andere - nur ohne Plan, gegen den sie sich hält. Strecke und
   * Energieprofil entstehen beim Beenden aus den Messpunkten. */
  /* Eine Aufzeichnung starten - mit Dongle, wenn er zu haben ist.
   *
   * Die Reihenfolge ist nicht beliebig: `requestDevice` darf nur in
   * unmittelbarer Folge einer Nutzergeste laufen. Wer vorher auf GPS oder
   * eine API-Antwort wartet, hat die Geste verbraucht und bekommt ein
   * `SecurityError` - deshalb steht der Dongle **zuerst**, noch vor allem
   * anderen.
   *
   * Scheitert er, geht es ohne weiter. Das ist der ganze Sinn: In Safari
   * gibt es Web Bluetooth nicht, im Auto steckt der Dongle vielleicht
   * nicht, und in beiden Fällen ist eine Aufzeichnung mit von Hand
   * gemeldetem Ladestand besser als keine.
   */
  async function aufzeichnungStarten() {
    const knopf = document.getElementById("aufz-start");
    const stand = (text) => {
      const el = document.getElementById("aufz-stand");
      if (el) el.textContent = text;
    };
    knopf.disabled = true;
    try {
      let mitDongle = false;
      if (window.joltObd && window.joltObd.verfuegbar()) {
        stand("Verbinde mit dem OBD2-Dongle …");
        try {
          window.joltObd.einrichten((t) => console.log("[obd]", t));
          // Erst ohne Dialog: Ist der Dongle schon einmal erlaubt
          // worden, verbindet er ohne Berührung.
          await window.joltObd.anschliessen();
          if (window.joltObd.verbunden() && await window.joltObd.handshake()) {
            mitDongle = true;
          }
        } catch (fehler) {
          // Kein Grund abzubrechen - nur einer, ohne Dongle weiterzumachen.
          console.log("[obd] Verbindung nicht zustande gekommen:", fehler);
        }
        if (!mitDongle) {
          stand("Ohne Dongle – der Ladestand kommt von Hand.");
        }
      }

      const wahl = document.getElementById("fahrzeug-wahl");
      const id = wahl && wahl.value ? Number(wahl.value)
        : ((K.zustand.fahrzeuge || [])[0] || {}).id;
      if (!id) { K.melden("Erst ein Fahrzeug anlegen.", "fehler"); return; }

      stand("Standort holen …");
      const ort = await new Promise((erfuellen, ablehnen) => {
        if (!navigator.geolocation) {
          ablehnen(new Error("Dieses Gerät liefert keinen Standort."));
          return;
        }
        navigator.geolocation.getCurrentPosition(
          (p) => erfuellen(p.coords),
          // Ohne Startposition gäbe es keinen ersten Punkt der Strecke.
          (f) => ablehnen(new Error("Standort: " + f.message)),
          { enableHighAccuracy: true, timeout: 10000 });
      });

      // Mit Dongle gleich den echten Startladestand mitgeben - besser als
      // die 100 %, die der Server sonst annimmt.
      let soc = null;
      if (mitDongle) {
        try {
          const wert = window.joltObd.socAusAntwort(
            await window.joltObd.befehl("22028C"));
          if (wert) soc = Math.round(wert.hmi * 10) / 10;
        } catch (fehler) { mitDongle = false; }
      }

      stand("Fahrt anlegen …");
      const antwort = await K.api("/api/live/aufzeichnung", {
        method: "POST",
        body: { fahrzeug_id: id, lat: ort.latitude, lon: ort.longitude,
                soc: soc,
                name: document.getElementById("aufz-name").value },
      });
      K.zustand.sitzungId = antwort.sitzung_id;
      window.joltApp.ansichtZeigen("live");
      document.getElementById("live-leer").hidden = true;
      document.getElementById("live-inhalt").hidden = false;
      window.joltLive.verbinden(antwort.sitzung_id);
      window.joltLive.positionVerfolgen();
      if (mitDongle) {
        window.joltLive.dongleNutzen();
        K.melden("Aufzeichnung läuft, Ladestand kommt aus dem Auto.",
                 "hinweis");
      } else {
        K.melden("Aufzeichnung läuft. Den Ladestand unterwegs gelegentlich "
          + "melden – ohne ihn lässt sich hinterher nichts lernen.", "hinweis");
      }
      stand("");
    } catch (fehler) {
      K.melden("Aufzeichnung: " + fehler.message, "fehler");
      stand("");
    } finally {
      knopf.disabled = false;
    }
  }

  /* Beim Öffnen sagen, was dieser Browser kann - bevor jemand tippt und
   * sich wundert, dass kein Geräte-Dialog kommt. */
  function dongleHinweis() {
    const el = document.getElementById("aufz-dongle-hinweis");
    if (!el) return;
    el.innerHTML = (window.joltObd && window.joltObd.verfuegbar())
      ? "Dieser Browser kann Bluetooth – beim Starten wird versucht, den "
        + "OBD2-Dongle zu verbinden. Klappt es nicht, läuft die Aufzeichnung "
        + "trotzdem, dann mit dem Ladestand von Hand."
      : "Dieser Browser kann kein Bluetooth, der Ladestand kommt also von "
        + "Hand. Mit Dongle: dieselbe Adresse in <strong>Bluefy</strong> "
        + "öffnen, dann geht es automatisch.";
  }

  function einrichten() {
    K.an("aufz-start", "click", aufzeichnungStarten);
    dongleHinweis();
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
