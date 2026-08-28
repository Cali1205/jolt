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

  const zahl = (wert, stellen) =>
    (wert === null || wert === undefined) ? "" : K.zahl(wert, stellen);

  /* Was eine Fahrt mit einer anderen unvergleichbar macht.
   *
   * Eine Fahrt mit Fahrradträger ist keine Vergleichsgrösse für eine ohne,
   * und ein blosser Entwurf keine für eine gefahrene Strecke. Ohne diese
   * Marken vergleicht man Äpfel mit Birnen und wundert sich über den
   * Verbrauch. */
  function marken(fahrt) {
    const m = [];
    if (fahrt.aufzeichnung) m.push('<span class="marke auf">aufgez.</span>');
    else if (fahrt.gefahren) m.push('<span class="marke gut">gefahren</span>');
    else m.push('<span class="marke">Entwurf</span>');
    if (fahrt.luftwiderstand_faktor && fahrt.luftwiderstand_faktor > 1.001) {
      m.push(`<span class="marke warn">Anbau ×${K.zahl(fahrt.luftwiderstand_faktor, 2)}</span>`);
    }
    return m.join(" ");
  }

  /* Eine Tabelle statt einer Liste aus punktgetrennten Sätzen.
   *
   * Der Zweck dieser Ansicht ist Vergleich - dieselbe Strecke im Januar und
   * im Juni, einmal leer und einmal beladen. Vergleichen heisst Zahlen
   * untereinander lesen, und dafür ist eine Tabelle das richtige Mittel:
   * gleiche Spalte, gleiche Stelle, rechtsbündig und in gleichbreiten
   * Ziffern. In einer Textzeile steht der Verbrauch mal an dritter, mal an
   * fünfter Stelle - je nachdem, was sonst noch bekannt ist.
   *
   * Auf schmalen Schirmen fallen die hinteren Spalten weg (siehe CSS), und
   * zwar in der Reihenfolge ihres Werts fürs Vergleichen. Was bleibt, ist
   * Datum, Strecke, Kilometer und Verbrauch. */
  /* Start und Ziel, oder nur das, was bekannt ist.
   *
   * Bei einer Aufzeichnung hat das Ziel keinen Namen - jolt kann Orte
   * suchen, aber nicht umgekehrt aus einer Koordinate einen Ortsnamen
   * machen. Ein Pfeil ins Leere ("Dienstag 14:32 → ?") sieht aus wie ein
   * Fehler; der Name der Aufzeichnung allein ist die ehrlichere Zeile. */
  function strecke(fahrt) {
    const von = (fahrt.start || "").trim();
    const nach = (fahrt.ziel || "").trim();
    if (von && nach) return `${von} → ${nach}`;
    return von || nach || "ohne Namen";
  }

  function zeile(fahrt) {
    const soc = (fahrt.soc_am_ziel === null || fahrt.soc_am_ziel === undefined)
      ? "" : `${zahl(fahrt.start_soc, 0)}→${zahl(fahrt.soc_am_ziel, 0)} %`;
    return `<tr data-id="${fahrt.id}">
      <td class="datum">${datum(fahrt.angelegt)}</td>
      <td class="strecke">
        <div class="titel">${strecke(fahrt)}</div>
        <div class="unter">${fahrt.fahrzeug || ""} ${marken(fahrt)}</div>
      </td>
      <td class="num">${zahl(fahrt.strecke_km, 0)}</td>
      <td class="num weg-eng">${K.dauer(fahrt.fahrzeit_minuten)}</td>
      <td class="num stark">${zahl(fahrt.verbrauch_kwh_100km, 1)}</td>
      <td class="num weg-schmal">${zahl(fahrt.kwh_gesamt, 0)}</td>
      <td class="num weg-schmal">${zahl(fahrt.aussentemp_c, 0)}</td>
      <td class="num weg-schmal">${zahl(fahrt.zuladung_kg, 0)}</td>
      <td class="num weg-schmal">${fahrt.tempo_faktor
        ? Math.round(fahrt.tempo_faktor * 100) : ""}</td>
      <td class="num weg-schmal">${soc}</td>
      <td class="tat-spalte">
        <button class="klein" data-oeffnen="${fahrt.id}">öffnen</button>
        <button class="klein" data-loeschen="${fahrt.id}">×</button>
      </td>
    </tr>`;
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
      halter.innerHTML = `
        <div class="tabelle-halter">
          <table class="fahrten">
            <thead><tr>
              <th class="datum">Datum</th>
              <th>Strecke</th>
              <th class="num">km</th>
              <th class="num weg-eng">Zeit</th>
              <th class="num">kWh/100</th>
              <th class="num weg-schmal">kWh</th>
              <th class="num weg-schmal">°C</th>
              <th class="num weg-schmal">Zuladung</th>
              <th class="num weg-schmal">Tempo %</th>
              <th class="num weg-schmal">Ladestand</th>
              <th></th>
            </tr></thead>
            <tbody>${fahrten.map(zeile).join("")}</tbody>
          </table>
        </div>`;
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

  /* Nachfragen, bevor gelöscht wird.
   *
   * Das Kreuz sitzt in einer Tabellenzeile, auf dem Telefon eine Daumenbreite
   * neben "öffnen". Und eine aufgezeichnete Fahrt ist nicht wiederherstellbar:
   * Sie ist eine Messung, die genau einmal stattgefunden hat - anders als eine
   * geplante Route, die man neu rechnen kann. */
  async function loeschen(id, beschriftung) {
    if (!window.confirm(`„${beschriftung}" löschen?\n\nEine aufgezeichnete `
                        + "Fahrt lässt sich nicht wiederherstellen.")) {
      return;
    }
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
      veraltet();   // die neue Aufzeichnung gehört in die Liste
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
      if (weg) {
        const zeile = weg.closest("tr");
        const titel = zeile ? zeile.querySelector(".titel") : null;
        loeschen(weg.dataset.loeschen,
                 titel ? titel.textContent.trim() : "Diese Fahrt");
      }
    });
  }

  // Beim Wechsel in die Ansicht laden, nicht beim Start: Wer nie auf den
  // Reiter tippt, soll die Liste auch nicht bezahlen.
  //
  // Aber `geladen` wurde nirgends zurückgesetzt, und damit war die Liste nach
  // dem ersten Öffnen eingefroren: Wer eine Route plante oder eine
  // Aufzeichnung beendete und dann hierher wechselte, sah sie nicht - bis er
  // die Seite neu lud. Das Zwischenspeichern soll den zweiten Blick sparen,
  // nicht neue Fahrten verstecken; deshalb hält `veraltet()` fest, dass sich
  // etwas geändert hat, und die Ansicht lädt beim nächsten Mal neu.
  function anzeigen() {
    if (!geladen) laden();
  }

  function veraltet() {
    geladen = false;
  }

  return { einrichten, laden, anzeigen, veraltet };
})();
