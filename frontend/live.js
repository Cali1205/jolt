/* Die Live-Ansicht: Ist gegen Soll, während gefahren wird.
 *
 * Zwei Wege herein: der eigene Standort des Telefons (GPS), oder der
 * Simulator im Server. Der Ladestand ist in dieser Stufe noch nicht aus dem
 * Auto zu haben - er kommt aus der Simulation oder wird fortgeschrieben.
 * Wenn der OBD2-Logger anschliesst, ändert sich an dieser Ansicht nichts,
 * nur die Quelle der Messpunkte.
 */
window.joltLive = (function () {
  "use strict";

  const K = window.jolt;
  let steckdose = null;       // WebSocket
  let plan = null;            // der aktuell gültige Ladeplan
  let wache = null;           // watchPosition-Kennung
  let letzteMeldung = 0;      // Zeitpunkt der letzten Positionsmeldung
  let dongle = false;         // liest der OBD2-Dongle mit?
  let runde = 0;

  // Wie oft die Position gemeldet wird. Die Nachführung mittelt über 25 km
  // und braucht mindestens 5 km, bevor sie überhaupt etwas sagt - alle
  // dreissig Sekunden ist bei Landstrassentempo rund ein halber Kilometer und
  // damit dicht genug. Häufiger kostet Akku und Mobilfunk, ohne etwas zu sagen.
  const MELDEABSTAND_MS = 30000;

  function verbindungAnzeigen(text, farbe) {
    const el = document.getElementById("live-verbindung");
    if (!el) return;
    el.textContent = text;
    el.style.color = farbe || "";
  }

  async function starten() {
    const fahrt = K.zustand.fahrt;
    if (!fahrt) { K.melden("Erst eine Route rechnen.", "fehler"); return; }
    try {
      // Mit denselben Filtern wie in der Planen-Ansicht: Ein Ladeplan, der
      // unterwegs plötzlich andere Säulen zulässt als beim Planen, wäre
      // nicht mehr nachvollziehbar.
      // Auch der Aufwand je Halt geht mit: Ein Plan, der unterwegs plötzlich
      // nach einem anderen Massstab umgeplant wird als beim Losfahren, wäre
      // nicht mehr nachvollziehbar.
      const haltekosten = document.getElementById("haltekosten");
      const antwort = await K.api(`/api/live/start/${fahrt.fahrt_id}`
        + `?min_kw=${document.getElementById("min-kw").value}`
        + `&radius_km=${document.getElementById("radius").value}`
        + (haltekosten ? `&stopp_fixkosten_min=${haltekosten.value}` : ""),
        { method: "POST" });
      K.zustand.sitzungId = antwort.sitzung_id;
      document.getElementById("live-leer").hidden = true;
      document.getElementById("live-inhalt").hidden = false;
      plan = antwort.plan || null;
      planZeichnen();
      // Beim Losfahren ist der Startladestand der beste bekannte Wert - besser
      // jedenfalls als eine feste Zahl, die mit diesem Auto nichts zu tun hat.
      socFeldVorbelegen(fahrt.start_soc);
      verbinden(antwort.sitzung_id);
      positionVerfolgen();
      window.joltApp.ansichtZeigen("live");
      // Einmal beim Start fragen, wo die Frage etwas bedeutet - und nicht
      // beim ersten geänderten Plan, wo sie im Weg steht.
      benachrichtigungenEinrichten();
      K.melden("Live-Fahrt läuft. Über „Simulation starten“ lässt sie sich "
        + "ohne Auto durchspielen.", "hinweis");
    } catch (fehler) {
      K.melden("Live: " + fehler.message, "fehler");
    }
  }

  function verbinden(sitzungId) {
    if (steckdose) { try { steckdose.close(); } catch (e) {} }
    const schema = location.protocol === "https:" ? "wss" : "ws";
    steckdose = new WebSocket(`${schema}://${location.host}/api/live/${sitzungId}/ws`);

    steckdose.onopen = () => verbindungAnzeigen("verbunden", "#57c98a");
    steckdose.onclose = () => verbindungAnzeigen("getrennt", "#8a97a5");
    steckdose.onerror = () => verbindungAnzeigen("gestört", "#e2596a");
    steckdose.onmessage = (nachricht) => {
      let daten;
      try { daten = JSON.parse(nachricht.data); } catch (e) { return; }
      if (daten.typ === "ende") {
        verbindungAnzeigen("Fahrt beendet", "#8a97a5");
        return;
      }
      zustandAnzeigen(daten);
    };
  }

  function zustandAnzeigen(z) {
    const fahrt = K.zustand.fahrt;
    const reserve = fahrt ? fahrt.fahrzeug.reserve_soc : 10;

    // Ein neu gerechneter Plan kommt am Zustand mit. Nur wenn er sich
    // wirklich unterscheidet, wird darauf hingewiesen - ein Plan, der sich
    // alle dreissig Sekunden meldet, ist kein Plan.
    if (z.plan) {
      plan = z.plan;
      planZeichnen();
      if (z.plan_geaendert) aenderungMelden(z.aenderung);
    }

    // Der zuletzt bekannte Ladestand als Vorschlag fürs nächste Melden: Am
    // Ladepunkt ist der neue Wert höher, unterwegs niedriger - in beiden
    // Fällen ist der letzte Wert der kürzere Weg als eine feste Zahl.
    socFeldVorbelegen(z.ist_soc);

    const abweichungArt = z.abweichung_pp === null ? ""
      : (z.abweichung_pp <= -5 ? "schlecht"
        : (z.abweichung_pp <= -2 ? "warnung" : "gut"));
    const prognoseArt = z.prognose_soc_am_ziel === null ? ""
      : (z.prognose_soc_am_ziel < reserve ? "schlecht"
        : (z.prognose_soc_am_ziel < reserve + 10 ? "warnung" : "gut"));

    document.getElementById("live-werte").innerHTML = [
      // Ein gerechneter Ladestand ist keine Messung, und das muss man ihm
      // ansehen: Wer bei 12 % an der Säule steht, will wissen, ob die Zahl
      // aus dem Auto kam oder aus einem Modell, das seit hundert Kilometern
      // niemand nachgeprüft hat.
      K.wertKachel(z.soc_gemeldet === false ? "Ladestand gerechnet" : "Ladestand",
        K.zahl(z.ist_soc) + " %"),
      K.wertKachel("Nach Plan", K.zahl(z.soll_soc) + " %"),
      K.wertKachel("Abweichung",
        (z.abweichung_pp === null ? "–"
          : (z.abweichung_pp > 0 ? "+" : "") + K.zahl(z.abweichung_pp, 1) + " pp"),
        abweichungArt),
      K.wertKachel("Verbrauch", "×" + K.zahl(z.verbrauchsfaktor, 2),
        z.verbrauchsfaktor > 1.1 ? "warnung" : ""),
      // Die Ankunftszeit steht neben dem Verbrauch, weil sie das Zweite ist,
      // was sich unterwegs verschiebt - und die einzige Grösse, die ein Stau
      // bewegt, ohne den Verbrauch anzufassen.
      K.wertKachel("Ankunft",
        (z.ankunft_verschiebung_min === null ? "–"
          : (Math.abs(z.ankunft_verschiebung_min) < 1 ? "nach Plan"
            : (z.ankunft_verschiebung_min > 0 ? "+" : "–")
              + K.dauer(Math.abs(z.ankunft_verschiebung_min)))),
        (z.ankunft_verschiebung_min || 0) >= 10 ? "warnung" : ""),
      K.wertKachel("Noch", K.zahl(z.rest_km) + " km"),
      // Ein negativer Ladestand ist keine Aussage über den Akku, sondern
      // darüber, dass es ohne Nachladen nicht reicht. Genau das gehört dann
      // auch dort zu stehen - "-188 %" liest niemand als Antwort.
      //
      // Und die Kachel heisst "Ohne Laden", nicht "Am Ziel": Sie rechnet die
      // Reststrecke ohne jeden Ladestopp hoch. Solange darunter kein Plan
      // stand, war das dasselbe; jetzt stünde sonst "reicht nicht" direkt
      // über einem Ladeplan, der aufgeht.
      K.wertKachel("Ohne Laden",
        (z.prognose_soc_am_ziel === null ? "–"
          : (z.prognose_soc_am_ziel < 0 ? "reicht nicht"
            : K.zahl(z.prognose_soc_am_ziel) + " %")),
        prognoseArt),
    ].join("");

    const balken = document.getElementById("live-balken");
    balken.style.width = Math.max(0, Math.min(100, z.ist_soc)) + "%";
    balken.style.background = z.ist_soc <= reserve ? "#e2596a"
      : (z.ist_soc <= reserve + 10 ? "#e8804f" : "#57c98a");

    const hinweis = document.getElementById("live-hinweis");
    hinweis.textContent = z.grund || "im Plan";
    hinweis.style.color = z.neuplanung_noetig ? "#e8804f" : "";

    // Die Reserve-Marke wandert mit: Das ist die eigentliche Aussage der
    // Live-Funktion - nicht "du verbrauchst mehr", sondern "es reicht jetzt
    // nur noch bis dorthin".
    if (fahrt && window.joltKarte) {
      const marker = [{ lat: z_lat(z), lon: z_lon(z), typ: "auto", text: "hier" }];
      if (z.reserve_bei_km !== null && fahrt.profil) {
        const treffer = fahrt.profil.find((p) => p.km >= z.reserve_bei_km);
        if (treffer) {
          marker.push({ lat: treffer.lat, lon: treffer.lon, typ: "reserve",
                        text: "Reserve " + K.zahl(z.reserve_bei_km) + " km" });
        }
      }
      for (const stopp of (plan && plan.stopps) || []) {
        marker.push({ lat: stopp.lat, lon: stopp.lon, typ: "stopp",
                      text: K.dauer(stopp.ladezeit_minuten) });
      }
      window.joltKarte.markerSetzen(marker);
    }
  }

  /* ---------- Der Ladeplan unterwegs ---------- */

  function planZeichnen() {
    const liste = document.getElementById("live-plan");
    const stand = document.getElementById("live-plan-stand");
    if (!liste || !stand) return;

    if (!plan) {
      liste.innerHTML = '<li class="leer">Noch kein Ladeplan.</li>';
      stand.textContent = "";
      return;
    }
    stand.textContent = plan.stand_km ? "gerechnet ab km " + K.zahl(plan.stand_km)
                                      : "beim Losfahren gerechnet";

    if (!plan.machbar) {
      liste.innerHTML = `<li class="leer" style="color:#e2596a">${
        entschaerfen(plan.grund || "Kein Ladeplan möglich.")}</li>`;
      return;
    }
    if (!plan.stopps || !plan.stopps.length) {
      liste.innerHTML = '<li class="leer">Kein Ladestopp mehr nötig.</li>';
      return;
    }

    liste.innerHTML = "";
    plan.stopps.forEach((s, i) => {
      const eintrag = document.createElement("li");
      eintrag.innerHTML = `
        <div class="haupt">
          <div class="titel">${i + 1}. ${entschaerfen(s.name || s.betreiber
            || "Ladepunkt")}</div>
          <div class="unter">km ${K.zahl(s.km_auf_route)} ·
            ${K.zahl(s.ankunft_soc)} % → ${K.zahl(s.abfahrt_soc)} % ·
            ${K.zahl(s.max_kw)} kW · ${s.anzahl_punkte} Ladepunkte</div>
        </div>
        <div class="kw">${K.dauer(s.ladezeit_minuten)}</div>`;
      liste.appendChild(eintrag);
    });
  }

  /* Eine Änderung am Plan ist der einzige Anlass, jemanden am Steuer zu
   * stören - deshalb hier und sonst nirgends eine Benachrichtigung. */
  function aenderungMelden(text) {
    const kasten = document.getElementById("live-aenderung");
    if (kasten) {
      kasten.textContent = text || "Der Ladeplan hat sich geändert.";
      kasten.hidden = false;
    }
    benachrichtigen(text || "Der Ladeplan hat sich geändert.");
  }

  function benachrichtigen(text) {
    // Ohne erteilte Erlaubnis wird nicht gefragt und nicht benachrichtigt:
    // Wer die Ansicht offen hat, sieht die Meldung ohnehin. Gefragt wird
    // einmal beim Start der Fahrt, wo die Frage auch etwas bedeutet.
    try {
      if (!("Notification" in window) || Notification.permission !== "granted") {
        return;
      }
      new Notification("jolt – Ladeplan geändert", { body: text, tag: "jolt-plan" });
    } catch (e) { /* je nach Browser und Kontext nicht erlaubt - dann eben nicht */ }
  }

  /* ---------- Benachrichtigungen aufs Telefon ---------- */

  /* Beim Start der Fahrt einmal fragen und das Gerät anmelden.
   *
   * Der Weg über den Push-Dienst ist der einzige, der ein Telefon mit dunklem
   * Bildschirm erreicht: Die WebSocket-Verbindung schläft dann mit. Deshalb
   * hier ein echtes Abo und nicht nur die Erlaubnis für die Notification-API.
   *
   * Scheitert irgendein Schritt, läuft die Fahrt trotzdem - dann eben nur mit
   * der Meldung in der offenen Ansicht. */
  async function benachrichtigungenEinrichten() {
    try {
      if (!("Notification" in window) || !("PushManager" in window)) return;

      const schluessel = await K.api("/api/push/schluessel");
      if (!schluessel.eingerichtet) return;   // kein VAPID-Schlüssel am Server

      if (Notification.permission === "default") {
        await Notification.requestPermission();
      }
      if (Notification.permission !== "granted") return;

      const registrierung = K.zustand.serviceWorker
        || (navigator.serviceWorker && await navigator.serviceWorker.ready);
      if (!registrierung || !registrierung.pushManager) return;

      // Ein bestehendes Abo weiterverwenden. Ein neues anzulegen gäbe
      // denselben Endpunkt zurück, kostet aber einen Umweg.
      let abo = await registrierung.pushManager.getSubscription();
      if (!abo) {
        abo = await registrierung.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: schluesselAlsBytes(schluessel.schluessel),
        });
      }

      const daten = abo.toJSON();
      await K.api("/api/push/abo", { method: "POST", body: {
        endpoint: daten.endpoint,
        p256dh: daten.keys.p256dh,
        auth: daten.keys.auth,
        geraet: navigator.userAgent.slice(0, 120),
      }});
    } catch (fehler) {
      // Bewusst nur ins Log: Wer gerade losfährt, will keine Fehlermeldung
      // über eine Nebenfunktion lesen.
      if (window.console) console.warn("Benachrichtigungen:", fehler.message);
    }
  }

  /* Der öffentliche Schlüssel kommt als base64url und muss als Uint8Array
   * übergeben werden - der Browser nimmt die Zeichenkette nicht an. */
  function schluesselAlsBytes(text) {
    const gefuellt = (text + "=".repeat((4 - text.length % 4) % 4))
      .replace(/-/g, "+").replace(/_/g, "/");
    const roh = atob(gefuellt);
    const bytes = new Uint8Array(roh.length);
    for (let i = 0; i < roh.length; i++) bytes[i] = roh.charCodeAt(i);
    return bytes;
  }

  /* Die Namen kommen aus fremden Datenquellen und landen in innerHTML. */
  function entschaerfen(text) {
    const hilfe = document.createElement("div");
    hilfe.textContent = text || "";
    return hilfe.innerHTML;
  }

  /* Der Zustand trägt keine Koordinate, aber den Kilometerstand - und über
   * das Profil hängt an jedem Kilometerstand eine Position. */
  function z_lat(z) { return punktBeiKm(z.km_auf_route).lat; }
  function z_lon(z) { return punktBeiKm(z.km_auf_route).lon; }

  function punktBeiKm(km) {
    const profil = (K.zustand.fahrt || {}).profil || [];
    return profil.find((p) => p.km >= km) || profil[profil.length - 1]
      || { lat: 0, lon: 0 };
  }

  async function simulieren() {
    if (!K.zustand.sitzungId) { K.melden("Keine Live-Fahrt.", "fehler"); return; }
    const mehr = Number(document.getElementById("mehrverbrauch").value) / 100;
    const stau = Number(document.getElementById("stau").value) / 100;
    try {
      await K.api(`/api/live/${K.zustand.sitzungId}/simulieren`
        + `?mehrverbrauch=${mehr}&takt_s=0.3&zeitfaktor=${stau}`,
        { method: "POST" });
      K.melden(`Simulation läuft mit ${Math.round(mehr * 100)} % Verbrauch `
        + `und ${Math.round(stau * 100)} % Fahrzeit.`, "hinweis");
    } catch (fehler) {
      K.melden("Simulation: " + fehler.message, "fehler");
    }
  }

  /* ---------- Ladestand von Hand ---------- */

  /* ---------- Position laufend melden ---------- */

  /* Ohne diese Meldungen bekommt jolt zwischen zwei eingetippten Ladeständen
   * überhaupt nichts - keine Position, keine Zeit. Dann steht der Zeitfaktor
   * die ganze Fahrt auf 1,0, die Ankunftsprognose auf dem Stand der Abfahrt,
   * und ein Umweg fällt erst auf, wenn jemand von sich aus etwas eintippt.
   *
   * Der Ladestand wird bewusst *nicht* mitgeschickt: Er ist unbekannt, und
   * den letzten bekannten Wert erneut zu senden hiesse, eine Messung zu
   * erfinden - der Verbrauchsfaktor läse daraus, das Auto habe seither nichts
   * verbraucht. Was zwischen zwei Meldungen gilt, rechnet der Server aus dem
   * Energieprofil hoch. */
  function positionVerfolgen() {
    if (!navigator.geolocation || wache !== null) return;
    wache = navigator.geolocation.watchPosition(
      (pos) => {
        const jetzt = Date.now();
        // Nicht jede GPS-Aktualisierung melden: Das Gerät liefert im
        // Sekundentakt, und die Nachführung mittelt ohnehin über Kilometer.
        // Häufiger zu senden kostet Akku und Mobilfunk, ohne etwas zu sagen.
        if (jetzt - letzteMeldung < MELDEABSTAND_MS) return;
        letzteMeldung = jetzt;
        positionMelden(pos.coords);
      },
      // Ein GPS-Fehler unterwegs ist kein Grund, den Nutzer zu behelligen -
      // in einem Tunnel ist er der Normalfall, und die nächste Messung kommt.
      () => {},
      { enableHighAccuracy: true, maximumAge: 15000, timeout: 30000 });
  }

  function positionAufgeben() {
    if (wache === null) return;
    try { navigator.geolocation.clearWatch(wache); } catch (e) {}
    wache = null;
  }

  /* Wenn ein Dongle mitliest, wandert der Ladestand von hier aus mit.
   *
   * Bewusst an dieselbe Meldung gehängt und nicht als zweite Schleife: So
   * gehören Position und Ladestand zu **einem** Messpunkt und derselben
   * Sekunde. Zwei Schleifen ergäben Punkte, die sich abwechseln - einer mit
   * Position, einer mit Ladestand -, und die Nachführung müsste beides
   * wieder zusammensuchen. */
  function dongleNutzen() {
    dongle = true;
    if (window.joltObd) {
      window.joltObd.einrichten(
        (t) => console.log("[obd]", t),
        // Ein Abriss im Tunnel ist kein Grund aufzuhören, solange die Fahrt
        // läuft: Der Baustein baut selbst wieder auf.
        () => { if (K.zustand.sitzungId) window.joltObd.wiederverbinden(
          1, () => !!K.zustand.sitzungId); });
    }
  }

  async function positionMelden(coords) {
    if (!K.zustand.sitzungId) return;
    const nutzlast = {
      lat: coords.latitude, lon: coords.longitude,
      tempo_kmh: coords.speed === null ? null : coords.speed * 3.6,
    };

    if (dongle && window.joltObd && window.joltObd.verbunden()) {
      try {
        const roh = await window.joltObd.satzLesen(runde++);
        if (typeof roh.hoehe_m !== "number" && typeof coords.altitude === "number") {
          roh.hoehe_m = Math.round(coords.altitude);
        }
        const wert = window.joltObd.socAusRoh(roh.soc_roh);
        nutzlast.soc = Math.round(wert.hmi * 10) / 10;
        nutzlast.rohwerte = roh;
        // Was das Auto selbst misst, schlägt jede Vorhersage.
        if (typeof roh.tempo_kmh === "number") nutzlast.tempo_kmh = roh.tempo_kmh;
        if (typeof roh.aussentemp_c === "number") {
          nutzlast.aussentemp_c = roh.aussentemp_c;
        }
      } catch (fehler) {
        // Eine Runde ohne Ladestand ist immer noch eine Positionsmeldung -
        // und die trägt Zeitfaktor und Ankunftsprognose weiter.
        console.log("[obd] Runde übersprungen:", fehler);
      }
    }

    try {
      const zustand = await K.api(`/api/live/${K.zustand.sitzungId}/punkt`,
        { method: "POST", body: nutzlast });
      zustandAnzeigen(zustand);
    } catch (fehler) {
      // Stillschweigend: Ein Funkloch ist unterwegs normal, und eine
      // Fehlermeldung je verlorener Positionsmeldung wäre eine Meldung alle
      // dreissig Sekunden.
    }
  }

  function standortHolen() {
    return new Promise((erfuellen, ablehnen) => {
      if (!navigator.geolocation) {
        ablehnen(new Error("Dieses Gerät liefert keinen Standort."));
        return;
      }
      navigator.geolocation.getCurrentPosition(
        (pos) => erfuellen({ lat: pos.coords.latitude, lon: pos.coords.longitude,
                             tempo_kmh: pos.coords.speed === null ? null
                               : pos.coords.speed * 3.6 }),
        // Ohne Standort ist der Ladestand allein wertlos: Erst die Position
        // sagt, mit welchem Sollwert er zu vergleichen ist.
        (fehler) => ablehnen(new Error("Standort nicht verfügbar ("
          + fehler.message + "). Über HTTPS oder localhost erlaubt der "
          + "Browser den Zugriff.")),
        { enableHighAccuracy: true, timeout: 10000, maximumAge: 5000 });
    });
  }

  /* Den zuletzt bekannten Ladestand ins Feld schreiben - aber nie, während
   * jemand darin tippt. Am Ladepunkt wird der Wert eingetippt, und ein Feld,
   * das sich beim Eintippen unter den Fingern ändert, weil gerade eine
   * Nachricht über den WebSocket kam, ist schlimmer als ein leeres. */
  function socFeldVorbelegen(wert) {
    const feld = document.getElementById("ist-soc");
    if (!feld || document.activeElement === feld) return;
    if (wert === null || wert === undefined || Number.isNaN(wert)) return;
    feld.value = Math.round(wert);
  }

  async function socMelden() {
    if (!K.zustand.sitzungId) { K.melden("Keine Live-Fahrt.", "fehler"); return; }
    const knopf = document.getElementById("soc-melden");
    const feld = document.getElementById("ist-soc");
    const soc = Number(feld.value);
    if (!feld.value || !(soc >= 0 && soc <= 100)) {
      K.melden("Ladestand zwischen 0 und 100 % angeben.", "fehler");
      return;
    }

    knopf.disabled = true;
    // Die Tastatur weg, sonst verdeckt sie auf dem Telefon genau die Werte,
    // wegen derer man den Ladestand gerade gemeldet hat.
    feld.blur();
    try {
      const ort = await standortHolen();
      const zustand = await K.api(`/api/live/${K.zustand.sitzungId}/punkt`,
        { method: "POST", body: { lat: ort.lat, lon: ort.lon, soc: soc,
                                  tempo_kmh: ort.tempo_kmh } });
      zustandAnzeigen(zustand);
      // Die Abweichung ist der Grund, warum das Eintippen sich lohnt - also
      // gehört sie unmittelbar danach als Satz auf den Schirm und nicht nur
      // als Kachel unter fünf anderen.
      const erklaerung = document.getElementById("soc-erklaerung");
      if (erklaerung) {
        erklaerung.textContent = zustand.abweichung_pp === null
          || zustand.abweichung_pp === undefined
          ? "Aufgenommen."
          : (Math.abs(zustand.abweichung_pp) < 0.5
            ? "Aufgenommen – genau im Plan."
            : `Aufgenommen – ${K.zahl(Math.abs(zustand.abweichung_pp), 1)} `
              + `Prozentpunkte ${zustand.abweichung_pp < 0 ? "unter" : "über"} Plan.`);
      }
    } catch (fehler) {
      K.melden(fehler.message, "fehler");
    } finally {
      knopf.disabled = false;
    }
  }

  async function beenden() {
    if (!K.zustand.sitzungId) return;
    let ergebnis = null;
    try {
      ergebnis = await K.api(`/api/live/${K.zustand.sitzungId}/ende`,
                             { method: "POST" });
    } catch (fehler) { /* eine bereits beendete Fahrt ist kein Problem */ }
    positionAufgeben();
    dongle = false;
    if (steckdose) { try { steckdose.close(); } catch (e) {} }
    K.zustand.sitzungId = null;
    plan = null;
    const kasten = document.getElementById("live-aenderung");
    if (kasten) kasten.hidden = true;
    document.getElementById("live-inhalt").hidden = true;
    document.getElementById("live-leer").hidden = false;
    gelerntesMelden(ergebnis);
  }

  /* Was jolt aus der Fahrt gelernt hat - und warum nicht, wenn nicht.
   *
   * Das Backend schreibt den Korrekturfaktor des Fahrzeugs bei jedem
   * Fahrtende fort und meldet das Ergebnis zurück; gelesen hat es bisher
   * niemand. Für den Zweck, um den es dabei geht - kurze bekannte Strecken
   * fahren und daraus den echten Verbrauch lernen -, ist das der einzige
   * Rückkanal. Ohne ihn fährt man dieselbe Strecke dreimal und weiss
   * hinterher nicht, ob überhaupt etwas angekommen ist.
   *
   * Auch das Ausbleiben wird gemeldet: `gelernt: null` heisst "zu kurz oder
   * unplausibel". Eine Fahrt, die stillschweigend nichts beiträgt, sieht
   * sonst aus wie eine, die bestätigt hat. */
  function gelerntesMelden(ergebnis) {
    if (!ergebnis) {
      K.melden("Live-Fahrt beendet.", "hinweis");
      return;
    }
    const g = ergebnis.gelernt;
    if (!g && ergebnis.nicht_gelernt) {
      // Der Zuschlag für Träger oder Box ist kein Fehler, sondern der Grund,
      // warum diese Fahrt bewusst nicht in den Fahrzeugfaktor eingeht.
      K.melden("Fahrt beendet. " + ergebnis.nicht_gelernt
        + " Die Aufzeichnung bleibt erhalten.", "hinweis");
      return;
    }
    if (!g) {
      K.melden("Fahrt beendet. Für die Kalibrierung war sie nicht verwertbar "
        + "– unter 30 km, oder der gemessene Verbrauch lag ausserhalb des "
        + "Plausiblen.", "hinweis");
      return;
    }
    const richtung = g.nachher > g.vorher ? "mehr" : "weniger";
    K.melden(`Gelernt: Diese Fahrt brauchte ${K.zahl(g.rohfaktor, 2)}× so viel `
      + `wie gerechnet. Der Korrekturfaktor des Fahrzeugs geht von `
      + `${K.zahl(g.vorher, 3)} auf ${K.zahl(g.nachher, 3)} – künftige `
      + `Planungen rechnen also ${richtung}.`, "hinweis");
  }

  function einrichten() {
    K.reglerKoppeln("mehrverbrauch", "mehrverbrauch-wert");
    K.reglerKoppeln("stau", "stau-wert");
    K.an("live-starten", "click", starten);
    K.an("simulieren", "click", simulieren);
    K.an("live-beenden", "click", beenden);
    K.an("soc-melden", "click", socMelden);
    // Auf dem Telefon ist die Eingabetaste der kürzere Weg als das Zielen auf
    // einen Knopf - `enterkeyhint="send"` beschriftet sie passend.
    K.an("ist-soc", "keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); socMelden(); }
    });
  }

  return { einrichten, starten, beenden, verbinden, positionVerfolgen,
           dongleNutzen };
})();
