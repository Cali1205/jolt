/* Die Fahrten-Ansicht: was bisher geplant und gefahren wurde.
 *
 * Der Zweck ist nicht Buchhaltung, sondern Vergleich: Dieselbe Strecke im
 * Januar und im Juni, einmal leer und einmal beladen - erst nebeneinander
 * wird sichtbar, woran die zwei Ladestopps Unterschied lagen. Deshalb steht
 * in jeder Zeile der Verbrauch neben Temperatur, Tempo und Zuladung, und
 * nicht nur Start und Ziel.
 */
window.joltTrips = (function () {
  "use strict";

  const K = window.jolt;
  let charged = false;

  function date(iso) {
    // Über K.zeit: Der Server liefert UTC, und `new Date` auf einem Text ohne
    // Zone läse es als Ortszeit - zwei Stunden daneben.
    const d = K.timestamp(iso);
    if (!d) return "–";
    return d.toLocaleString("de-DE", { day: "2-digit", month: "2-digit",
                                       year: "numeric", hour: "2-digit",
                                       minute: "2-digit" });
  }

  const num = (val, put) =>
    (val === null || val === undefined) ? "" : K.num(val, put);

  /* Was eine Fahrt mit einer anderen unvergleichbar macht.
   *
   * Eine Fahrt mit Fahrradträger ist keine Vergleichsgrösse für eine ohne,
   * und ein blosser Entwurf keine für eine gefahrene Strecke. Ohne diese
   * Marken vergleicht man Äpfel mit Birnen und wundert sich über den
   * Verbrauch. */
  function brands(trip) {
    const m = [];
    if (trip.recording) m.push('<span class="marke auf">aufgez.</span>');
    else if (trip.driven) m.push('<span class="marke gut">gefahren</span>');
    else m.push('<span class="marke">Entwurf</span>');
    if (trip.air_drag_factor && trip.air_drag_factor > 1.001) {
      m.push(`<span class="marke warn">Anbau ×${K.num(trip.air_drag_factor, 2)}</span>`);
    }
    if (trip.trailer_kg) {
      m.push(`<span class="marke warn">Anhänger ${K.num(trip.trailer_kg, 0)} kg</span>`);
    }
    if (trip.speed_max_kmh) {
      m.push(`<span class="marke">max ${K.num(trip.speed_max_kmh, 0)} km/h</span>`);
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
  function distance(trip) {
    const begin = (trip.start || "").trim();
    const past = (trip.destination || "").trim();
    if (begin && past) return `${begin} → ${past}`;
    return begin || past || "ohne Namen";
  }

  function row(trip) {
    const soc = (trip.soc_at_target === null || trip.soc_at_target === undefined)
      ? "" : `${num(trip.start_soc, 0)}→${num(trip.soc_at_target, 0)} %`;
    return `<tr data-id="${trip.id}">
      <td class="datum">${date(trip.created_at)}</td>
      <td class="strecke">
        <div class="titel">${distance(trip)}</div>
        <div class="unter">${trip.vehicle || ""} ${brands(trip)}</div>
      </td>
      <td class="num">${num(trip.distance_km, 0)}</td>
      <td class="num weg-eng">${K.duration(trip.drive_time_minutes)}</td>
      <td class="num stark">${num(trip.consumption_kwh_100km, 1)}</td>
      <td class="num weg-schmal">${num(trip.kwh_total, 0)}</td>
      <td class="num weg-schmal">${num(trip.outside_temp_c, 0)}</td>
      <td class="num weg-schmal">${num(trip.payload_kg, 0)}</td>
      <td class="num weg-schmal">${trip.speed_factor
        ? Math.round(trip.speed_factor * 100) : ""}</td>
      <td class="num weg-schmal">${soc}</td>
      <td class="tat-spalte">
        <button class="klein" data-oeffnen="${trip.id}">öffnen</button>
        <button class="klein" data-loeschen="${trip.id}">×</button>
      </td>
    </tr>`;
  }

  async function load() {
    const holder = document.getElementById("fahrten-liste");
    if (!holder) return;
    holder.innerHTML = '<p class="leer">lädt …</p>';
    try {
      const trips = await K.api("/api/fahrten");
      if (!trips.length) {
        holder.innerHTML = '<p class="leer">Noch keine Fahrt geplant.</p>';
        return;
      }
      holder.innerHTML = `
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
            <tbody>${trips.map(row).join("")}</tbody>
          </table>
        </div>`;
      charged = true;
    } catch (failure) {
      holder.innerHTML = "";
      K.report("Fahrten: " + failure.message, "fehler");
    }
  }

  /* Eine alte Fahrt wieder auf die Karte holen.
   *
   * Bewusst über joltRoute und nicht mit eigener Zeichenlogik: Es ist
   * dieselbe Ansicht wie nach einer frischen Berechnung, und zwei Wege,
   * dasselbe zu zeichnen, laufen unweigerlich auseinander. */
  async function open_it(id) {
    try {
      await window.joltRoute.tripCharging(Number(id));
      window.joltApp.showView("planen");
    } catch (failure) {
      K.report("Fahrt öffnen: " + failure.message, "fehler");
    }
  }

  /* Nachfragen, bevor gelöscht wird.
   *
   * Das Kreuz sitzt in einer Tabellenzeile, auf dem Telefon eine Daumenbreite
   * neben "öffnen". Und eine aufgezeichnete Fahrt ist nicht wiederherstellbar:
   * Sie ist eine Messung, die genau einmal stattgefunden hat - anders als eine
   * geplante Route, die man neu rechnen kann. */
  async function remove(id, label) {
    if (!window.confirm(`„${label}" löschen?\n\nEine aufgezeichnete `
                        + "Fahrt lässt sich nicht wiederherstellen.")) {
      return;
    }
    try {
      await K.api("/api/fahrten/" + id, { method: "DELETE" });
      await load();
    } catch (failure) {
      K.report("Löschen: " + failure.message, "fehler");
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
  // Warum der letzte Start nicht klappte - fuer CarPlay, das keine Meldung
  // der Oberflaeche sieht und den Grund selbst anzeigen muss.
  let lastStartError = "";

  async function startRecording() {
    lastStartError = "";
    const btn = document.getElementById("aufz-start");
    const as_of = (text) => {
      const el = document.getElementById("aufz-stand");
      if (el) el.textContent = text;
    };
    btn.disabled = true;
    try {
      let withDongle = false;
      let dongleLater = false;   // nicht verbunden, aber Bluetooth da
      if (window.joltObd && window.joltObd.obtainable()) {
        as_of("Verbinde mit dem OBD2-Dongle …");
        try {
          window.joltObd.set_up((t) => console.log("[obd]", t));
          // Erst ohne Dialog: Ist der Dongle schon einmal erlaubt
          // worden, verbindet er ohne Berührung.
          await window.joltObd.attach();
          if (await window.joltLive.handshakeSafe()) withDongle = true;
        } catch (failure) {
          // Kein Grund abzubrechen - nur einer, ohne Dongle weiterzumachen.
          console.log("[obd] Verbindung nicht zustande gekommen:", failure);
        }
        if (!withDongle) {
          as_of("Ohne Dongle – der Ladestand kommt von Hand.");
          dongleLater = true;
        }
      }

      // Die Wahl aus diesem Abschnitt, nicht die aus der Planen-Ansicht -
      // und **kein** Rückfall auf das erste Fahrzeug der Liste. Der war der
      // Fehler: Er schrieb die Fahrt stillschweigend dem "Allgemeinen
      // E-Auto" zu, und mit ihm rechnete danach alles - Akkugrösse, Masse,
      // Luftwiderstand. Lieber gar nicht aufzeichnen als dem falschen Auto.
      const choice = document.getElementById("aufz-fahrzeug");
      const id = choice && choice.value ? Number(choice.value) : null;
      if (!id) {
        K.report("Erst ein Fahrzeug wählen – ohne das gehört die "
                 + "Aufzeichnung niemandem.", "fehler");
        lastStartError = "Kein Fahrzeug gewählt – in jolt einmal auswählen.";
        return false;
      }

      as_of("Standort holen …");
      const city = await new Promise((fulfil, reject) => {
        if (!navigator.geolocation) {
          reject(new Error("Dieses Gerät liefert keinen Standort."));
          return;
        }
        navigator.geolocation.getCurrentPosition(
          (p) => fulfil(p.coords),
          // Ohne Startposition gäbe es keinen ersten Punkt der Strecke.
          (f) => reject(new Error("Standort: " + f.message)),
          { enableHighAccuracy: true, timeout: 10000 });
      });

      // Mit Dongle gleich den echten Startladestand mitgeben - besser als
      // die 100 %, die der Server sonst annimmt.
      let soc = null;
      if (withDongle) {
        try {
          const val = window.joltObd.socFromResponse(
            await window.joltObd.command("22028C"));
          if (val) soc = Math.round(val.hmi * 10) / 10;
        } catch (failure) {
          /* Keine Antwort auf die erste Abfrage heisst **nicht** "kein
           * Dongle". Ein Auto, das noch schläft, antwortet nicht - der
           * Dongle ist trotzdem da, und sobald es fährt, kommen die Werte.
           *
           * Hier stand `mitDongle = false`. Damit lief die ganze
           * Aufzeichnung ohne Fahrzeugwerte, obwohl der Dongle verbunden
           * war: Am 5.10. hat eine Fahrt (Sitzung 6) drei Minuten lang nur
           * GPS geliefert, bis jemand von Hand neu gestartet hat. Nur wenn
           * die Verbindung selbst weg ist, gilt der Dongle als nicht da. */
          if (!window.joltObd.linked()) {
            withDongle = false;
            dongleLater = true;
          } else {
            console.log("[obd] Startladestand ohne Antwort:", failure);
          }
        }
      }

      as_of("Fahrt anlegen …");
      const response = await K.api("/api/live/aufzeichnung", {
        method: "POST",
        body: { vehicle_id: id, lat: city.latitude, lon: city.longitude,
                soc: soc,
                name: document.getElementById("aufz-name").value },
      });
      K.state.sessionId = response.session_id;
      K.sessionRemember(response.session_id);
      K.state.recVehicle =
        (K.state.vehicles || []).find((f) => f.id === id) || null;
      // Die neue Aufzeichnung gehört in die Liste.
      K.state.tripsStale = true;
      window.joltApp.showView("live");
      document.getElementById("live-leer").hidden = true;
      document.getElementById("live-inhalt").hidden = false;
      window.joltLive.link(response.session_id);
      // Wer die Aufzeichnung startet, sitzt im Auto: Es darf gelesen werden,
      // bis das Telefon sagt, dass das Auto steht. Ohne das begann sie im
      // Zustand "steht" und las erst ab 15 km/h - im Stand also nie. Die
      // Live-Anzeige blieb leer, und wer nicht losfuhr, sah nichts.
      window.joltLive.drivingStateStart("faehrt");
      window.joltLive.positionTrace();
      if (withDongle) {
        window.joltLive.dongleUse();
        K.report(soc !== null
          ? "Aufzeichnung läuft, Ladestand kommt aus dem Auto."
          : "Aufzeichnung läuft. Das Auto antwortet noch nicht – jolt liest "
            + "den Ladestand, sobald du fährst.", "hinweis");
      } else {
        if (dongleLater) {
          // Der Dongle kann noch kommen: Das Auto wird oft erst jetzt
          // eingeschaltet. jolt klopft von selbst an, statt auf den Knopf
          // zu warten - und gibt nach einigen Versuchen auf.
          window.joltLive.dongleUse();
          window.joltLive.reconnectDongle();
        }
        K.report("Aufzeichnung läuft. Den Ladestand unterwegs gelegentlich "
          + "melden – ohne ihn lässt sich hinterher nichts lernen.", "hinweis");
      }
      // Das zuletzt aufgezeichnete Fahrzeug gilt beim naechsten Mal wieder -
      // auch fuer den Start aus CarPlay, wo man keines waehlen kann.
      try { localStorage.setItem("jolt-aufz-fahrzeug", String(id)); } catch (e) { /* ohne Gedaechtnis */ }
      reportVehicle();
      as_of("");
    } catch (failure) {
      K.report("Aufzeichnung: " + failure.message, "fehler");
      lastStartError = failure.message || "Start fehlgeschlagen.";
      as_of("");
    } finally {
      btn.disabled = false;
    }
    return !!K.state.sessionId;
  }

  /* ---------- CarPlay ----------
   *
   * Die CarPlay-Liste (plugins/jolt-anzeige) kann die Aufzeichnung starten und
   * beenden. Sie ruft hierher ueber ein Ereignis des Plugins und bekommt das
   * Ergebnis zurueck - schiefgegangenes zeigt CarPlay selbst an, denn die
   * Meldungen dieser Oberflaeche sieht im Auto niemand.
   *
   * Gestartet wird mit dem **zuletzt benutzten Fahrzeug**: Im Auto laesst sich
   * keines waehlen, und dieselbe Erinnerung nutzt der Start auf dem Telefon. */
  function carplayPlugin() {
    const shell = window.joltBlePlugin;
    if (!shell || !shell.JoltAnzeige || !shell.Capacitor
        || !shell.Capacitor.isNativePlatform()) return null;
    // The native plugin speaks German; this wrapper translates at the border.
    const native = shell.JoltAnzeige;
    return {
      addListener: (name, fn) => native.addListener(name, (e) => fn(e && { action: e.aktion })),
      ready: (a) => native.bereit({ fahrzeug: a.vehicle }),
      aktionErgebnis: (a) => native.aktionErgebnis({ aktion: a.action, ok: a.ok, text: a.text }),
    };
  }

  function reportVehicle() {
    const p = carplayPlugin();
    if (!p) return;
    const choice = document.getElementById("aufz-fahrzeug");
    const option = choice && choice.selectedOptions && choice.selectedOptions[0];
    const name = option && choice.value ? String(option.textContent || "").trim() : "";
    try {
      const response = p.ready({ vehicle: name });
      if (response && response.catch) response.catch(() => {});
    } catch (e) { /* kein Plugin, kein CarPlay */ }
  }

  async function carplayAktion(action) {
    const p = carplayPlugin();
    let ok = false, text = "";
    try {
      if (action === "starten") {
        if (K.state.sessionId) {
          ok = true; text = "Die Aufzeichnung läuft schon.";
        } else if (document.getElementById("aufz-start")
                   && document.getElementById("aufz-start").disabled) {
          ok = false; text = "Ein Start läuft gerade.";
        } else {
          ok = await startRecording();
          text = ok ? "" : (lastStartError || "Der Start hat nicht geklappt.");
        }
      } else if (action === "beenden") {
        if (!K.state.sessionId) {
          ok = true; text = "Es läuft keine Aufzeichnung.";
        } else {
          await window.joltLive.finish();
          ok = !K.state.sessionId;
          text = ok ? "" : "Das Beenden hat nicht geklappt.";
        }
      } else {
        return;
      }
    } catch (failure) {
      ok = false;
      text = (failure && failure.message) || "Unbekannter Fehler.";
    }
    if (p) {
      try {
        const response = p.aktionErgebnis({ action, ok, text });
        if (response && response.catch) response.catch(() => {});
      } catch (e) { /* CarPlay zeigt dann eben nichts */ }
    }
    return ok;
  }

  function carplayConnect() {
    const p = carplayPlugin();
    if (!p || typeof p.addListener !== "function") return;
    try {
      const h = p.addListener("carplayAktion", (e) => carplayAktion(e && e.action));
      if (h && h.catch) h.catch(() => {});
    } catch (e) { /* ohne Plugin kein CarPlay */ }
    reportVehicle();
  }

  /* Beim Öffnen sagen, was dieser Browser kann - bevor jemand tippt und
   * sich wundert, dass kein Geräte-Dialog kommt. */
  function dongleHint() {
    const el = document.getElementById("aufz-dongle-hinweis");
    if (!el) return;
    el.innerHTML = (window.joltObd && window.joltObd.obtainable())
      ? "Dieser Browser kann Bluetooth – beim Starten wird versucht, den "
        + "OBD2-Dongle zu verbinden. Klappt es nicht, läuft die Aufzeichnung "
        + "trotzdem, dann mit dem Ladestand von Hand."
      : "Dieser Browser kann kein Bluetooth, der Ladestand kommt also von "
        + "Hand. Mit Dongle: dieselbe Adresse in <strong>Bluefy</strong> "
        + "öffnen, dann geht es automatisch.";
  }

  function set_up() {
    K.at("aufz-start", "click", startRecording);
    carplayConnect();
    dongleHint();
    const holder = document.getElementById("fahrten-liste");
    if (!holder) return;
    // Ein Zuhörer am Halter statt einer je Zeile: Die Liste wird nach jedem
    // Löschen neu gebaut, einzeln gebundene Zuhörer wären dann tot.
    holder.addEventListener("click", (event) => {
      const uphill = event.target.closest("[data-oeffnen]");
      if (uphill) { open_it(uphill.dataset.oeffnen); return; }
      const path = event.target.closest("[data-loeschen]");
      if (path) {
        const row = path.closest("tr");
        const title = row ? row.querySelector(".titel") : null;
        remove(path.dataset.loeschen,
                 title ? title.textContent.trim() : "Diese Fahrt");
      }
    });
  }

  // Beim Wechsel in die Ansicht laden, nicht beim Start: Wer nie auf den
  // Reiter tippt, soll die Liste auch nicht bezahlen.
  //
  // Zwischengespeichert wird nur so lange, wie sich nichts geändert hat.
  // `geladen` wurde ursprünglich nirgends zurückgesetzt - die Liste war nach
  // dem ersten Öffnen eingefroren, und eine frisch geplante oder gerade
  // beendete Fahrt tauchte erst nach einem Neuladen der Seite auf. Wer eine
  // Fahrt anlegt, setzt jetzt `K.zustand.fahrtenVeraltet`; hier wird die
  // Marke gelesen und wieder gelöscht.
  function show() {
    if (!charged || K.state.tripsStale) {
      K.state.tripsStale = false;
      load();
    }
  }

  return { set_up, load, show, startRecording,
           carplayAktion, reportVehicle };
})();
