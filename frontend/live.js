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
  let socket = null;       // WebSocket
  let plan = null;            // der aktuell gültige Ladeplan
  let awake = null;           // watchPosition-Kennung, oder "nativ"
  let nativeAwakeId = null;   // Kennung des Hintergrund-Standorts der App
  let nativeRun = 0;         // zählt Starts, damit ein später Rückruf weiss, ob er noch gilt
  let locationErrorReported = false;
  let latestReport = 0;      // Zeitpunkt der letzten Positionsmeldung
  let dongle = false;         // liest der OBD2-Dongle mit?
  let lap = 0;
  // Die gefahrene Spur einer Aufzeichnung, [[lon, lat], ...].
  let track = [];
  // Die entlang dieser Spur zurückgelegte Strecke. Sie wird **fortlaufend**
  // mitgeführt und an jedem Verlaufspunkt festgehalten, statt sie beim
  // Zeichnen aus der Spur nachzurechnen: Spur und Verlauf wachsen unter
  // verschiedenen Bedingungen (die Spur bei jeder neuen Position, der
  // Verlauf bei jedem neuen Ladestand), also gehört `spur[i]` nicht zu
  // `verlauf[i]`. Beim Aufzeichnen mit Dongle ist der Unterschied gewaltig -
  // der Ladestand ändert sich alle paar Minuten, die Position im Sekundentakt.
  let drivenKm = 0;
  // Der gemessene Verlauf: [{km, soc, gemeldet}, ...] für die Kurve.
  let history = [];
  // Die zuletzt aus dem Auto gelesenen Werte. Der Server schickt sie nicht
  // zurück - er speichert sie nur -, also hält die Anzeige sie selbst.
  let latestRawValues = null;
  let latestRawValuesTime = 0;   // wann der letzte vollständige Satz ankam
  /* Der letzte bekannte Wert je Messgrösse, mit seinem Zeitpunkt.
   *
   * Die Tabelle zeigte nur, was in **dieser** Runde ankam - und wurde damit
   * löchrig: Der DC/DC-Strom wird nur jede zehnte Runde gelesen und stand
   * neun von zehn Runden leer, und ein Wert, der einmal ausfällt,
   * verschwand mitsamt seiner Zeile.
   *
   * Ein alter Wert ist aber fast immer nützlicher als gar keiner. Der
   * Kilometerstand von vor dreissig Sekunden stimmt noch; die Innentemperatur
   * von vor zwei Minuten auch. Was fehlt, ist nicht der Wert, sondern die
   * Angabe, wie alt er ist - und die steht jetzt daneben. */
  let valuesAsOf = {};          // name -> {wert, zeit}
  // Anfang der Fahrt für den laufenden Verbrauch: {soc, km} aus der ersten
  // Runde, in der beides zugleich vorlag.
  let consumptionStart = null;
  // Ob wegen der Stille schon gewarnt wurde. Einmal genügt: Eine Meldung,
  // die alle zwölf Sekunden kommt, schaltet man ab.
  let quietReported = false;
  /* Messpunkte für den Verbrauchsplot: [{zeit, kw, km, soc}, ...].
   *
   * Roh gesammelt und erst beim Zeichnen zu Abschnitten verrechnet - so
   * lässt sich die Abschnittsbreite ändern, ohne die Messung zu verlieren. */
  let consumption_track = [];
  let neverCome = new Set();  // Kennungen, die dieses Auto nicht beantwortet
  /* Die Leistung der Nebenverbraucher, wenn das Steuergerät sie nicht sagt.
   *
   * Es gibt sie als fertige Zahl (DID 0364, "HV auxiliary consumer power"),
   * und die ist jeder Rechnung überlegen. Antwortet dieses Steuergerät
   * nicht, bleibt die Näherung: Im Fahren enthält die Packleistung Antrieb
   * **und** Nebenverbraucher, und den Antrieb zu modellieren brauchte die
   * Steigung, die während einer Aufzeichnung niemand kennt. Steht das Auto
   * aber und lädt nicht, dann ist die Packleistung die der Nebenverbraucher.
   *
   * Weil dieser Rückfall nur so lange gilt, wie sich an der Heizung nichts
   * ändert, wird er mit seinem Alter angezeigt - anders als der gemessene
   * Wert, der immer von jetzt ist. */
  let aux_load = null;   // {kw, zeit}

  /* Wie oft die Position gemeldet wird.
   *
   * Hier standen dreissig Sekunden, mit der Begründung, die Nachführung
   * mittle ohnehin über Kilometer. Für die **Nachführung** stimmt das; für
   * die **Aufzeichnung** nicht, und die war damals noch nicht gebaut. Dort
   * ist jeder Messpunkt ein Stützpunkt der Strecke, die hinterher aus ihnen
   * entsteht - bei Landstrassentempo lagen vierhundert Meter dazwischen, und
   * die Luftlinie schneidet jede Kurve ab. Die erste echte Testfahrt hat
   * genau das gezeigt.
   *
   * Zwölf Sekunden sind rund hundertsechzig Meter und bringen die Kurven
   * zurück, ohne dass Akku und Mobilfunk spürbar mehr kosten: Eine Meldung
   * ist ein kleines JSON, und das GPS läuft ohnehin.
   *
   * Für die Länge der Strecke ist der Kilometerstand des Fahrzeugs die
   * bessere Quelle (siehe `live/aufzeichnung.odometer_faktor`) - dichtere
   * Punkte braucht es trotzdem, denn sie tragen den **Verlauf**: Höhenprofil,
   * Tempo je Teilstück, und die Karte. */
  const REPORT_INTERVAL_MS = 12000;

  function showConnection(text, colour) {
    const el = document.getElementById("live-verbindung");
    if (!el) return;
    el.textContent = text;
    el.style.color = colour || "";
  }

  async function launch() {
    const trip = K.state.trip;
    if (!trip) { K.report("Erst eine Route rechnen.", "fehler"); return; }
    // Zuerst der Dongle, dann die Sitzung - siehe dongleAnbieten(). Wer
    // keinen auswählt, fährt ohne: Die Fahrt startet in jedem Fall.
    const withDongle = await dongleOffer();
    try {
      // Mit denselben Filtern wie in der Planen-Ansicht: Ein Ladeplan, der
      // unterwegs plötzlich andere Säulen zulässt als beim Planen, wäre
      // nicht mehr nachvollziehbar.
      // Auch der Aufwand je Halt geht mit: Ein Plan, der unterwegs plötzlich
      // nach einem anderen Massstab umgeplant wird als beim Losfahren, wäre
      // nicht mehr nachvollziehbar.
      const holding_cost = document.getElementById("haltekosten");
      const response = await K.api(`/api/live/start/${trip.trip_id}`
        + `?min_kw=${document.getElementById("min-kw").value}`
        + `&radius_km=${document.getElementById("radius").value}`
        + (holding_cost ? `&stop_fixed_cost_min=${holding_cost.value}` : "")
        + (function () {
            const p = document.getElementById("ladepark");
            return p ? `&charge_park_bonus_min=${p.value}` : "";
          })(),
        { method: "POST" });
      K.state.sessionId = response.session_id;
      K.sessionRemember(response.session_id);
      // Wer die Fahrt startet, sitzt im Auto: Es darf gelesen werden, bis
      // das Telefon sagt, dass das Auto steht.
      drivingStateStart("faehrt");
      document.getElementById("live-leer").hidden = true;
      document.getElementById("live-inhalt").hidden = false;
      plan = response.plan || null;
      drawPlan();
      // Beim Losfahren ist der Startladestand der beste bekannte Wert - besser
      // jedenfalls als eine feste Zahl, die mit diesem Auto nichts zu tun hat.
      socFieldPrefill(trip.start_soc);
      link(response.session_id);
      positionTrace();
      window.joltApp.showView("live");
      // Einmal beim Start fragen, wo die Frage etwas bedeutet - und nicht
      // beim ersten geänderten Plan, wo sie im Weg steht.
      notificationsSetUp();
      showDongle();
      K.report(withDongle
        ? "Live-Fahrt läuft – Ladestand und Zähler kommen aus dem Auto."
        : "Live-Fahrt läuft ohne Dongle – der Ladestand kommt von Hand. "
          + "Über „Dongle verbinden“ geht es jederzeit nachträglich.",
        "hinweis");
    } catch (failure) {
      K.report("Live: " + failure.message, "fehler");
    }
  }

  /* Die Verbindung nach einem Abriss wieder aufbauen.
   *
   * Ein WebSocket überlebt keinen Tunnel und keinen Wechsel von WLAN auf
   * Mobilfunk. Ohne Wiederaufbau blieb die Live-Ansicht danach für den Rest
   * der Fahrt stehen: Die Messpunkte gingen weiter hinaus (der POST ist ein
   * eigener Weg), aber zurück kam nichts mehr - also keine Abweichung, keine
   * Ankunftsprognose und vor allem keine Meldung über einen geänderten
   * Plan. Genau die Lage, in der man sie braucht.
   *
   * Wachsende Abstände wie beim Dongle: Ein Tunnel dauert Sekunden, ein
   * Funkloch auf dem Land Minuten. Beendet die Fahrt, hört es auf -
   * `K.zustand.sitzungId` ist die Bedingung, und `beenden()` löscht sie. */
  let reconnectClock = null;

  function reconnectAgain(sessionId, attempt) {
    if (reconnectClock) clearTimeout(reconnectClock);
    if (attempt > 8 || K.state.sessionId !== sessionId) return;
    const wait = Math.min(30000, 2000 * Math.pow(2, attempt - 1));
    showConnection(`getrennt – neuer Versuch in ${wait / 1000} s`,
                       "#e8804f");
    reconnectClock = setTimeout(() => {
      reconnectClock = null;
      if (K.state.sessionId === sessionId) link(sessionId, attempt);
    }, wait);
  }

  function link(sessionId, attempt = 1) {
    if (socket) {
      // Den alten Zuhörer abhängen, bevor geschlossen wird: Sonst löst
      // dieses Schliessen selbst einen Wiederaufbau aus.
      try { socket.onclose = null; socket.close(); } catch (e) {}
    }
    const schema = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${schema}://${location.host}/api/live/${sessionId}/ws`);

    // Ein Browser kann beim WebSocket keine Header setzen; der Token geht
    // deshalb als erste Nachricht. Erst die Antwort "bereit" heisst, dass der
    // Server ihn angenommen hat - vorher gilt die Verbindung nicht als
    // stehend, und die Wartezeit bleibt, wie sie ist.
    socket.onopen = () => {
      try { socket.send(JSON.stringify({ token: K.token() })); }
      catch (e) { /* onclose baut neu auf */ }
    };
    socket.onclose = () => {
      if (K.state.sessionId === sessionId) reconnectAgain(sessionId, attempt + 1);
      else showConnection("getrennt", "#8a97a5");
    };
    socket.onerror = () => showConnection("gestört", "#e2596a");
    socket.onmessage = (msg) => {
      let records;
      try { records = JSON.parse(msg.data); } catch (e) { return; }
      if (records.kind === "bereit") {
        showConnection("verbunden", "#57c98a");
        attempt = 0;   // eine stehende Verbindung setzt die Wartezeit zurück
        return;
      }
      if (records.kind === "ende") {
        showConnection("Fahrt beendet", "#8a97a5");
        return;
      }
      showState(records);
    };
  }

  function showState(z) {
    // Das Anzeigemodell für alles ausserhalb dieser Oberfläche (CarPlay,
    // Widget): wenige Zahlen, gedrosselt. Ein Fehler dort darf die Anzeige
    // hier nie mitreissen.
    try {
      if (window.joltDisplay) {
        window.joltDisplay.report(z, { track: consumption_track, vals: valuesAsOf,
                                       aux: aux_load, plan: z.plan || plan });
      }
    }
    catch (e) { console.log("[anzeige]", e && e.message); }
    const trip = K.state.trip;
    const reserve = trip ? trip.vehicle.reserve_soc : 10;

    // Ein neu gerechneter Plan kommt am Zustand mit. Nur wenn er sich
    // wirklich unterscheidet, wird darauf hingewiesen - ein Plan, der sich
    // alle dreissig Sekunden meldet, ist kein Plan.
    if (z.plan) {
      plan = z.plan;
      drawPlan();
      if (z.plan_changed) reportChange(z.change);
    }

    // Der zuletzt bekannte Ladestand als Vorschlag fürs nächste Melden: Am
    // Ladepunkt ist der neue Wert höher, unterwegs niedriger - in beiden
    // Fällen ist der letzte Wert der kürzere Weg als eine feste Zahl.
    socFieldPrefill(z.actual_soc);

    const deviationVariety = z.deviation_pp === null ? ""
      : (z.deviation_pp <= -5 ? "schlecht"
        : (z.deviation_pp <= -2 ? "warnung" : "gut"));
    const forecastVariety = z.forecast_soc_at_target === null ? ""
      : (z.forecast_soc_at_target < reserve ? "schlecht"
        : (z.forecast_soc_at_target < reserve + 10 ? "warnung" : "gut"));

    /* Die Antwort zuerst, und die Antwort ist nicht der Ladestand.
     *
     * Der Ladestand ist eine Eingabe - die Frage im Auto lautet "reicht
     * es?", und die beantwortet der Ankunftswert. Solange ein Ladeplan
     * steht, ist der nächste Stopp die nähere und damit dringlichere
     * Antwort; ohne Plan zählt das Ziel. */
    showResponse(z, reserve);

    /* Darunter nur das, was eine Entscheidung ändert. Ladestand und
     * Abweichung stehen bewusst hier und nicht oben: Sie sind Beleg, nicht
     * Antwort - man liest sie, wenn man der grossen Zahl nachgehen will. */
    document.getElementById("live-werte").innerHTML = [
      // Eine Nachkommastelle: Der Dongle liefert den Ladestand in Schritten
      // von 0,4 Prozentpunkten (ein Byte durch 2,5). Auf ganze Prozent
      // gerundet steht die Zahl minutenlang still, obwohl sie sich bewegt -
      // und gerade die Bewegung will man sehen.
      K.valueTile(z.soc_source === "zuletzt" ? "Ladestand (zuletzt gemessen)"
                   : (z.soc_reported === false ? "Ladestand (gerechnet)" : "Ladestand"),
        K.num(z.actual_soc, 1) + " %"),
      K.valueTile("Abweichung",
        (z.deviation_pp === null ? "–"
          : (z.deviation_pp > 0 ? "+" : "") + K.num(z.deviation_pp, 1) + " pp"),
        deviationVariety),
      // Die Ankunftszeit ist die zweite Grösse, die sich unterwegs
      // verschiebt - und die einzige, die ein Stau bewegt, ohne den
      // Verbrauch anzufassen.
      K.valueTile("Ankunft",
        (z.arrival_shift_min === null ? "–"
          : (Math.abs(z.arrival_shift_min) < 1 ? "nach Plan"
            : (z.arrival_shift_min > 0 ? "+" : "–")
              + K.duration(Math.abs(z.arrival_shift_min)))),
        (z.arrival_shift_min || 0) >= 10 ? "warnung" : ""),
      K.valueTile("Noch", K.num(z.remaining_km) + " km"),
    ].join("");

    /* Jeder Messpunkt kommt **zweimal** hier an: einmal als Antwort auf den
     * eigenen POST, einmal über den WebSocket, der ihn an alle Zuschauer
     * zurückspiegelt - und der eigene Browser ist einer davon. Ohne diese
     * Prüfung stünde jeder Punkt doppelt im Verlauf und in der Spur; über
     * eine Langstrecke wären das tausend Einträge zu viel. */
    // Position und Strecke **vor** dem Verlauf: Der Verlaufspunkt soll
    // wissen, wie weit gefahren wurde, als er entstand.
    const city = measurement_site(z);
    if (city) {
      const most_recent = track[track.length - 1];
      if (!most_recent || most_recent[0] !== city[0] || most_recent[1] !== city[1]) {
        if (most_recent) drivenKm += spacingKm(most_recent, city);
        track.push(city);
      }
    }

    // Den letzten Verbrauchspunkt mit der jetzt bekannten GPS-Strecke
    // versehen. Er entstand beim Auslesen des Dongles, also bevor die
    // Position durch war.
    const lastV = consumption_track[consumption_track.length - 1];
    if (lastV && lastV.gps === null) lastV.gps = drivenKm;

    const previous = history[history.length - 1];
    // Auch die gefahrene Strecke zählt beim Vergleich: Bei einer
    // Aufzeichnung ist `km_auf_route` für jeden Punkt null (es gibt noch
    // keine Route), und ohne diesen Teil galt jeder Punkt mit unverändertem
    // Ladestand als Dublette. Beim Aufzeichnen mit Dongle sind das fast
    // alle - der Ladestand ändert sich alle paar Minuten.
    const actualNew = z.actual_soc !== null && z.actual_soc !== undefined
      && !(previous && previous.km === (z.km_on_route || 0)
           && previous.driven_km === drivenKm
           && previous.soc === z.actual_soc);
    if (actualNew) {
      history.push({ km: z.km_on_route || 0, driven_km: drivenKm,
                     soc: z.actual_soc, reported: z.soc_reported !== false });
    }
    drawHistory();
    drawConsumption();
    autoRow(z);
    showDongle();

    const bar = document.getElementById("live-balken");
    bar.style.width = Math.max(0, Math.min(100, z.actual_soc)) + "%";
    bar.style.background = z.actual_soc <= reserve ? "#e2596a"
      : (z.actual_soc <= reserve + 10 ? "#e8804f" : "#57c98a");

    const hint = document.getElementById("live-hinweis");
    hint.textContent = z.reason || "im Plan";
    hint.style.color = z.replanning_required ? "#e8804f" : "";

    // Die Reserve-Marke wandert mit: Das ist die eigentliche Aussage der
    // Live-Funktion - nicht "du verbrauchst mehr", sondern "es reicht jetzt
    // nur noch bis dorthin".
    /* Die Karte auch **ohne** geplante Route bedienen.
     *
     * Hier stand `if (fahrt && ...)`, und `K.zustand.fahrt` ist nur gesetzt,
     * wenn vorher eine Route gerechnet wurde. Bei einer Aufzeichnung gibt es
     * keine - also wurde der ganze Block übersprungen und die Karte blieb
     * leer, obwohl die Position längst hereinkam. Die eigene Position hat
     * mit dem Vorhandensein einer Route nichts zu tun.
     */
    if (window.joltMap) {
      const here = city || [z_lon(z), z_lat(z)];
      const marker = [{ lat: here[1], lon: here[0], kind: "auto", text: "hier" }];
      // Bei einer Aufzeichnung ist die gefahrene Spur das, was es zu sehen
      // gibt: Sie wächst mit und zeigt, dass wirklich mitgeschrieben wird.
      // Gefüllt wird sie weiter oben, zusammen mit der Strecke.
      if (!trip) {
        window.joltMap.setRoute(track);
        // Die Karte folgt der Spur von selbst (map.js), bis jemand sie
        // anfasst - dann bleibt sie, wo sie ist. Frueher wurde nur der erste
        // Punkt zentriert, und die wachsende Strecke musste man von Hand
        // verfolgen.
      }
      if (trip && z.reserve_at_km !== null && trip.profile) {
        const hit = trip.profile.find((p) => p.km >= z.reserve_at_km);
        if (hit) {
          marker.push({ lat: hit.lat, lon: hit.lon, kind: "reserve",
                        text: "Reserve " + K.num(z.reserve_at_km) + " km" });
        }
      }
      for (const stop of (plan && plan.stops) || []) {
        marker.push({ lat: stop.lat, lon: stop.lon, kind: "stopp",
                      text: K.duration(stop.charge_time_minutes) });
      }
      window.joltMap.setMarker(marker);
    }
  }


  /* ---------- Die Antwort ---------- */

  function showResponse(z, reserve) {
    const box = document.getElementById("live-antwort");
    const num = document.getElementById("live-antwort-zahl");
    const text = document.getElementById("live-antwort-text");
    if (!box) return;

    const stop = z.next_stop;
    let val = null, wo = "", variety = "";
    if (stop && stop.expected_soc !== null && stop.expected_soc !== undefined) {
      val = stop.expected_soc;
      wo = `an ${stop.name || "nächster Stopp"} · km ${K.num(stop.km_on_route)}`;
    } else if (z.forecast_soc_at_target !== null) {
      val = z.forecast_soc_at_target;
      wo = "am Ziel, ohne Nachladen";
    }

    if (val === null) {
      num.textContent = K.num(z.actual_soc) + " %";
      text.textContent = "Ladestand – noch keine Prognose";
      box.className = "";
      return;
    }
    // Ein negativer Wert ist keine Aussage über den Akku, sondern darüber,
    // dass es so nicht reicht. Genau das gehört dann da zu stehen.
    if (val < 0) {
      num.textContent = "reicht nicht";
      variety = "schlecht";
    } else {
      num.textContent = K.num(val) + " %";
      variety = val < reserve ? "schlecht" : (val < reserve + 8 ? "warnung" : "gut");
    }
    text.textContent = wo;
    box.className = variety;
  }

  /* Luftlinie zwischen zwei [lon, lat] in Kilometern. Für eine gefahrene
   * Spur mit Punkten alle dreissig Sekunden ist der Unterschied zur
   * Strassenlänge vernachlässigbar - und für die Achse einer Kurve zählt
   * ohnehin nur, dass sie monoton wächst. */
  function spacingKm(a, b) {
    if (!a || !b) return 0;
    const R = 6371, r = Math.PI / 180;
    const dLat = (b[1] - a[1]) * r, dLon = (b[0] - a[0]) * r;
    const h = Math.sin(dLat / 2) ** 2
      + Math.cos(a[1] * r) * Math.cos(b[1] * r) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  /* ---------- Der Verlauf ---------- */

  /* Soll und Ist über die Strecke, in einem Bild.
   *
   * Das ist jolts These als Zeichnung: Ein Plan, der bei Abfahrt gerechnet
   * wurde, ist nach achtzig Kilometern falsch - und zwei Kurven, die
   * auseinanderlaufen, sagen das in einem Blick, während eine Kachel mit
   * "-6 pp" erst gelesen und eingeordnet werden will. Vor allem sagt die
   * Kurve, ob es besser oder schlechter wird; eine Momentaufnahme kann das
   * grundsätzlich nicht.
   *
   * Gezeichnet wird auch ohne Plan: Bei einer Aufzeichnung gibt es keine
   * Soll-Kurve, aber die gemessene ist dann erst recht das, was man sehen
   * will.
   */
  function drawHistory() {
    const canvas = document.getElementById("live-verlauf");
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const extent = canvas.clientWidth, elevation = canvas.clientHeight;
    if (!extent || !elevation) return;
    canvas.width = extent * dpr;
    canvas.height = elevation * dpr;
    const pen = canvas.getContext("2d");
    pen.setTransform(dpr, 0, 0, dpr, 0, 0);
    pen.clearRect(0, 0, extent, elevation);

    const trip = K.state.trip;
    const profile = (trip && trip.profile) || [];
    const reserve = trip ? trip.vehicle.reserve_soc : 10;

    // Der Massstab richtet sich nach dem, was es gibt: mit Plan nach der
    // ganzen Strecke, ohne Plan nach dem, was schon gefahren wurde.
    /* Ohne Plan gibt es kein `km_auf_route` - es kommt aus der Projektion
     * auf die Route, und eine Aufzeichnung hat keine. Es steht deshalb
     * für **jeden** Punkt auf null, und die Kurve fiel zu einem senkrechten
     * Strich am linken Rand zusammen. Ausgerechnet dort, wo sie das
     * Einzige ist, was es zu sehen gibt.
     *
     * Gemessen wird dann entlang der gefahrenen Spur: Für Punkt i die
     * Summe der Abstände bis dorthin. Das ist die Strecke, die wirklich
     * zurückgelegt wurde, und damit die richtige Achse. */
    /* Hier stand eine Schleife, die die Strecke beim Zeichnen aus der Spur
     * nachrechnete - `abstandKm(spur[i-1], spur[i])` für jeden Verlaufs-
     * punkt i. Das setzte voraus, dass `spur[i]` zu `verlauf[i]` gehört,
     * und das tut es nicht: Die Spur wächst bei jeder neuen Position, der
     * Verlauf bei jedem neuen Ladestand. Beim Aufzeichnen mit Dongle war
     * der Verlauf um ein Vielfaches kürzer, und die Kurve rückte dadurch
     * an den linken Rand - genau der Fehler, den diese Schleife beheben
     * sollte. Jetzt trägt jeder Verlaufspunkt seine Strecke selbst. */
    const ownKm = !profile.length;
    const distanceFrom = (v) => ownKm ? (v.driven_km || 0) : v.km;
    const maxKm = profile.length
      ? (profile[profile.length - 1].km || 1)
      : Math.max(1, ...history.map(distanceFrom));
    const left_side = 4, right = extent - 4, upper = 8, bottom = elevation - 16;
    const x = (km) => left_side + (km / maxKm) * (right - left_side);
    const y = (soc) => bottom - (Math.max(0, Math.min(100, soc)) / 100)
      * (bottom - upper);

    // Höhenprofil im Hintergrund. Es erklärt die Knicke in beiden Kurven -
    // ohne diese Erklärung wirken sie wie Messfehler.
    if (profile.length > 1) {
      let maxElevation = 1;
      for (const p of profile) maxElevation = Math.max(maxElevation, p.elevation || 0);
      pen.beginPath();
      pen.moveTo(x(0), bottom);
      for (const p of profile) {
        pen.lineTo(x(p.km), bottom - ((p.elevation || 0) / maxElevation) * (bottom - upper) * 0.3);
      }
      pen.lineTo(x(maxKm), bottom);
      pen.closePath();
      pen.fillStyle = "rgba(138,151,165,.12)";
      pen.fill();
    }

    // Die Reserve als Linie, nicht als Zahl: Man sieht sofort, wo die
    // gemessene Kurve auf sie zuläuft.
    pen.beginPath();
    pen.setLineDash([4, 4]);
    pen.moveTo(left_side, y(reserve));
    pen.lineTo(right, y(reserve));
    pen.strokeStyle = "rgba(226,89,106,.6)";
    pen.lineWidth = 1;
    pen.stroke();
    pen.setLineDash([]);
    pen.fillStyle = "rgba(226,89,106,.75)";
    pen.font = "10px system-ui, sans-serif";
    pen.fillText("Reserve", left_side + 2, y(reserve) - 3);

    // Geplante Kurve: gedämpft, sie ist der Bezug und nicht die Nachricht.
    if (profile.length > 1) {
      pen.beginPath();
      profile.forEach((p, i) => {
        const px = x(p.km), py = y(p.soc);
        if (i === 0) pen.moveTo(px, py); else pen.lineTo(px, py);
      });
      pen.strokeStyle = "rgba(138,151,165,.55)";
      pen.lineWidth = 1.5;
      pen.stroke();
    }

    // Die Ladestopps als Marken - sie erklären die Sprünge, die gleich
    // kommen, und zeigen, wie weit der nächste noch weg ist.
    for (const stop of (plan && plan.stops) || []) {
      const px = x(stop.km_on_route);
      pen.beginPath();
      pen.moveTo(px, upper);
      pen.lineTo(px, bottom);
      pen.strokeStyle = "rgba(255,201,60,.35)";
      pen.lineWidth = 1;
      pen.stroke();
    }

    // Die gemessene Kurve. Sie ist die Nachricht, also kräftig.
    if (history.length > 1) {
      pen.beginPath();
      history.forEach((v, i) => {
        const px = x(distanceFrom(v)), py = y(v.soc);
        if (i === 0) pen.moveTo(px, py); else pen.lineTo(px, py);
      });
      pen.strokeStyle = "#ffc93c";
      pen.lineWidth = 2.5;
      pen.lineJoin = "round";
      pen.stroke();
    }

    // Wo das Auto gerade ist. Ein gerechneter Ladestand bekommt einen
    // hohlen Punkt - man soll ihm ansehen, dass er nicht gemessen ist.
    const now_ts = history[history.length - 1];
    if (now_ts) {
      pen.beginPath();
      pen.arc(x(distanceFrom(now_ts)), y(now_ts.soc), 4.5, 0, Math.PI * 2);
      if (now_ts.reported) { pen.fillStyle = "#ffc93c"; pen.fill(); }
      else { pen.strokeStyle = "#ffc93c"; pen.lineWidth = 2; pen.stroke(); }
    }

    pen.fillStyle = "rgba(138,151,165,.8)";
    pen.fillText("0", left_side, elevation - 4);
    const label = K.num(maxKm) + " km";
    pen.fillText(label, right - pen.measureText(label).width,
                   elevation - 4);
  }

  /* ---------- Der Ladeplan unterwegs ---------- */

  function drawPlan() {
    const lst = document.getElementById("live-plan");
    const as_of = document.getElementById("live-plan-stand");
    if (!lst || !as_of) return;

    if (!plan) {
      lst.innerHTML = '<li class="leer">Noch kein Ladeplan.</li>';
      as_of.textContent = "";
      return;
    }
    as_of.textContent = plan.reading_km ? "gerechnet ab km " + K.num(plan.reading_km)
                                      : "beim Losfahren gerechnet";

    if (!plan.feasible) {
      lst.innerHTML = `<li class="leer" style="color:#e2596a">${
        sanitize(plan.reason || "Kein Ladeplan möglich.")}</li>`;
      return;
    }
    if (!plan.stops || !plan.stops.length) {
      lst.innerHTML = '<li class="leer">Kein Ladestopp mehr nötig.</li>';
      return;
    }

    lst.innerHTML = "";
    plan.stops.forEach((s, i) => {
      const entry = document.createElement("li");
      entry.innerHTML = `
        <div class="haupt">
          <div class="titel">${i + 1}. ${sanitize(s.name || s.operator
            || "Ladepunkt")}</div>
          <div class="unter">km ${K.num(s.km_on_route)} ·
            ${K.num(s.arrival_soc)} % → ${K.num(s.departure_soc)} % ·
            ${K.num(s.max_kw)} kW · ${s.point_count} Ladepunkte</div>
        </div>
        <div class="kw">${K.duration(s.charge_time_minutes)}</div>`;
      lst.appendChild(entry);
    });
  }

  /* Eine Änderung am Plan ist der einzige Anlass, jemanden am Steuer zu
   * stören - deshalb hier und sonst nirgends eine Benachrichtigung. */
  function reportChange(text) {
    const box = document.getElementById("live-aenderung");
    if (box) {
      box.textContent = text || "Der Ladeplan hat sich geändert.";
      box.hidden = false;
    }
    notify(text || "Der Ladeplan hat sich geändert.");
  }

  function notify(text) {
    // Ohne erteilte Erlaubnis wird nicht gefragt und nicht benachrichtigt:
    // Wer die Ansicht offen hat, sieht die Meldung ohnehin. Gefragt wird
    // einmal beim Start der Fahrt, wo die Frage auch etwas bedeutet.
    try {
      if (!("Notification" in window) || Notification.permission !== "granted") {
        return;
      }
      new Notification("jolt – Ladeplan geändert", { body: text, day: "jolt-plan" });
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
  async function notificationsSetUp() {
    try {
      if (!("Notification" in window) || !("PushManager" in window)) return;

      const keyname = await K.api("/api/push/schluessel");
      if (!keyname.configured) return;   // kein VAPID-Schlüssel am Server

      if (Notification.permission === "default") {
        await Notification.requestPermission();
      }
      if (Notification.permission !== "granted") return;

      const registrierung = K.state.serviceWorker
        || (navigator.serviceWorker && await navigator.serviceWorker.ready);
      if (!registrierung || !registrierung.pushManager) return;

      // Ein bestehendes Abo weiterverwenden. Ein neues anzulegen gäbe
      // denselben Endpunkt zurück, kostet aber einen Umweg.
      let subscription = await registrierung.pushManager.getSubscription();
      if (!subscription) {
        subscription = await registrierung.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: keyAsBytes(keyname.keyname),
        });
      }

      const records = subscription.toJSON();
      await K.api("/api/push/abo", { method: "POST", body: {
        endpoint: records.endpoint,
        p256dh: records.keys.p256dh,
        auth: records.keys.auth,
        device: navigator.userAgent.slice(0, 120),
      }});
    } catch (failure) {
      // Bewusst nur ins Log: Wer gerade losfährt, will keine Fehlermeldung
      // über eine Nebenfunktion lesen.
      if (window.console) console.warn("Benachrichtigungen:", failure.message);
    }
  }

  /* Der öffentliche Schlüssel kommt als base64url und muss als Uint8Array
   * übergeben werden - der Browser nimmt die Zeichenkette nicht an. */
  function keyAsBytes(text) {
    const filled = (text + "=".repeat((4 - text.length % 4) % 4))
      .replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(filled);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return bytes;
  }

  /* Die Namen kommen aus fremden Datenquellen und landen in innerHTML. */
  function sanitize(text) {
    const helper = document.createElement("div");
    helper.textContent = text || "";
    return helper.innerHTML;
  }

  /* Wo der Messpunkt lag - [lon, lat], oder null.
   *
   * Der Server schickt die Koordinate mit. Vorher tat er das nicht, und die
   * Ansicht rechnete sie aus dem *geplanten* Profil zurück (`z_lat`/`z_lon`
   * darunter). Bei einer Aufzeichnung gibt es dieses Profil nicht - es
   * entsteht erst beim Abschliessen -, und die Rückrechnung lieferte stumm
   * (0, 0): Karte im Golf von Guinea, Spur aus einem einzigen Punkt.
   *
   * Der Rückfall bleibt für Sitzungen, die noch von einer älteren Fassung
   * bedient werden - dort ist er richtig, weil es dann eine Route gibt. */
  function measurement_site(z) {
    if (typeof z.lat === "number" && typeof z.lon === "number"
        && (z.lat !== 0 || z.lon !== 0)) {
      return [z.lon, z.lat];
    }
    const lat = z_lat(z), lon = z_lon(z);
    return (lat === 0 && lon === 0) ? null : [lon, lat];
  }

  /* Der Rückfall: über das Profil hängt an jedem Kilometerstand eine
   * Position. Gilt nur für geplante Fahrten. */
  function z_lat(z) { return pointAtKm(z.km_on_route).lat; }
  function z_lon(z) { return pointAtKm(z.km_on_route).lon; }

  function pointAtKm(km) {
    const profile = (K.state.trip || {}).profile || [];
    return profile.find((p) => p.km >= km) || profile[profile.length - 1]
      || { lat: 0, lon: 0 };
  }

  async function simulate() {
    if (!K.state.sessionId) { K.report("Keine Live-Fahrt.", "fehler"); return; }
    const more = Number(document.getElementById("mehrverbrauch").value) / 100;
    const jam = Number(document.getElementById("stau").value) / 100;
    try {
      await K.api(`/api/live/${K.state.sessionId}/simulieren`
        + `?extra_consumption=${more}&tick_s=0.3&time_factor=${jam}`,
        { method: "POST" });
      K.report(`Simulation läuft mit ${Math.round(more * 100)} % Verbrauch `
        + `und ${Math.round(jam * 100)} % Fahrzeit.`, "hinweis");
    } catch (failure) {
      K.report("Simulation: " + failure.message, "fehler");
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
  function locationInput(coords, timeMs) {
    // Vor der Drosselung: Der Zustand will jeden Fix sehen, nicht jeden
    // zwölften.
    examineDrivingState(coords, timeMs);
    const now_ts = Date.now();
    // Nicht jede GPS-Aktualisierung melden: Das Gerät liefert im
    // Sekundentakt, und die Nachführung mittelt ohnehin über Kilometer.
    // Häufiger zu senden kostet Akku und Mobilfunk, ohne etwas zu sagen.
    if (now_ts - latestReport < REPORT_INTERVAL_MS) return;
    latestReport = now_ts;
    reportPosition(coords, timeMs);
  }

  function positionTrace() {
    if (awake !== null) return;
    const native = nativeLocation();
    if (native) { nativeTrace(native); return; }
    webTrace();
  }

  function webTrace() {
    if (!navigator.geolocation) return;
    screenAwakeHold();
    awake = navigator.geolocation.watchPosition(
      (pos) => locationInput(pos.coords, pos.timestamp),
      // Ein GPS-Fehler unterwegs ist kein Grund, den Nutzer zu behelligen -
      // in einem Tunnel ist er der Normalfall, und die nächste Messung kommt.
      () => {},
      { enableHighAccuracy: true, maximumAge: 15000, timeout: 30000 });
  }

  /* Der Standort bei gesperrtem Telefon - nur in der iOS-App.
   *
   * `watchPosition` im WebView liefert nichts mehr, sobald der Bildschirm
   * gesperrt ist: iOS friert die Seite ein, und mit ihr die Meldungen. Das
   * Plugin dagegen läuft über `CLLocationManager` mit
   * `allowsBackgroundLocationUpdates`; solange es Positionen liefert, hält
   * iOS die App am Leben, und die Meldungen - samt Dongle-Lesen und
   * Warteschlange - laufen weiter wie im Vordergrund.
   *
   * Entscheidend ist `backgroundMessage`: Ist sie gesetzt, bleibt der Watcher
   * auch im Hintergrund aktiv, sonst nur im Vordergrund. Auf iOS sieht man
   * dann die blaue Standortanzeige in der Statusleiste - gewollt.
   *
   * Im Browser gibt es das Plugin nicht, dort gilt `watchPosition`.
   *
   * **Das Plugin muss auch wirklich eingebaut sein.** Die App lädt ihre
   * Oberfläche zur Laufzeit vom Server; dieser Code ist also sofort da, die
   * native Klasse dagegen erst nach einem neuen App-Bau. Auf einem älteren
   * Stand liefert `registerPlugin` einen Stellvertreter, dessen Aufrufe mit
   * "not implemented" scheitern - und die Fahrt hätte gar keinen Standort
   * mehr. `isPluginAvailable` fragt nach, und sonst gilt der Browser-Weg. */
  function nativeLocation() {
    const h = window.joltBlePlugin;
    if (!h || !h.Capacitor || !h.Capacitor.isNativePlatform()
        || !h.BackgroundGeolocation) return null;
    if (typeof h.Capacitor.isPluginAvailable === "function"
        && !h.Capacitor.isPluginAvailable("BackgroundGeolocation")) return null;
    return h.BackgroundGeolocation;
  }

  async function nativeTrace(plugin) {
    // Sofort besetzen: `addWatcher` antwortet erst nach der
    // Berechtigungsfrage, und bis dahin darf kein zweiter Start dazwischen.
    awake = "nativ";
    const cycle = ++nativeRun;
    screenAwakeHold();
    let id;
    try {
      id = await plugin.addWatcher({
        backgroundTitle: "jolt zeichnet die Fahrt auf",
        backgroundMessage: "Position und Ladestand werden weiter erfasst.",
        requestPermissions: true,
        distanceFilter: 0,
      }, (city, failure) => {
        if (failure) { locationError(failure); return; }
        if (!city || typeof city.latitude !== "number") return;
        locationInput({
          latitude: city.latitude, longitude: city.longitude,
          // Das Plugin liefert null statt -1, wenn die Geschwindigkeit fehlt.
          speed: typeof city.speed === "number" ? city.speed : null,
          altitude: typeof city.altitude === "number" ? city.altitude : null,
        }, city.time);
      });
    } catch (failure) {
      // Das Plugin ging nicht - dann wenigstens der Standort im Vordergrund,
      // statt für den Rest der Fahrt gar keinen.
      if (cycle === nativeRun) { awake = null; webTrace(); }
      locationError(failure);
      return;
    }
    // Beendet, während iOS noch fragte: den eben angelegten Watcher gleich
    // wieder entfernen, sonst läuft er ohne Fahrt weiter und kostet Akku.
    if (cycle !== nativeRun || awake !== "nativ") {
      dropWatcher(plugin, id);
      return;
    }
    nativeAwakeId = id;
  }

  function dropWatcher(plugin, id) {
    // Ein Promise: Eine Ablehnung fängt kein try/catch.
    try {
      Promise.resolve(plugin.removeWatcher({ id })).catch(() => {});
    } catch (e) { /* schon weg */ }
  }

  function locationError(failure) {
    if (failure && failure.code === "NOT_AUTHORIZED") {
      // Einmal sagen, nicht bei jedem Rückruf. Ohne Erlaubnis gibt es bei
      // gesperrtem Telefon keine Messpunkte - das muss man wissen, bevor man
      // losfährt.
      if (locationErrorReported) return;
      locationErrorReported = true;
      K.report("Standort nicht erlaubt. In den iOS-Einstellungen für jolt "
        + "Standort auf „Beim Verwenden“ oder „Immer“ stellen - sonst "
        + "kommen bei gesperrtem Telefon keine Messpunkte an.", "warnung");
    }
    // Alles andere ist wie beim Browser: ein Tunnel, die nächste Messung kommt.
  }

  function positionGiveUp() {
    if (awake === null) return;
    if (awake === "nativ") {
      const id = nativeAwakeId;
      nativeAwakeId = null;
      nativeRun++;
      const plugin = nativeLocation();
      if (id && plugin) {
        dropWatcher(plugin, id);
      }
    } else {
      try { navigator.geolocation.clearWatch(awake); } catch (e) {}
    }
    awake = null;
    locationErrorReported = false;
    screenRelease();
  }

  /* ---------- Der Bildschirm muss anbleiben ---------- */

  /* Ohne das schaltet iOS den Bildschirm nach einer Minute aus, und mit dem
   * Bildschirm schläft die Seite: `watchPosition` liefert nichts mehr, die
   * Bluetooth-Schleife steht, und beim Aufwachen fehlt das Stück dazwischen.
   *
   * Die Diagnoseseite unter /obd hatte das von Anfang an, diese Ansicht
   * nicht - und aufgezeichnet wird hier. In der ersten echten Testfahrt
   * klafft genau deshalb eine Lücke von acht Minuten mit einem einzigen
   * Messpunkt darin.
   *
   * Die Sperre geht verloren, sobald die Seite in den Hintergrund gerät, und
   * kommt nicht von selbst zurück; deshalb wird sie beim Zurückkommen neu
   * geholt. */
  let wake_lock = null;
  let keepAwakeVideo = null;

  /* Auf iOS bleibt die Wake-Lock-API in einer als App vom Homescreen
   * gestarteten Seite ("standalone", siehe manifest.json) unzuverlässig -
   * mal wird die Sperre gar nicht erst erteilt, mal fällt sie nach kurzer
   * Zeit von selbst weg, ohne ein "release"-Ereignis auszulösen. Ein
   * stummes, unsichtbares Video, das in einer Dauerschleife läuft, hält den
   * Bildschirm dagegen zuverlässig wach - dieselbe Technik, die NoSleep.js
   * verwendet. Beide Wege laufen parallel, keiner schadet dem anderen.
   *
   * Ein leeres Canvas als Quelle (`captureStream()`) reichte nicht: iOS hat
   * das offenbar nicht durchgehend als echte Wiedergabe gewertet, der
   * Bildschirm ging trotzdem aus. Ein tatsächliches, wenn auch winziges
   * Video (1 Sekunde, 2x2 Pixel, schwarz, ohne Ton - 1,5 kB) läuft
   * zuverlässiger. Eingebettet statt als eigene Datei, damit nichts vom
   * Netz nachgeladen werden muss, bevor die Sperre greift. */
  const KEEP_AWAKE_MP4 = "data:video/mp4;base64,AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAMXbW9vdgAAAGxtdmhkAAAA"
    + "AAAAAAAAAAAAAAAD6AAAA+gAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAB"
    + "AAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAkF0"
    + "cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAA+gAAAAAAAAAAAAAAAAAAAAA"
    + "AAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAIAAAACAAAAAAAkZWR0"
    + "cwAAABxlbHN0AAAAAAAAAAEAAAPoAAAAAAABAAAAAAG5bWRpYQAAACBtZGhkAAAAAAAA"
    + "AAAAAAAAAABAAAAAQABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABW"
    + "aWRlb0hhbmRsZXIAAAABZG1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAA"
    + "HGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAASRzdGJsAAAAwHN0c2QAAAAAAAAAAQAA"
    + "ALBhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAIAAgBIAAAASAAAAAAAAAABFUxh"
    + "dmM1OS4zNy4xMDAgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAANmF2Y0MBZAAK/+EAGWdk"
    + "AAqs2V+IiMBEAAADAAQAAAMACDxIllgBAAZo6+PLIsD9+PgAAAAAEHBhc3AAAAABAAAA"
    + "AQAAABRidHJ0AAAAAAAAFigAABYoAAAAGHN0dHMAAAAAAAAAAQAAAAEAAEAAAAAAHHN0"
    + "c2MAAAAAAAAAAQAAAAEAAAABAAAAAQAAABRzdHN6AAAAAAAAAsUAAAABAAAAFHN0Y28A"
    + "AAAAAAAAAQAAA0cAAABidWR0YQAAAFptZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGly"
    + "YXBwbAAAAAAAAAAAAAAAAC1pbHN0AAAAJal0b28AAAAdZGF0YQAAAAEAAAAATGF2ZjU5"
    + "LjI3LjEwMAAAAAhmcmVlAAACzW1kYXQAAAKtBgX//6ncRem95tlIt5Ys2CDZI+7veDI2"
    + "NCAtIGNvcmUgMTY0IHIzMDk1IGJhZWU0MDAgLSBILjI2NC9NUEVHLTQgQVZDIGNvZGVj"
    + "IC0gQ29weWxlZnQgMjAwMy0yMDIyIC0gaHR0cDovL3d3dy52aWRlb2xhbi5vcmcveDI2"
    + "NC5odG1sIC0gb3B0aW9uczogY2FiYWM9MSByZWY9MyBkZWJsb2NrPTE6MDowIGFuYWx5"
    + "c2U9MHgzOjB4MTEzIG1lPWhleCBzdWJtZT03IHBzeT0xIHBzeV9yZD0xLjAwOjAuMDAg"
    + "bWl4ZWRfcmVmPTEgbWVfcmFuZ2U9MTYgY2hyb21hX21lPTEgdHJlbGxpcz0xIDh4OGRj"
    + "dD0xIGNxbT0wIGRlYWR6b25lPTIxLDExIGZhc3RfcHNraXA9MSBjaHJvbWFfcXBfb2Zm"
    + "c2V0PS0yIHRocmVhZHM9MSBsb29rYWhlYWRfdGhyZWFkcz0xIHNsaWNlZF90aHJlYWRz"
    + "PTAgbnI9MCBkZWNpbWF0ZT0xIGludGVybGFjZWQ9MCBibHVyYXlfY29tcGF0PTAgY29u"
    + "c3RyYWluZWRfaW50cmE9MCBiZnJhbWVzPTMgYl9weXJhbWlkPTIgYl9hZGFwdD0xIGJf"
    + "Ymlhcz0wIGRpcmVjdD0xIHdlaWdodGI9MSBvcGVuX2dvcD0wIHdlaWdodHA9MiBrZXlp"
    + "bnQ9MjUwIGtleWludF9taW49MSBzY2VuZWN1dD00MCBpbnRyYV9yZWZyZXNoPTAgcmNf"
    + "bG9va2FoZWFkPTQwIHJjPWNyZiBtYnRyZWU9MSBjcmY9MjMuMCBxY29tcD0wLjYwIHFw"
    + "bWluPTAgcXBtYXg9NjkgcXBzdGVwPTQgaXBfcmF0aW89MS40MCBhcT0xOjEuMDAAgAAA"
    + "ABBliIQAFf/+98nvwKbr29+B";

  /* Ausserhalb des sichtbaren Bereichs positioniert (negative Koordinaten)
   * hat es nicht gehalten - vermutlich zaehlt ein Video, das gar nicht im
   * sichtbaren Bereich liegt, für iOS nicht als echte Wiedergabe. Jetzt
   * steht es tatsächlich in der oberen linken Ecke, nur eben ein einzelnes,
   * fast durchsichtiges Pixel gross - das fällt nicht auf, zählt aber. */
  function fetchVideoKeepAwake() {
    if (keepAwakeVideo) return keepAwakeVideo;
    const video = document.createElement("video");
    video.muted = true;
    video.loop = true;
    video.playsInline = true;
    video.setAttribute("playsinline", "");
    video.setAttribute("webkit-playsinline", "");
    video.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;"
      + "opacity:0.01;pointer-events:none;z-index:-1;";
    video.src = KEEP_AWAKE_MP4;
    document.body.appendChild(video);
    keepAwakeVideo = video;
    return video;
  }

  /* Beide Wege melden zurück, statt nur ins unsichtbare Konsolenprotokoll zu
   * schreiben - am Steuer kommt niemand an die Konsole heran, und ohne eine
   * sichtbare Rückmeldung liesse sich ein Fehlschlag nur raten statt sehen. */
  /* In der iOS-App ist das eine Zeile, und sie hält.
   *
   * `isIdleTimerDisabled` sagt dem System schlicht, den Sperrtimer nicht
   * laufen zu lassen - kein Wake Lock, der widerrufen wird, kein Video, das
   * als Wiedergabe gelten muss. Der Weg darunter bleibt trotzdem stehen:
   * Die Oberfläche läuft weiter auch im Browser, und dort gibt es nichts
   * Besseres als Wake Lock und den Videobehelf. */
  async function nativeAwakeHold() {
    const h = window.joltBlePlugin;
    if (!h || !h.Capacitor || !h.Capacitor.isNativePlatform()) return false;
    try {
      await h.KeepAwake.keepAwake();
      return true;
    } catch (failure) {
      console.log("[live] KeepAwake ging nicht:", failure.message);
      return false;
    }
  }

  async function screenAwakeHold() {
    if (await nativeAwakeHold()) return;
    let hint = "";
    if (!wake_lock && "wakeLock" in navigator) {
      try {
        wake_lock = await navigator.wakeLock.request("screen");
        wake_lock.addEventListener("release", () => { wake_lock = null; });
      } catch (failure) {
        hint = "Wake Lock: " + failure.message;
      }
    } else if (!("wakeLock" in navigator)) {
      hint = "Wake Lock: vom Browser nicht unterstützt";
    }
    try {
      await fetchVideoKeepAwake().play();
    } catch (failure) {
      hint += (hint ? " / " : "") + "Video: " + failure.message;
    }
    if (hint) {
      console.log("[live] Bildschirm wachhalten:", hint);
      K.report("Bildschirm bleibt evtl. nicht an (" + hint + ") - "
        + "sicherheitshalber Automatische Sperre in den iOS-Einstellungen "
        + "auf „Nie” stellen.", "warnung");
    }
  }

  function screenRelease() {
    // Nach der Fahrt soll sich das Telefon wieder normal sperren. Der
    // native Weg wird zuerst zurückgenommen; die beiden darunter schaden
    // nicht, wenn sie gar nicht erst gegriffen haben.
    const h = window.joltBlePlugin;
    if (h && h.Capacitor && h.Capacitor.isNativePlatform()) {
      h.KeepAwake.allowSleep().catch(() => {});
    }
    if (wake_lock) {
      try { wake_lock.release(); } catch (e) {}
      wake_lock = null;
    }
    if (keepAwakeVideo) {
      try { keepAwakeVideo.pause(); } catch (e) {}
    }
  }

  /* Zurück im Vordergrund: aufholen, was im Hintergrund liegengeblieben ist.
   *
   * iOS friert eine Seite im Hintergrund ein. Der Abriss der
   * Bluetooth-Verbindung wird dann zwar gemeldet, aber der Wiederaufbau
   * hängt an einem Zeitgeber, und der läuft erst weiter, wenn die Seite
   * wieder sichtbar ist. Ohne dieses Nachfassen bliebe der Dongle getrennt,
   * bis der nächste Abriss kommt - und der kommt nicht mehr. */
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible" || !K.state.sessionId) return;
    screenAwakeHold();
    // `donglePause` gehört mit in beide Bedingungen: An der Ladesäule ist
    // das Auto verriegelt, und eine Verbindung, die jolt hier von selbst
    // zurückholt, löst die Alarmanlage aus. Heute deckt `dongle` den Fall
    // schon ab - aber diese Bedingung darf nicht davon abhängen, dass eine
    // zweite Variable anderswo richtig gesetzt wurde.
    reconnectDongle();
    // Sofort einen Punkt melden, statt bis zum nächsten Takt zu warten:
    // Nach einer Pause im Hintergrund ist gerade der erste Punkt danach der
    // wichtige - er schliesst die Lücke.
    latestReport = 0;
  });

  /* Wenn ein Dongle mitliest, wandert der Ladestand von hier aus mit.
   *
   * Bewusst an dieselbe Meldung gehängt und nicht als zweite Schleife: So
   * gehören Position und Ladestand zu **einem** Messpunkt und derselben
   * Sekunde. Zwei Schleifen ergäben Punkte, die sich abwechseln - einer mit
   * Position, einer mit Ladestand -, und die Nachführung müsste beides
   * wieder zusammensuchen. */
  /* Pausiert der Dongle gerade?
   *
   * An der Ladesäule wird abgeschlossen, und ein verriegeltes Auto, das
   * weiter über CAN gefragt wird, löst die Alarmanlage aus. Ein blosses
   * "nicht mehr lesen" genügt dabei nicht - der Wiederaufbau würde die
   * Verbindung von selbst zurückholen. Pause heisst deshalb: trennen und
   * nicht wieder aufbauen, bis jemand es sagt. */
  let donglePause = false;

  /* ---------- Wann darf der Dongle das Auto fragen? ---------- */

  /* Ob das Auto verriegelt ist, lässt sich **nicht** erfahren, ohne es zu
   * fragen - und genau das Fragen löst bei verriegeltem Auto die
   * Alarmanlage aus. Ein Signal, das der Dongle oder das Auto von sich aus
   * aussendet, gibt es nicht. Also wird umgekehrt gerechnet: Gelesen wird
   * nur, wenn das Telefon etwas weiss, was bei verriegeltem Auto nicht sein
   * kann - es bewegt sich mit Fahrtgeschwindigkeit.
   *
   *   fährt    ab 15 km/h (zwei Messungen hintereinander). Zu Fuss kommt man
   *            nicht dorthin, und wer so schnell ist, sitzt im Auto.
   *            Gelesen wird, und ist der Dongle weg, wird er geholt.
   *   steht    unter 3 km/h seit zehn Sekunden. Es wird nichts mehr gefragt;
   *            die Verbindung bleibt, ein Dongle im Leerlauf sendet nichts auf
   *            den Bus. Ampel, Stau und Zapfsäule kosten so keinen Neuaufbau.
   *   geparkt  das Telefon ist zu Fuss mehr als 25 m vom Halteort weg, oder
   *            das Auto steht seit drei Minuten: Verbindung trennen, nicht
   *            wieder aufbauen. Erst eine Fahrt holt sie zurück.
   *
   * Die Zähler im Auto laufen über die Lebensdauer: Was während einer Pause
   * verbraucht wurde, steckt in der Differenz der nächsten Messung. Eine
   * Lücke im Stand kostet deshalb keinen Verbrauch, nur Einzelwerte.
   *
   * Nicht erfassbar: Wer abschliesst, noch bevor zehn Sekunden vergangen
   * sind, kann im ersten Moment noch eine Abfrage auslösen. Sicher wäre nur
   * ein Signal aus dem Auto selbst. */
  const FAST_KMH = 15;
  const STAND_KMH = 3;
  const STANDING_TIME_MS = 10000;
  const PARK_TIME_MS = 180000;
  const PATH_M = 25;
  // Hat jemand "Dongle verbinden" von Hand getippt, will er im Stand lesen.
  const MANUAL_MS = 600000;
  // Kein Dongle in Reichweite (Fahrrad, Bus): Nach so vielen Fehlversuchen ist
  // Schluss, bis wieder angehalten wurde.
  const AGAIN_ATTEMPTS_MAX = 8;
  const AGAIN_ATTEMPTS_DRIVING = 40;

  let autoMode = true;
  try { autoMode = localStorage.getItem("jolt-dongle-auto") !== "0"; }
  catch (e) { /* ohne Speicher gilt die Vorgabe */ }

  let driveState = "steht";     // "faehrt" | "steht" | "geparkt"
  let standSince = null;
  let standCity = null;
  let fastSequence = 0;
  let asOfSeen = true;
  let manualUntil = 0;
  let latestPosition = null;
  let latestSpeed = null;

  function readAllowed() {
    if (donglePause) return false;
    // Beim Mithören (Einstellungen) darf nichts gefragt werden.
    if (window.joltObd && window.joltObd.listens && window.joltObd.listens()) return false;
    if (!autoMode) return true;
    return driveState === "faehrt" || Date.now() < manualUntil;
  }

  function drivingStateStart(state) {
    driveState = state;
    standSince = null; standCity = null; fastSequence = 0;
    asOfSeen = true; manualUntil = 0; latestPosition = null;
    voltageBasis = []; voltageSequence = 0;
  }

  function distanceM(a, b) {
    const R = 6371000, rad = Math.PI / 180;
    const dLat = (b.lat - a.lat) * rad, dLon = (b.lon - a.lon) * rad;
    const h = Math.sin(dLat / 2) ** 2
      + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(h));
  }

  /* Die Geschwindigkeit aus dem Fix - oder aus zwei Fixes, wenn das Gerät
   * keine liefert (im Browser auf iOS regelmässig). */
  function speedKmh(coords, city, timeMs) {
    const prior = latestPosition;
    latestPosition = { lat: city.lat, lon: city.lon, timestamp: timeMs };
    if (typeof coords.speed === "number" && coords.speed >= 0) {
      return coords.speed * 3.6;
    }
    if (!prior) return null;
    const dt = (timeMs - prior.timestamp) / 1000;
    if (dt < 1 || dt > 30) return null;
    return distanceM(prior, city) / dt * 3.6;
  }

  function examineDrivingState(coords, timeMs) {
    if (!autoMode || !K.state.sessionId) return;
    const now_ts = Date.now();
    const city = { lat: coords.latitude, lon: coords.longitude };
    const v = speedKmh(coords, city, timeMs || now_ts);
    if (v === null) return;
    latestSpeed = v;

    if (v >= FAST_KMH) {
      standSince = null; standCity = null;
      fastSequence++;
      if (fastSequence >= 2 && driveState !== "faehrt") driveBegins();
      return;
    }
    fastSequence = 0;
    if (v < STAND_KMH) asOfSeen = true;
    if (now_ts < manualUntil || driveState === "geparkt") return;

    if (v < STAND_KMH) {
      if (standSince === null) { standSince = now_ts; standCity = city; }
      const as_of = now_ts - standSince;
      if (as_of >= PARK_TIME_MS) statePark("Das Auto steht seit drei Minuten");
      else if (as_of >= STANDING_TIME_MS && driveState === "faehrt") driveState = "steht";
    } else if (standCity && distanceM(standCity, city) > PATH_M) {
      // Langsam und weit vom Halteort: Man ist ausgestiegen und geht.
      statePark("Du bist vom Auto weggegangen");
    }
  }

  /* Das Auto ist aus - das merkt man an der 12-V-Spannung, ohne zu fragen.
   *
   * `ATRV` misst der ELM-Chip selbst, es geht nichts auf den CAN-Bus (siehe
   * `obd-core.js: spannung`). Solange das Auto an ist oder lädt, hält der
   * DC/DC-Wandler die Spannung oben; geht es aus, fällt sie binnen
   * Sekunden. Das passiert **vor** dem Abschliessen - man schaltet aus,
   * steigt aus und schliesst ab - und ist damit das Signal, das die
   * Bewegung des Telefons nicht liefern kann.
   *
   * Eine feste Schwelle gibt es absichtlich nicht: Wie hoch die Spannung
   * bei laufendem Wandler liegt, ist von Auto zu Auto verschieden. Verglichen
   * wird mit dem Mittel aus der letzten Fahrt, und zwar nur im Stand - ein
   * Abfall während der Fahrt ist keine Parkposition. Wer nie gefahren ist,
   * hat keine Grundlage; dann gelten die Regeln über Stand und Weg.
   *
   * Lädt das Auto verriegelt, bleibt die Spannung oben, und es bleibt bei
   * diesen Regeln. Auch das ist in Ordnung: Dann ist die Abfrage gerade
   * *nicht* das Problem, solange nichts mehr gefragt wird. */
  const VOLTAGE_TICK_MS = 2000;
  const VOLTAGE_CASE_V = 0.7;
  const VOLTAGE_SEQUENCE = 2;
  const VOLTAGE_BASIS = 5;
  let voltageBasis = [];
  let voltageSequence = 0;
  let voltageRunning = false;
  let latestVoltage = null;
  let latestVoltageTime = 0;

  function avg(lst) {
    const sortiert = [...lst].sort((a, b) => a - b);
    const m = Math.floor(sortiert.length / 2);
    return sortiert.length % 2 ? sortiert[m] : (sortiert[m - 1] + sortiert[m]) / 2;
  }

  async function examineVoltage() {
    if (voltageRunning || !K.state.sessionId || !dongle || !autoMode
        || driveState === "geparkt" || donglePause || !window.joltObd
        || !window.joltObd.linked() || !window.joltObd.voltage) return;
    voltageRunning = true;
    try {
      const v = await window.joltObd.voltage();
      if (typeof v !== "number" || !(v > 5 && v < 30)) return;
      latestVoltage = v;
      latestVoltageTime = Date.now();
      if (latestSpeed !== null && latestSpeed >= FAST_KMH) {
        voltageBasis.push(v);
        if (voltageBasis.length > 40) voltageBasis.shift();
        voltageSequence = 0;
        return;
      }
      if (Date.now() < manualUntil || voltageBasis.length < VOLTAGE_BASIS) return;
      if (v < avg(voltageBasis) - VOLTAGE_CASE_V) {
        if (++voltageSequence >= VOLTAGE_SEQUENCE) {
          statePark("Die 12-V-Spannung ist gefallen, das Auto ist aus");
        }
      } else {
        voltageSequence = 0;
      }
    } finally {
      voltageRunning = false;
    }
  }

  function statePark(reason) {
    if (driveState === "geparkt") return;
    // Erst den Zustand setzen, dann trennen: `trennen()` löst den
    // Verbindungsabriss aus, und dessen Behandlung fragt `lesenErlaubt()`.
    driveState = "geparkt";
    standSince = null; standCity = null;
    asOfSeen = false;
    voltageBasis = []; voltageSequence = 0;
    if (dongle && window.joltObd) {
      try { window.joltObd.detach(); } catch (e) { /* schon getrennt */ }
      K.report(reason + " – jolt fragt das Auto nicht mehr, bis du losfährst. "
        + "So löst ein abgeschlossenes Auto keinen Alarm aus.", "hinweis");
    }
    showDongle();
  }

  function driveBegins() {
    // Nach einem Fehlversuch ohne Dongle erst wieder, wenn angehalten wurde -
    // sonst probierte eine Radfahrt den ganzen Weg über zu verbinden.
    if (driveState === "geparkt" && !asOfSeen) return;
    const wasParked = driveState === "geparkt";
    driveState = "faehrt";
    standSince = null; standCity = null;
    if (wasParked && dongle) {
      K.report("Fahrt erkannt – jolt verbindet den Dongle wieder.", "hinweis");
    }
    reconnectDongle();
    showDongle();
  }

  /* Den Dongle holen, wenn er fehlt - solange das Auto fährt, aber nicht
   * endlos: Ist keiner in Reichweite, bleibt die Verbindung aus, und jeder
   * weitere Versuch kostet nur Akku. */
  function reconnectDongle() {
    if (!dongle || !readAllowed() || !window.joltObd
        || window.joltObd.linked()) return;
    let attempts = 0;
    window.joltObd.reconnect(1, () => {
      if (!K.state.sessionId || !readAllowed()) return false;
      // Wer gerade fährt (GPS: mindestens 15 km/h), hat das Auto nicht
      // verlassen: Nach zwei Minuten aufzugeben hiesse, bis zum nächsten Halt
      // ohne Fahrzeugwerte zu fahren und von Hand neu zu verbinden. Dann wird
      // gut zwölf Minuten lang weiter angeklopft (alle 20 s, kostet wenig).
      // Im Stand - und im Bus ohne Dongle - bleibt es bei den acht Versuchen.
      const driving = latestSpeed !== null
                      && latestSpeed >= FAST_KMH;
      const bound = driving ? AGAIN_ATTEMPTS_DRIVING : AGAIN_ATTEMPTS_MAX;
      if (autoMode && ++attempts > bound) {
        driveState = "geparkt";
        asOfSeen = false;
        K.report("Kein Dongle in Reichweite – jolt versucht es erst nach dem "
          + "nächsten Halt wieder.", "hinweis");
        showDongle();
        return false;
      }
      return true;
    });
  }

  function showDongle() {
    const at = document.getElementById("dongle-an");
    const pause = document.getElementById("dongle-pause");
    if (!at || !pause) return;
    const linked = dongle && window.joltObd && window.joltObd.linked();
    at.hidden = linked && !donglePause;
    at.textContent = donglePause ? "Dongle wieder verbinden"
                                 : "Dongle verbinden";
    pause.hidden = !linked || donglePause;
  }

  async function donglePausieren() {
    donglePause = true;
    dongle = false;
    try { window.joltObd.detach(); } catch (e) {}
    showDongle();
    K.report("Dongle getrennt. Das Auto kann jetzt abgeschlossen werden – "
      + "jolt fragt nichts mehr über CAN. Die Fahrt läuft weiter, die "
      + "Position kommt vom Telefon.", "hinweis");
  }

  /* Der Dongle, der verbunden ist und trotzdem schweigt.
   *
   * Beide Rettungswege - das Ereignis `gattserverdisconnected` und das
   * Nachfassen beim Zurückkommen in den Vordergrund - fragen `verbunden()`
   * und werden nur tätig, wenn die Verbindung **weg** ist. Der häufigere
   * Fall sieht anders aus: GATT meldet weiter "verbunden", der Dongle
   * antwortet aber auf nichts mehr. Dann fällt jede Leserunde still in
   * ihren `catch`, der Messpunkt geht ohne Fahrzeugwerte hinaus - und
   * ausgelöst wird nie etwas, weil formal alles in Ordnung ist.
   *
   * Am 2. September hat das zwei Fahrten halbiert: In Sitzung 28 kamen
   * nach 16:45 Uhr 51 von 110 Minuten ohne einen einzigen Wert an, in
   * Sitzung 26 die letzten 27 Minuten. Beide Male blieb die Verbindung
   * bestehen, und beide Male hat niemand es gemerkt.
   *
   * Das Gegenmittel ist, den Abriss selbst herbeizuführen: `trennen()`
   * löst `gattserverdisconnected` aus, und daran hängt der Wiederaufbau,
   * den es längst gibt. Bleibt das Ereignis aus, wird nachgefasst.
   *
   * An der Ladesäule gilt das alles nicht: Dort ist Schweigen gewollt, und
   * ein verriegeltes Auto, das wieder über CAN gefragt wird, löst die
   * Alarmanlage aus. Daher die Bedingung auf `donglePause`. */
  const QUIET_RESTART_MS = 120000;

  function quietWatch() {
    if (!readAllowed() || !window.joltObd || !window.joltObd.linked()) return;
    // Kam noch nie etwas, läuft die Uhr ab jetzt - sonst wartet die
    // Überwachung auf einen Wert, der nie kommt, und greift nie ein.
    if (!latestRawValuesTime) { latestRawValuesTime = Date.now(); return; }
    if (Date.now() - latestRawValuesTime < QUIET_RESTART_MS) return;
    // Die Uhr sofort weiterstellen, sonst stösst die nächste Runde
    // denselben Neuaufbau noch einmal an, während der erste läuft.
    latestRawValuesTime = Date.now();
    K.report("Der Dongle antwortet seit zwei Minuten nicht mehr – jolt baut "
      + "die Verbindung neu auf.", "hinweis");
    try { window.joltObd.detach(); } catch (e) { /* schon getrennt */ }
    setTimeout(reconnectDongle, 3000);
  }

  /* Den Dongle anbieten, bevor sonst irgendetwas läuft.
   *
   * Die Reihenfolge ist nicht beliebig: `requestDevice` darf nur in
   * unmittelbarer Folge einer Nutzergeste laufen. Wer vorher auf GPS oder
   * eine API-Antwort wartet, hat die Geste verbraucht und bekommt ein
   * `SecurityError`. Deshalb steht der Dongle **zuerst** - noch vor dem
   * Anlegen der Sitzung.
   *
   * Kommt keiner zustande - kein Bluetooth im Browser, kein Dongle im
   * Auto, Dialog weggetippt -, ist das kein Grund abzubrechen, sondern
   * einer, ohne weiterzufahren: Eine Fahrt mit von Hand gemeldetem
   * Ladestand ist besser als keine Fahrt. Entschieden wird hier nichts,
   * die Rückgabe sagt nur, was daraus geworden ist. */
  async function dongleOffer() {
    if (!window.joltObd || !window.joltObd.obtainable()) return false;
    try {
      dongleUse();
      // Erst ohne Dialog: Ist der Dongle schon einmal erlaubt worden,
      // verbindet er ohne Berührung.
      await window.joltObd.attach();
      if (await handshakeSafe()) return true;
    } catch (failure) {
      console.log("[obd] Verbindung nicht zustande gekommen:", failure);
    }
    dongle = false;
    return false;
  }

  /* Den Handshake schicken, im Zweifel zweimal.
   *
   * Der ELM antwortet auf das erste ATZ nach dem Verbinden oft erst nach
   * Sekunden, oder eine Antwort kommt zu spät und trifft den nächsten Befehl.
   * Die Reihe läuft trotzdem weiter und konfiguriert den Dongle - nur das
   * Urteil "unvollständig" war bisher endgültig, und mit ihm gab der Start
   * den Dongle auf, obwohl er verbunden war und Werte lieferte. Deshalb: ein
   * zweiter Durchlauf, und wer danach verbunden ist, wird genutzt.
   * Gibt zurück, ob der Dongle benutzbar ist; die Gründe stehen im Protokoll. */
  async function handshakeSafe() {
    const O = window.joltObd;
    for (let attempt = 1; attempt <= 2; attempt++) {
      if (!O.linked()) return false;
      if (await O.handshake()) return true;
      console.log("[obd] Handshake unvollständig (Versuch " + attempt + "):",
                  (O.seriesError ? O.seriesError() : []).join("; "));
    }
    return O.linked();
  }

  async function connectDongle() {
    const btn = document.getElementById("dongle-an");
    if (btn) btn.disabled = true;
    try {
      if (!window.joltObd || !window.joltObd.obtainable()) {
        K.report("Dieser Browser kann kein Bluetooth. Mit Dongle: dieselbe "
          + "Adresse in Bluefy öffnen.", "fehler");
        return;
      }
      donglePause = false;
      // Wer von Hand verbindet, will lesen - auch im Stand, zehn Minuten lang.
      manualUntil = Date.now() + MANUAL_MS;
      if (driveState === "geparkt") driveState = "steht";
      dongleUse();
      await window.joltObd.attach();
      if (!window.joltObd.linked()) throw new Error("keine Verbindung");
      if (!(await handshakeSafe())) throw new Error("keine Verbindung");
      K.report("Dongle verbunden – ab jetzt kommen die Werte aus dem Auto.",
               "hinweis");
    } catch (failure) {
      dongle = false;
      K.report("Dongle: " + failure.message, "fehler");
    } finally {
      if (btn) btn.disabled = false;
      showDongle();
    }
  }

  function dongleUse() {
    dongle = true;
    donglePause = false;
    if (window.joltObd) {
      window.joltObd.set_up(
        (t) => console.log("[obd]", t),
        // Ein Abriss im Tunnel ist kein Grund aufzuhören, solange die Fahrt
        // läuft: Der Baustein baut selbst wieder auf.
        () => { if (K.state.sessionId) reconnectDongle(); });
    }
  }

  /* Leistung aus Spannung mal Strom. Das Vorzeichen ist **nicht** bestätigt:
   * Der Rohwert des Stroms ist um 150000 versetzt, positiv heisst also
   * entweder Entladen oder Laden - welches davon, zeigt die erste Messung
   * am Fahrzeug. Deshalb wird der Betrag angezeigt und die Richtung
   * benannt, statt eine Annahme zu treffen, die man nicht sieht. */
  function powerKw(raw) {
    if (!raw || typeof raw.voltage_v !== "number"
        || typeof raw.current_a !== "number") return null;
    return raw.voltage_v * raw.current_a / 1000;
  }

  function auxLoadRemember(raw) {
    // Liegt der gemessene Wert vor, braucht es die Näherung nicht.
    if (typeof raw.aux_load_kw === "number") return;
    const kw = powerKw(raw);
    if (kw === null) return;
    const velocity = typeof raw.speed_kmh === "number" ? raw.speed_kmh : null;
    // Nur im Stand, und nur wenn Energie entnommen wird - beim Laden misst
    // man den Lader, nicht die Heizung.
    if (velocity !== null && velocity < 5 && kw > 0) {
      aux_load = { kw, timestamp: Date.now() };
    }
  }

  /* Was das Auto misst - als **Zeile**, nicht als Kachelreihe.
   *
   * Sechs weitere Kacheln hätten dieselbe Grösse gehabt wie die vier
   * darüber, und damit hätte alles gleich wichtig ausgesehen. Diese Werte
   * braucht man aber nur gelegentlich: Man sieht hin, wenn man wissen will,
   * *warum* der Verbrauch hoch ist - nicht, um zu erfahren, dass er es ist.
   */
  function autoRow(z) {
    const block = document.getElementById("live-auto");
    const raw = latestRawValues;
    if (!block) return;
    if (!raw) {
      block.hidden = true;
      const to = document.getElementById("live-roh");
      if (to) to.hidden = true;
      return;
    }
    block.hidden = false;

    const kw = powerKw(raw);
    const velocity = typeof raw.speed_kmh === "number" ? raw.speed_kmh : null;
    const current = (kw !== null && velocity !== null && velocity >= 5)
      ? Math.abs(kw) / velocity * 100 : null;

    const parts = [];
    /* Der Verbrauch der Fahrt zuerst: Das ist die Zahl, wegen der man
     * aufzeichnet. Die momentane Leistung darunter ist Beiwerk - und beim
     * ID.Buzz ohnehin nicht zu haben, weil der Batteriestrom nicht
     * antwortet. */
    const so_far = runningConsumption(raw, z && z.actual_soc);
    if (so_far) {
      parts.push(`<b>${K.num(so_far.kwh100, 1)}</b> kWh/100 auf `
                 + `${K.num(so_far.km, 0)} km`);
    }
    if (current !== null) {
      parts.push(`<b>${K.num(current, 1)}</b> kWh/100 gerade`);
    }
    if (kw !== null) {
      parts.push(`<b>${K.num(Math.abs(kw), 0)}</b> kW${kw < 0 ? " zurück" : ""}`);
    }
    if (typeof raw.aux_load_kw === "number") {
      let n = `<b>${K.num(raw.aux_load_kw, 1)}</b> kW Nebenverbraucher`;
      if (typeof raw.ptc_current_a === "number"
          && typeof raw.voltage_v === "number") {
        n += `, davon <b>${K.num(raw.ptc_current_a * raw.voltage_v / 1000, 1)}</b> Heizung`;
      }
      parts.push(n);
    } else if (aux_load) {
      const age = Math.round((Date.now() - aux_load.timestamp) / 60000);
      parts.push(`<b>${K.num(aux_load.kw, 1)}</b> kW Nebenverbraucher`
                 + ` (im Stand${age > 0 ? `, vor ${age} min` : ""})`);
    }
    /* Aussentemperatur und Kilometerstand standen hier auch. Sie sind
     * richtig und interessant, aber nicht **im Fahren** - und eine Zeile
     * mit sechs Angaben liest man gar nicht mehr. Beide stehen in der
     * Tabelle unter der Klappe, wo man sie sucht, wenn man sie sucht. */

    // Zeile und Tabelle stehen jetzt an verschiedenen Stellen: die Zeile
    // oben bei den Kacheln, die Tabelle unten hinter der Klappe.
    document.getElementById("live-auto-zeile").innerHTML = parts.join(" · ");
    const flap = document.getElementById("live-roh");
    if (flap) {
      flap.hidden = false;
      document.getElementById("live-auto-werte").innerHTML =
        rawValuesTable(raw);
    }

    /* Wie alt der letzte Satz ist - die Frage, die man am Steuer wirklich
     * hat. "3 s" heisst, der Dongle antwortet; "4 min" heisst, er ist weg,
     * und die Zahlen darunter sind Erinnerungen. Ohne diese Angabe sieht
     * eine eingefrorene Anzeige genauso aus wie eine laufende. */
    const age = latestRawValuesTime
      ? Math.round((Date.now() - latestRawValuesTime) / 1000) : null;
    const as_of = document.getElementById("live-auto-stand");
    if (age === null) {
      as_of.textContent = "";
    } else if (age < 90) {
      as_of.textContent = `vor ${age} s`;
      as_of.style.color = "";
    } else {
      as_of.textContent = `seit ${K.duration(age / 60)} keine Antwort`;
      as_of.style.color = "#e8804f";
      /* Einmal deutlich sagen, dass nichts mehr aus dem Auto kommt.
       *
       * Die blasse Zeile hinter der Klappe reicht dafür nicht. Auf einer
       * echten Fahrt sind so zwanzig Kilometer ohne einen einzigen
       * Fahrzeugwert aufgezeichnet worden - GPS lief weiter, der Dongle
       * war weg, und gemerkt hat es niemand. Eine Aufzeichnung ohne
       * Ladestand ist für das Lernen wertlos, und das erfährt man sonst
       * erst hinterher.
       *
       * Drei Minuten, nicht neunzig Sekunden: Ein Tunnel oder eine kurze
       * Sperre soll nicht melden, ein abgerissener Dongle schon. */
      if (!quietReported && age > 180) {
        quietReported = true;
        K.report("Seit drei Minuten kommt nichts mehr aus dem Auto. jolt "
          + "zeichnet die Strecke weiter auf, aber ohne Ladestand – zum "
          + "Lernen taugt sie dann nicht. jolt in den Vordergrund holen, "
          + "dann verbindet sich der Dongle von selbst wieder.", "fehler");
      }
    }
  }

  /* Alles, was das Auto liefert - als Tabelle, nicht als Satz.
   *
   * Die Zeile darüber beantwortet "wie läuft es gerade". Diese Tabelle
   * beantwortet die andere Frage: "kommt überhaupt an, was ankommen soll".
   * Dafür muss auch dastehen, was **nicht** geantwortet hat - ein fehlender
   * Wert ist beim Einrichten die interessantere Information als ein
   * vorhandener, und ein Zähler ("3 Werte antworten nicht") sagt nicht,
   * welche drei.
   *
   * Die Liste kommt aus `joltObd.FELDER`, damit eine neue Datenkennung hier
   * von selbst auftaucht und nicht an zwei Stellen gepflegt werden muss. */
  function valuesRemember(raw) {
    const now_ts = Date.now();
    // Für den Verbrauchsplot: Kilometerstand und Ladestand mit Zeitstempel.
    // Die Leistung steht bewusst nicht dabei - siehe verbrauchsabschnitte().
    if (typeof raw.odometer_km === "number") {
      /* `netto` ist der Zählerstand: entladen minus geladen. Seine
       * Differenz über ein Stück Fahrt **ist** die verbrauchte Energie -
       * ohne Umweg über Ladestand und Akkugrösse, und mit 0,117 Wh
       * Auflösung statt 339. */
      const net = (typeof raw.discharge_kwh === "number")
        ? raw.discharge_kwh - (typeof raw.charged_kwh === "number"
                              ? raw.charged_kwh : 0)
        : null;
      consumption_track.push({
        timestamp: now_ts, km: raw.odometer_km, net,
        // Die beiden Zähler einzeln, für die Rekuperation der Anzeige.
        disch: typeof raw.discharge_kwh === "number" ? raw.discharge_kwh : null,
        chg: typeof raw.charged_kwh === "number" ? raw.charged_kwh : null,
        // Die GPS-Strecke wird in `zustandAnzeigen` nachgetragen, sobald
        // die Position dieses Punktes bekannt ist.
        gps: null,
        soc: typeof raw.soc_raw === "number" ? raw.soc_raw / 2.5 : null });
      // Grosszuegig: 20 000 Punkte sind bei Zwoelf-Sekunden-Takt rund
      // 66 Stunden. Bei 3000 waeren nach zehn Stunden die ersten Punkte
      // herausgefallen - und mit ihnen der Anfang der Fahrt.
      if (consumption_track.length > 20000) consumption_track.shift();
    }
    for (const [name, val] of Object.entries(raw)) {
      if (typeof val === "number") {
        valuesAsOf[name] = { val, timestamp: now_ts };
        neverCome.delete(name);
      }
    }
    // "Geantwortet, aber ohne brauchbaren Wert" heisst: Die Datenkennung
    // passt für dieses Fahrzeug nicht. Das bleibt so, bis doch einmal ein
    // Wert kommt - deshalb gemerkt und nicht je Runde neu entschieden.
    for (const name of raw._empty || []) {
      if (!valuesAsOf[name]) neverCome.add(name);
    }
  }

  /* Der Verbrauch der laufenden Fahrt in kWh/100 km.
   *
   * **Warum gerechnet und nicht gelesen.** Die MEB-Liste kennt keinen
   * Parameter dafür; das Auto zeigt den Wert im Bordcomputer, gibt ihn aber
   * nicht über die Diagnose heraus. Er entsteht hier aus zwei Grössen, die
   * beide jede Runde ankommen:
   *
   *   verbrauchte kWh = (Ladestand am Anfang − jetzt) / 100 × Akku netto
   *   gefahrene km    = Kilometerstand jetzt − am Anfang
   *
   * **Der Kilometerstand und nicht das GPS.** Er zählt Radumdrehungen und
   * kennt weder abgeschnittene Kurven noch Funklöcher - für eine Grösse mit
   * der Strecke im Nenner ist das der Unterschied zwischen brauchbar und
   * irreführend.
   *
   * Er löst allerdings in ganzen Kilometern auf. Unter fünf gefahrenen
   * Kilometern kommt deshalb nichts: Bei zwei Kilometern wäre die Angabe
   * auf ±50 % genau und damit schlimmer als keine.
   */
  const CONSUMPTION_FROM_KM = 5.0;
  /* Ab wie viel Energie **hinein** bei stehendem Auto es ein Ladevorgang
   * ist. Rekuperation gibt es nur in Fahrt; wer steht und trotzdem Energie
   * aufnimmt, hängt am Kabel. */
  const CHARGING_AS_OF_KWH = 0.05;

  function runningConsumption(raw, soc) {
    const fz = K.state.recVehicle
      || (K.state.trip && K.state.trip.vehicle);
    const battery = fz && (fz.capacity_kwh || fz.battery_net_kwh);
    if (consumption_track.length < 2) return null;

    /* **Aufsummiert statt Anfang gegen Ende.**
     *
     * Hier stand `netto - anfang.netto`, und das ist auf einer kurzen Fahrt
     * richtig. Auf einer **langen** nicht: Der Zähler `geladen` wächst auch
     * an der Ladesäule. Wer vierzig Kilowattstunden nachlädt, dessen
     * Nettozähler fällt um vierzig - der angezeigte Verbrauch der Fahrt
     * wäre danach nahe null oder negativ.
     *
     * Also abschnittsweise, und Ladevorgänge fallen heraus: Energie hinein
     * bei stehendem Auto ist keine Rekuperation, sondern das Kabel. Alles
     * andere zählt mit, auch der Verbrauch im Stand - der ist echt.
     */
    let kwh = 0, km = 0;
    for (let i = 1; i < consumption_track.length; i++) {
      const a = consumption_track[i - 1], b = consumption_track[i];
      const dkm = (b.km ?? 0) - (a.km ?? 0);
      let d = null;
      if (a.net !== null && b.net !== null) d = b.net - a.net;
      else if (battery && a.soc !== null && b.soc !== null) {
        d = (a.soc - b.soc) / 100 * battery;
      }
      if (d === null) continue;
      if (dkm <= 0 && d < -CHARGING_AS_OF_KWH) continue;   // an der Säule
      kwh += d;
      km += Math.max(0, dkm);
    }
    if (km < CONSUMPTION_FROM_KM) return null;
    return { kwh100: kwh / km * 100, kwh, km };
  }

  /* ---------- Verbrauch je Zeitabschnitt ---------- */

  /* **Woher die Energie kommt, und was das fuer die Balkenbreite heisst.**
   *
   * Erster Entwurf: Leistung (Spannung mal Strom) ueber die Zeit
   * aufsummieren. Falsch - der Strom aendert sich im Sekundentakt,
   * gemeldet wird alle zwoelf Sekunden. Fuenf Stichproben je Minute sind
   * kein Integral, sondern eine Umfrage; auf der Landstrasse 25 % Fehler,
   * in der Stadt 59 %.
   *
   * Zweiter Entwurf: aus dem **Ladestand**. Sein Quantisierungsfehler ist
   * absolut begrenzt (ein Schritt, 0,44 pp = 339 Wh), also relativ umso
   * kleiner, je laenger der Abschnitt - ab fuenf Minuten ueberall unter
   * 3 %. Aber eben erst ab fuenf Minuten.
   *
   * Jetzt: die **Energiezaehler** des Fahrzeugs. Ihre Differenz ist die
   * verbrauchte Energie, mit 0,117 Wh Auflösung - fast dreitausendmal
   * feiner als der Ladestand. Damit ist ein Balken je Minute keine
   * Schaetzung mehr, sondern eine Messung (0,05 % statt 136 %).
   *
   * Die Balkenbreite folgt deshalb der Quelle, und nicht dem Wunsch:
   * eine Minute mit Zaehler, fuenf ohne.
   */
  const SECTION_WITH_COUNTER_S = 60;
  const SECTION_FROM_SOC_S = 300;
  /* Bei einer langen Fahrt wird die Minute zu fein.
   *
   * Ein Balken je Minute ist auf einer halben Stunde genau richtig - auf
   * sechs Stunden waeren es 360 Balken auf rund 340 Pixeln, also 0,9 Pixel
   * je Balken. Das ist kein Diagramm mehr, sondern eine Textur.
   *
   * Deshalb waechst die Abschnittsbreite mit der Fahrt, aber nur auf runde
   * Werte: zwei Minuten liest man noch als zwei Minuten, 87 Sekunden nicht.
   * Die Genauigkeit leidet dabei nicht - mit den Zaehlern ist schon die
   * Minute weit ueber der Aufloesungsgrenze, breiter wird nur besser. */
  const WIDTHS_MIN = [1, 2, 5, 10, 15, 30, 60];
  const BAR_AT_MOST = 60;

  function widthChoose(duration_ms, min_s) {
    for (const min of WIDTHS_MIN) {
      if (min * 60 < min_s) continue;
      if (duration_ms / (min * 60000) <= BAR_AT_MOST) return min * 60000;
    }
    return WIDTHS_MIN[WIDTHS_MIN.length - 1] * 60000;
  }
  // Unter dieser Strecke ist kWh/100 km nicht sinnvoll - das Auto stand.
  const BAR_MIN_KM = 0.3;

  /* **Die Strecke je Balken kommt aus dem GPS, die Energie aus den Zählern.**
   *
   * Das klingt nach einem Rückschritt - für die **Gesamtstrecke** einer
   * Aufzeichnung ist der Kilometerstand ja gerade die bessere Quelle, weil
   * er weder Kurven abschneidet noch Funklöcher kennt. Über eine einzelne
   * Minute kehrt sich das um: Er löst in ganzen Kilometern auf, und eine
   * Minute bei siebzig km/h sind 1,2 km. Gemessen wird dann 1 oder 2 -
   * vierzig Prozent Fehler auf den Nenner. Die GPS-Spur schneidet bei einer
   * Meldung alle zwölf Sekunden nur wenige Prozent ab.
   *
   * Aufgefallen an einer echten Fahrt: Die Kilometerspalte je Minute stand
   * durchgehend auf 0 oder 1, und die Balken schwankten entsprechend.
   *
   * Die Energie bleibt bei den Zählern - dort ist die Auflösung 0,117 Wh
   * und damit kein Thema. Jede Grösse aus der Quelle, die sie am besten
   * kennt. */
  function consumption_sections() {
    const points = consumption_track.filter((p) => typeof p.gps === "number");
    if (points.length < 2) return null;
    const withCounter = points.every((p) => typeof p.net === "number");
    const fz = K.state.recVehicle
      || (K.state.trip && K.state.trip.vehicle) || {};
    const battery = fz.capacity_kwh || fz.battery_net_kwh;
    if (!withCounter && !battery) return null;

    const onset = points[0].timestamp;
    const extent = widthChoose(
      points[points.length - 1].timestamp - onset,
      withCounter ? SECTION_WITH_COUNTER_S : SECTION_FROM_SOC_S);
    const eimer = new Map();
    for (const p of points) {
      const n = Math.floor((p.timestamp - onset) / extent);
      if (!eimer.has(n)) eimer.set(n, []);
      eimer.get(n).push(p);
    }

    const bar = [];
    for (const [n, group] of [...eimer.entries()].sort((a, b) => a[0] - b[0])) {
      if (group.length < 2) continue;
      const at_first = group[0], final = group[group.length - 1];
      const km = final.gps - at_first.gps;
      if (km < BAR_MIN_KM) continue;
      const kwh = withCounter ? (final.net - at_first.net)
        : ((at_first.soc !== null && final.soc !== null)
           ? (at_first.soc - final.soc) / 100 * battery : null);
      if (kwh === null || !Number.isFinite(kwh)) continue;
      bar.push({ n, kwh100: kwh / km * 100, km });
    }
    return bar.length ? { bar, extent, withCounter } : null;
  }

  function drawConsumption() {
    const canvas = document.getElementById("live-verbrauch");
    const foot = document.getElementById("live-verbrauch-fuss");
    if (!canvas || !foot) return;
    const records = consumption_sections();
    if (!records) { canvas.hidden = true; foot.hidden = true; return; }
    canvas.hidden = false; foot.hidden = false;

    const dpr = window.devicePixelRatio || 1;
    const extent = canvas.clientWidth, elevation = canvas.clientHeight;
    if (!extent || !elevation) return;
    canvas.width = extent * dpr;
    canvas.height = elevation * dpr;
    const pen = canvas.getContext("2d");
    pen.setTransform(dpr, 0, 0, dpr, 0, 0);
    pen.clearRect(0, 0, extent, elevation);

    const vals = records.bar.map((b) => b.kwh100);
    // Die Skala nach oben grosszuegig, damit ein Ausreisser die uebrigen
    // Balken nicht platt drueckt, und mit Nulllinie: Rekuperation geht
    // unter null, und genau das soll man sehen.
    const upper = Math.max(40, ...vals) * 1.1;
    const bottom = Math.min(0, ...vals) * 1.1;
    const span = upper - bottom || 1;
    const edge = 6, footElevation = 16;
    const area = elevation - footElevation - edge;
    const y = (v) => edge + (upper - v) / span * area;

    /* Beschriftete Achse. Ohne sie ist ein Balkendiagramm eine Form ohne
     * Aussage - man sieht, dass eine Minute teurer war als die andere, aber
     * nicht, ob es um zwanzig oder um vierzig kWh/100 km geht. Und genau
     * das ist die Zahl, die man mit dem eigenen Gefühl vergleicht.
     *
     * Beschriftet wird links, in die Fläche hinein: Eine eigene Spalte
     * dafür wäre auf dem Telefon zu teuer. Drei Linien reichen - null, ein
     * runder Wert dazwischen und das Maximum. */
    const axis = 30;
    const split = [0];
    const step = upper > 60 ? 25 : (upper > 25 ? 10 : 5);
    for (let w = step; w < upper; w += step) split.push(w);
    for (let w = -step; w > bottom; w -= step) split.push(w);

    pen.font = "10px system-ui, sans-serif";
    pen.textBaseline = "middle";
    for (const w of split) {
      const yy = y(w);
      pen.strokeStyle = w === 0 ? "#3a4652" : "#222c36";
      pen.lineWidth = 1;
      pen.beginPath();
      pen.moveTo(axis, yy); pen.lineTo(extent - edge, yy);
      pen.stroke();
      pen.fillStyle = "#8a97a5";
      pen.textAlign = "right";
      pen.fillText(String(w), axis - 4, yy);
    }
    // Die Einheit einmal oben links, nicht an jeden Strich.
    pen.fillStyle = "#8a97a5";
    pen.textAlign = "left";
    pen.fillText("kWh/100", axis + 3, edge + 4);

    const field = extent - edge - axis;
    const b = Math.max(2, field / records.bar.length - 2);
    records.bar.forEach((bar, i) => {
      const x = axis + i * (field / records.bar.length);
      const high = y(bar.kwh100) - y(0);
      // Farbe nach Höhe: was deutlich über dem Schnitt liegt, fällt auf.
      pen.fillStyle = bar.kwh100 < 0 ? "#57c98a"
        : (bar.kwh100 > 35 ? "#e8804f" : "#ffc93c");
      pen.fillRect(x, high < 0 ? y(bar.kwh100) : y(0),
                     b, Math.max(1, Math.abs(high)));
    });

    const average = vals.reduce((a, v) => a + v, 0) / vals.length;
    foot.children[0].textContent =
      `Verbrauch je ${records.extent / 60000} min`
      + (records.withCounter ? "" : " (aus dem Ladestand)");
    foot.children[1].textContent = `Ø ${K.num(average, 1)} kWh/100`;
  }

  function ageText(seconds) {
    if (seconds < 60) return `vor ${Math.round(seconds)} s`;
    return `vor ${Math.round(seconds / 60)} min`;
  }

  function rawValuesTable(raw) {
    const fields = (window.joltObd && window.joltObd.FIELDS) || [];
    if (!fields.length) return "";
    const missing = new Set(raw._missing || []);
    const now_ts = Date.now();

    const rows = fields.map((f) => {
      const as_of = valuesAsOf[f.name];
      if (!as_of) {
        // Noch nie ein Wert. Der Grund unterscheidet sich, und der
        // Unterschied ist beim Einrichten die eigentliche Information.
        const reason = neverCome.has(f.name) ? "antwortet nicht"
          : (missing.has(f.name) ? "keine Antwort" : "–");
        return `<tr class="leer"><th>${f.title}</th><td>${reason}</td></tr>`;
      }
      const age = (now_ts - as_of.timestamp) / 1000;
      const num = K.num(as_of.val, f.put)
        + (f.unit ? " " + f.unit : "");
      // Frisch heisst: in dieser Runde gekommen. Alles andere bekommt sein
      // Alter danebengeschrieben und wird blasser, je älter es ist - so
      // sieht man auf einen Blick, welche Zeile noch lebt.
      if (typeof raw[f.name] === "number") {
        return `<tr><th>${f.title}</th><td>${num}</td></tr>`;
      }
      const category = age > 120 ? "alt sehr" : "alt";
      return `<tr class="${category}"><th>${f.title}</th>`
        + `<td>${num}<span class="wann">${ageText(age)}</span></td></tr>`;
    });
    return `<table class="rohwerte"><tbody>${rows.join("")}</tbody></table>`;
  }

  /* Werte, die der Server annimmt - sonst keine.
   *
   * Der Server lehnt seit der Absicherung (Bug-Scan #49) Unmögliches mit 422
   * ab: Tempo ausserhalb 0 bis 500 km/h, Aussentemperatur ausserhalb -80 bis
   * 70 °C. Das Auto liefert dafür Anlass genug: Das Tempo-Byte steht bei
   * "ungültig" auf 255 (in den gespeicherten Fahrten kommt das vor), und die
   * Aussentemperatur `b0 / 2 - 50` ergibt bei 0xFF 77,5 °C. Ein solcher Wert
   * darf nicht den Punkt kosten, zu dem er gehört - und erst recht nicht den
   * Stapel. Hier wird er zu "nicht gemessen".
   *
   * 250 km/h statt 500: 255 ist der Platzhalter des Autos, und kein
   * Fahrzeug, das jolt kennt, fährt 250. */
  function speedOrNull(val) {
    return (typeof val === "number" && Number.isFinite(val)
            && val >= 0 && val <= 250) ? val : null;
  }

  function temperatureOrNull(val) {
    return (typeof val === "number" && Number.isFinite(val)
            && val >= -80 && val <= 70) ? val : null;
  }

  async function reportPosition(coords, timeMs) {
    if (!K.state.sessionId) return;
    const payload = {
      lat: coords.latitude, lon: coords.longitude,
      // Aus dem Fix: m/s, und -1 oder null, wenn das Gerät es nicht weiss.
      speed_kmh: speedOrNull(typeof coords.speed === "number"
                               ? coords.speed * 3.6 : null),
      // Wann gemessen wurde, nicht wann es ankommt - sonst wären alle
      // nachgereichten Punkte aus einem Funkloch auf dieselbe Sekunde datiert.
      timestamp: new Date(timeMs || Date.now()).toISOString(),
    };

    if (dongle && readAllowed() && window.joltObd
        && window.joltObd.linked()) {
      try {
        const raw = await window.joltObd.readRecord(lap++);
        // Die 12-V-Spannung mitschreiben, solange sie frisch ist: Daran
        // lässt sich später nachsehen, wie weit sie beim Ausschalten fällt.
        if (latestVoltage !== null && Date.now() - latestVoltageTime < 15000) {
          raw.batt_v = latestVoltage;
        }
        if (typeof raw.elevation_m !== "number" && typeof coords.altitude === "number") {
          raw.elevation_m = Math.round(coords.altitude);
        }
        latestRawValues = raw;
        latestRawValuesTime = Date.now();
        quietReported = false;
        valuesRemember(raw);
        auxLoadRemember(raw);
        const val = window.joltObd.socFromRaw(raw.soc_raw);
        payload.soc = Math.round(val.hmi * 10) / 10;
        payload.raw_values = raw;
        // Was das Auto selbst misst, schlägt jede Vorhersage - wenn es
        // plausibel ist. Ein "ungültig" (255 km/h, 77,5 °C) lässt das Tempo des
        // GPS stehen und die Temperatur leer.
        const autoSpeed = speedOrNull(raw.speed_kmh);
        if (autoSpeed !== null) payload.speed_kmh = autoSpeed;
        const autoTemp = temperatureOrNull(raw.outside_temp_c);
        if (autoTemp !== null) payload.outside_temp_c = autoTemp;
      } catch (failure) {
        // Eine Runde ohne Ladestand ist immer noch eine Positionsmeldung -
        // und die trägt Zeitfaktor und Ankunftsprognose weiter.
        console.log("[obd] Runde übersprungen:", failure);
      }
      quietWatch();
    }

    // Die Spannung auch ohne Fahrzeugabfrage mitgeben. Im Stand wird nichts
    // gefragt, `ATRV` aber weiter gemessen - und gerade dort fällt sie, wenn
    // das Auto ausgeht. Ohne diese Zeile stünde der Abfall nirgends, und die
    // Schwelle liesse sich nicht an einer echten Fahrt prüfen.
    if (!payload.raw_values && latestVoltage !== null
        && Date.now() - latestVoltageTime < 15000) {
      payload.raw_values = { batt_v: latestVoltage };
    }
    bufferAppend(payload);
    bufferProcess();
  }

  /* ---------- Messpunkte puffern ---------- */

  /* Jeder Messpunkt geht zuerst in eine Warteschlange und von dort an den
   * Server - nie direkt. Fällt das Netz aus, bleiben die Punkte liegen und
   * gehen beim nächsten Versuch gesammelt hinaus, mit ihrer Messzeit.
   *
   * Vorher verschluckte `positionMelden` den fehlgeschlagenen POST. Für eine
   * geplante Fahrt ist das harmlos, der nächste Punkt kommt. Für eine
   * Aufzeichnung ist es Datenverlust: Nach zwanzig Minuten ohne Netz fehlten
   * bis zu 13 % der Strecke, und der gelernte Faktor verschob sich um bis zu
   * 35 % - unsichtbar, in einer Zahl, die dauerhaft am Fahrzeug bleibt.
   *
   * Es gibt immer nur **einen** Sendevorgang. Zwei gleichzeitige könnten
   * einander überholen, und der Server bekäme neuere Punkte vor älteren.
   *
   * Die Warteschlange liegt auch im localStorage: Lädt iOS die Seite im
   * Hintergrund neu, sollen die Punkte nicht mit ihr verschwinden. Das Limit
   * schützt vor einem Speicher, der ewig wächst; dann gehen die ältesten. */
  const BUFFER_MAX = 2000;
  const BATCH_MAX = 100;
  // Wie viele Punkte der nächste Stapel hat. Gleich STAPEL_MAX, ausser der
  // Server hat gerade einen Stapel abgelehnt: dann wird halbiert, bis der
  // eine schlechte Punkt feststeht.
  let batchSize = BATCH_MAX;
  let buffer = [];
  let bufferSession = null;
  let bufferRun = null;
  let bufferAgain = false;
  let withoutGridReported = false;
  let supplied_later = 0;
  let rejected = 0;              // vom Server abgelehnte Einzelpunkte

  function bufferStorage(id) { return "jolt-puffer-" + id; }

  function bufferFor(id) {
    if (bufferSession === id) return;
    bufferSession = id;
    buffer = [];
    try {
      const raw = JSON.parse(localStorage.getItem(bufferStorage(id)) || "[]");
      if (Array.isArray(raw)) buffer = raw;
    } catch (e) { /* kein Speicher oder beschädigt: ohne weiter */ }
  }

  function saveBuffer() {
    if (bufferSession === null) return;
    try {
      if (buffer.length) {
        localStorage.setItem(bufferStorage(bufferSession), JSON.stringify(buffer));
      } else {
        localStorage.removeItem(bufferStorage(bufferSession));
      }
    } catch (e) { /* voll oder gesperrt: dann bleibt er eben im Arbeitsspeicher */ }
  }

  function bufferAppend(point) {
    bufferFor(K.state.sessionId);
    buffer.push(point);
    if (buffer.length > BUFFER_MAX) buffer.splice(0, buffer.length - BUFFER_MAX);
    // Gesichert wird nur, wenn sich etwas staut - im Normalfall steht der
    // Punkt eine Sekunde später auf dem Server.
    if (buffer.length > 1) saveBuffer();
  }

  function bufferProcess() {
    if (bufferRun) { bufferAgain = true; return bufferRun; }
    bufferRun = sendBuffer().finally(() => { bufferRun = null; });
    return bufferRun;
  }

  async function sendBuffer() {
    do {
      bufferAgain = false;
      const id = K.state.sessionId;
      while (id && buffer.length && K.state.sessionId === id) {
        const batch = buffer.slice(0, batchSize);
        let state;
        try {
          state = await K.api(`/api/live/${id}/punkte`,
            { method: "POST", body: { points: batch } });
        } catch (failure) {
          const status = failure.status;
          if (status === 404 || status === 409) {
            // Die Sitzung gibt es nicht mehr oder ist beendet: Weiter zu
            // senden hiesse, dieselbe Ablehnung bis in alle Ewigkeit zu holen.
            buffer = [];
          } else if (status === 422) {
            // Der Server hält den Stapel für ungültig - ein Punkt darin, etwa
            // mit einem Zeitstempel von vor mehr als zwei Tagen. Der ganze
            // Stapel würde nie angenommen und verstopfte alles dahinter.
            //
            // Hier wurde früher der Stapel verworfen: wegen eines schlechten
            // Punktes bis zu neunundneunzig gute. Jetzt wird halbiert, bis der
            // eine Punkt feststeht; nur der fliegt raus. Das kostet bei hundert
            // Punkten höchstens sieben weitere Anfragen.
            if (batch.length > 1) {
              batchSize = Math.ceil(batch.length / 2);
            } else {
              buffer.splice(0, 1);
              batchSize = BATCH_MAX;
              rejected += 1;
            }
          } else if (!withoutGridReported) {
            withoutGridReported = true;
            K.report("Keine Verbindung zu jolt – die Messpunkte werden "
              + "gesammelt und nachgereicht.", "hinweis");
          }
          saveBuffer();
          // Netz weg, Server überlastet oder abgemeldet: liegen lassen, der
          // nächste Punkt oder das Zurückkehren des Netzes versucht es erneut.
          if (status !== 404 && status !== 409 && status !== 422) return;
          continue;
        }
        buffer.splice(0, batch.length);
        saveBuffer();
        if (withoutGridReported) supplied_later += batch.length;
        // Den Zustand nur zeigen, wenn der Stapel die Gegenwart erreicht hat.
        // Mitten im Nachreichen wäre es der von vor zehn Minuten.
        if (!buffer.length) {
          if (withoutGridReported) {
            withoutGridReported = false;
            K.report("Verbindung wieder da – " + supplied_later
              + " Messpunkte nachgereicht.", "hinweis");
            supplied_later = 0;
          }
          showState(state);
        }
      }
    } while (bufferAgain);
    // Nach einer Ablehnung geht es in kleinen Stapeln weiter, bis der schlechte
    // Punkt gefunden ist (dann ist die Grösse wieder voll) - oder bis nichts
    // mehr wartet. Ein Wachsen nach jedem Erfolg träfe den schlechten Punkt
    // immer wieder: Das kostete bei 13 Punkten sieben abgelehnte Anfragen
    // statt vier.
    batchSize = BATCH_MAX;
    if (rejected > 0) {
      K.report(`${rejected} Messpunkt${rejected === 1 ? " wurde" : "e wurden"} `
        + "vom Server abgelehnt und verworfen - die übrigen sind angekommen.",
        "hinweis");
      rejected = 0;
    }
  }

  window.addEventListener("online", () => {
    if (K.state.sessionId) bufferProcess();
  });

  function fetchLocation() {
    return new Promise((fulfil, reject) => {
      if (!navigator.geolocation) {
        reject(new Error("Dieses Gerät liefert keinen Standort."));
        return;
      }
      navigator.geolocation.getCurrentPosition(
        (pos) => fulfil({ lat: pos.coords.latitude, lon: pos.coords.longitude,
                             speed_kmh: pos.coords.speed === null ? null
                               : pos.coords.speed * 3.6 }),
        // Ohne Standort ist der Ladestand allein wertlos: Erst die Position
        // sagt, mit welchem Sollwert er zu vergleichen ist.
        (failure) => reject(new Error("Standort nicht verfügbar ("
          + failure.message + "). Über HTTPS oder localhost erlaubt der "
          + "Browser den Zugriff.")),
        { enableHighAccuracy: true, timeout: 10000, maximumAge: 5000 });
    });
  }

  /* Den zuletzt bekannten Ladestand ins Feld schreiben - aber nie, während
   * jemand darin tippt. Am Ladepunkt wird der Wert eingetippt, und ein Feld,
   * das sich beim Eintippen unter den Fingern ändert, weil gerade eine
   * Nachricht über den WebSocket kam, ist schlimmer als ein leeres. */
  function socFieldPrefill(val) {
    const field = document.getElementById("ist-soc");
    if (!field || document.activeElement === field) return;
    if (val === null || val === undefined || Number.isNaN(val)) return;
    field.value = Math.round(val);
  }

  async function reportSoc() {
    if (!K.state.sessionId) { K.report("Keine Live-Fahrt.", "fehler"); return; }
    const btn = document.getElementById("soc-melden");
    const field = document.getElementById("ist-soc");
    const soc = Number(field.value);
    if (!field.value || !(soc >= 0 && soc <= 100)) {
      K.report("Ladestand zwischen 0 und 100 % angeben.", "fehler");
      return;
    }

    btn.disabled = true;
    // Die Tastatur weg, sonst verdeckt sie auf dem Telefon genau die Werte,
    // wegen derer man den Ladestand gerade gemeldet hat.
    field.blur();
    try {
      const city = await fetchLocation();
      const state = await K.api(`/api/live/${K.state.sessionId}/punkt`,
        { method: "POST", body: { lat: city.lat, lon: city.lon, soc: soc,
                                  speed_kmh: city.speed_kmh } });
      showState(state);
      // Die Abweichung ist der Grund, warum das Eintippen sich lohnt - also
      // gehört sie unmittelbar danach als Satz auf den Schirm und nicht nur
      // als Kachel unter fünf anderen.
      const explanation = document.getElementById("soc-erklaerung");
      if (explanation) {
        explanation.textContent = state.deviation_pp === null
          || state.deviation_pp === undefined
          ? "Aufgenommen."
          : (Math.abs(state.deviation_pp) < 0.5
            ? "Aufgenommen – genau im Plan."
            : `Aufgenommen – ${K.num(Math.abs(state.deviation_pp), 1)} `
              + `Prozentpunkte ${state.deviation_pp < 0 ? "unter" : "über"} Plan.`);
      }
    } catch (failure) {
      K.report(failure.message, "fehler");
    } finally {
      btn.disabled = false;
    }
  }

  async function finish() {
    if (!K.state.sessionId) return;
    // Was noch in der Warteschlange liegt, gehört zur Fahrt - und nach dem
    // Beenden nimmt der Server nichts mehr an.
    bufferFor(K.state.sessionId);
    if (buffer.length) await bufferProcess();
    if (buffer.length) {
      K.report(buffer.length + " Messpunkte konnten nicht mehr übertragen "
        + "werden - kein Netz.", "warnung");
    }
    let result = null;
    try {
      result = await K.api(`/api/live/${K.state.sessionId}/ende`,
                             { method: "POST" });
    } catch (failure) { /* eine bereits beendete Fahrt ist kein Problem */ }
    // Die Fahrten-Ansicht hat die Liste zwischengespeichert; eine gerade
    // beendete Fahrt gehört hinein.
    K.state.tripsStale = true;
    positionGiveUp();
    buffer = [];
    saveBuffer();
    bufferSession = null;
    withoutGridReported = false;
    supplied_later = 0;
    dongle = false;
    track = [];
    drivenKm = 0;
    history = [];
    latestRawValues = null;
    latestRawValuesTime = 0;
    quietReported = false;
    valuesAsOf = {};
    neverCome = new Set();
    consumptionStart = null;
    consumption_track = [];
    aux_load = null;
    if (reconnectClock) { clearTimeout(reconnectClock); reconnectClock = null; }
    if (socket) {
      try { socket.onclose = null; socket.close(); } catch (e) {}
    }
    K.state.sessionId = null;
    K.sessionRemember(null);
    drivingStateStart("steht");
    // Die Fahrt ist zu Ende: Die Anzeige im Auto soll verschwinden.
    try { if (window.joltDisplay) window.joltDisplay.finish(); }
    catch (e) { console.log("[anzeige]", e && e.message); }
    plan = null;
    const box = document.getElementById("live-aenderung");
    if (box) box.hidden = true;
    document.getElementById("live-inhalt").hidden = true;
    document.getElementById("live-leer").hidden = false;
    reportLearned(result);
    if (result && result.as_of_discarded) {
      K.report(`Die letzten ${result.as_of_discarded.discarded_minutes} min `
        + "Stillstand wurden verworfen - die Fahrt endet beim letzten Fahren.",
        "hinweis");
    }
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
  /* Woher die Höhen kamen - und zwar nur, wenn es nicht die Karte war.
   *
   * Ein Verbrauch ohne Höhenprofil ist nicht deutbar: Ob 22 kWh/100 km am
   * Fahrstil lagen oder an vierhundert Höhenmetern, lässt sich aus dem
   * Verbrauch allein nicht trennen. Fällt die Kartenabfrage aus, geht die
   * Fahrt trotzdem durch - aber dann soll man es wissen, statt die Zahl
   * später für bare Münze zu nehmen. */
  function reportElevations(built) {
    if (!built || !built.ok) return;
    if (built.elevations === "gps") {
      K.report("Die Höhen dieser Fahrt kommen aus dem GPS, nicht aus der "
        + "Karte – geglättet, aber ungenauer. Der gelernte Faktor ist "
        + "entsprechend weicher.", "hinweis");
    } else if (built.elevations === "flach") {
      K.report("Für diese Fahrt gab es keine Höhendaten; sie wurde flach "
        + "gerechnet. Auf einer Runde macht das wenig aus, auf einer Fahrt "
        + "ins Gebirge viel.", "hinweis");
    }
  }

  function reportLearned(result) {
    if (!result) {
      K.report("Live-Fahrt beendet.", "hinweis");
      return;
    }
    reportElevations(result.recording);
    const g = result.learned;
    if (!g && result.not_learned) {
      // Der Zuschlag für Träger oder Box ist kein Fehler, sondern der Grund,
      // warum diese Fahrt bewusst nicht in den Fahrzeugfaktor eingeht.
      K.report("Fahrt beendet. " + result.not_learned
        + " Die Aufzeichnung bleibt erhalten.", "hinweis");
      return;
    }
    if (!g) {
      K.report("Fahrt beendet. Für die Kalibrierung war sie nicht verwertbar "
        + "– unter 30 km, oder der gemessene Verbrauch lag ausserhalb des "
        + "Plausiblen.", "hinweis");
      return;
    }
    const direction = g.after > g.earlier ? "mehr" : "weniger";
    K.report(`Gelernt: Diese Fahrt brauchte ${K.num(g.raw_factor, 2)}× so viel `
      + `wie gerechnet. Der Korrekturfaktor des Fahrzeugs geht von `
      + `${K.num(g.earlier, 3)} auf ${K.num(g.after, 3)} – künftige `
      + `Planungen rechnen also ${direction}.`, "hinweis");
  }

  /* Nach einem Neuladen dort weitermachen, wo es aufhörte.
   *
   * Der Server weiss, ob die Sitzung noch läuft - er ist die Wahrheit, nicht
   * der Browser. Läuft sie, wird die Ansicht wiederhergestellt und weiter
   * gemeldet; ist sie beendet, wird die Marke stillschweigend verworfen.
   *
   * Ohne Rückfrage: Wer versehentlich neu lädt, will nicht gefragt werden,
   * ob er weitermachen möchte - er will, dass es weitergeht. */
  /* Wieviel Anläufe das Fortsetzen nimmt, bevor es aufgibt.
   *
   * Der Auslöser stand früher an einem Zeitgeber von 800 ms, mit dem
   * Kommentar "erst wenn die Fahrzeugliste steht". Eine geratene Zahl: Auf
   * einem kalt gestarteten Telefon im französischen Funkloch ist sie zu
   * kurz, und es gab keinen zweiten Versuch. */
  const RESUME_ATTEMPTS = 6;

  /* Spur, Ladestandskurve und Verbrauchsbalken aus den gespeicherten
   * Messpunkten wieder aufbauen.
   *
   * Der Zustand kennt nur den letzten Punkt. Ohne dieses Nachladen fing
   * nach einem Neuladen alles bei null an: leere Karte, leeres
   * Balkendiagramm, eine Kurve ab dem aktuellen Kilometer. Die Fahrt lief
   * weiter, sah aber aus wie neu - und wer sie für verloren hielt, plante
   * eine neue, die dann die laufende beendete.
   *
   * Bewusst nicht über `zustandAnzeigen()`: Das würde je Punkt die Karte
   * neu setzen, Kacheln schreiben und Meldungen auslösen. Hier wird nur
   * der Zustand aufgebaut, gezeichnet wird einmal am Ende. */
  async function rechargeHistory(id) {
    let records;
    try {
      records = await K.api(`/api/live/${id}/punkte`);
    } catch (failure) {
      // Kein Grund, das Fortsetzen scheitern zu lassen - die Fahrt läuft
      // auch ohne die Vorgeschichte weiter, sie sieht nur ärmer aus.
      console.log("[live] Verlauf nicht nachgeladen:", failure);
      return;
    }
    const points = (records && records.points) || [];
    if (!points.length) return;

    track = [];
    history = [];
    consumption_track = [];
    drivenKm = 0;

    for (const p of points) {
      if (typeof p.lat === "number" && typeof p.lon === "number") {
        // `spur` hält [lon, lat] - dieselbe Reihenfolge wie `messort()`,
        // und `abstandKm` rechnet damit.
        const city = [p.lon, p.lat];
        const most_recent = track[track.length - 1];
        if (!most_recent || most_recent[0] !== city[0] || most_recent[1] !== city[1]) {
          if (most_recent) drivenKm += spacingKm(most_recent, city);
          track.push(city);
        }
      }
      if (typeof p.odometer_km === "number") {
        const net = (typeof p.discharge_kwh === "number")
          ? p.discharge_kwh - (typeof p.charged_kwh === "number"
                              ? p.charged_kwh : 0)
          : null;
        consumption_track.push({
          timestamp: K.timeMs(p.timestamp), km: p.odometer_km, net,
          // Die GPS-Strecke ist hier schon bekannt - anders als im Betrieb,
          // wo sie erst mit der Position nachgetragen wird.
          gps: drivenKm,
          soc: typeof p.soc_raw === "number" ? p.soc_raw / 2.5 : null });
      }
      if (p.soc !== null && p.soc !== undefined) {
        history.push({
          km: p.km_on_route || 0, driven_km: drivenKm, soc: p.soc,
          /* Ob ein Ladestand gemeldet oder gerechnet war, steht nicht in
           * der Datenbank. Ein Punkt mit Rohwert kam aus dem Auto, das ist
           * sicher eine Messung; ein von Hand eingetippter Wert erscheint
           * hier faelschlich als gerechnet. Lieber so herum: Der Fehler
           * behauptet weniger, als er weiss. */
          reported: p.soc_raw !== null && p.soc_raw !== undefined });
      }
    }
    // Dieselbe Obergrenze wie im Betrieb.
    while (consumption_track.length > 20000) consumption_track.shift();
    while (track.length > 20000) track.shift();

    const last = points[points.length - 1];
    if (typeof last.odometer_km === "number") {
      // Über K.zeit: UTC vom Server. Als Ortszeit gelesen wäre der Wert zwei
      // Stunden alt, und `stilleUeberwachen` baute die Verbindung nach jedem
      // Neuladen der Seite neu auf ("antwortet seit zwei Minuten nicht").
      latestRawValuesTime = K.timeMs(last.timestamp);
    }
    drawHistory();
    drawConsumption();
    if (!K.state.trip && window.joltMap) {
      window.joltMap.setRoute(track);
    }
  }

  async function resumeSession(attempt = 1) {
    const id = K.rememberedSession();
    if (!id || K.state.sessionId) return;
    let state;
    try {
      state = await K.api(`/api/live/${id}`);
    } catch (failure) {
      /* Vergessen darf jolt eine laufende Fahrt nur, wenn der Server
       * eindeutig sagt, dass es sie nicht gibt.
       *
       * Vorher löschte **jeder** Fehler die gemerkte Nummer - und `api()`
       * wirft "Server nicht erreichbar." auch dann, wenn nur das Netz weg
       * ist. Wer die Seite im Tunnel neu lud, verlor die Fahrt endgültig:
       * Sie lief auf dem Server weiter, aber das Telefon bot sie nie
       * wieder an. Am 2. September ist genau das den ganzen Tag passiert -
       * eine Reise von 654 km zerfiel in neun Fahrten, weil nach jedem
       * Neuladen von Hand eine neue geplant werden musste. */
      if (/nicht gefunden|404/i.test(failure.message)) {
        K.sessionRemember(null);
        return;
      }
      if (attempt < RESUME_ATTEMPTS) {
        setTimeout(() => resumeSession(attempt + 1), 4000 * attempt);
      }
      return;
    }
    if (!state || state.running === false) { K.sessionRemember(null); return; }

    K.state.sessionId = id;
    // Nach einem Neuladen ist unbekannt, wo das Auto steht und ob es offen
    // ist. Also erst fragen, wenn gefahren wird.
    drivingStateStart("steht");
    bufferFor(id);
    if (buffer.length) bufferProcess();
    /* Die Fahrt dazuholen. Die Live-Ansicht braucht sie fuer das
     * Energieprofil, die Reserve-Marke und die Soll-Kurve; ohne sie zeigt
     * sie nur die halbe Wahrheit. Bei einer Aufzeichnung gibt es sie noch
     * nicht - dann bleibt es bei null, und die Ansicht kommt damit zurecht. */
    if (!K.state.trip && state.trip_id && window.joltRoute) {
      try { await window.joltRoute.tripCharging(state.trip_id); }
      catch (failure) { /* eine Aufzeichnung hat noch keine Geometrie */ }
    }
    const empty = document.getElementById("live-leer");
    const contents = document.getElementById("live-inhalt");
    if (empty) empty.hidden = true;
    if (contents) contents.hidden = false;
    if (state.plan) { plan = state.plan; drawPlan(); }
    // Vor dem Verbinden: Kommt über den WebSocket sofort ein neuer Punkt,
    // soll er auf den nachgeladenen Verlauf treffen und nicht auf nichts.
    await rechargeHistory(id);
    link(id);
    positionTrace();
    showDongle();
    /* In die Live-Ansicht wechseln, wie es `starten()` auch tut.
     *
     * Ohne das blieb man nach dem Neuladen in der Planen-Ansicht stehen:
     * Die Fahrt lief zwar weiter, war aber nirgends zu sehen. Wer nicht
     * wusste, dass er auf "Live" tippen muss, hielt sie für verloren und
     * plante eine neue - und die beendete dann die laufende. */
    if (window.joltApp) window.joltApp.showView("live");
    K.report("Die laufende Fahrt geht weiter – die Messpunkte von vorher "
      + "sind erhalten. Falls der Dongle mitlas, einmal neu verbinden.",
      "hinweis");
  }

  function set_up() {
    // Wie bei der Karte: Im versteckten Abschnitt hat das Canvas die Breite
    // null, und nach dem Einblenden oder Drehen muss neu gezeichnet werden.
    window.addEventListener("resize", drawHistory);
    window.addEventListener("resize", drawConsumption);
    K.sliderCouple("mehrverbrauch", "mehrverbrauch-wert");
    K.sliderCouple("stau", "stau-wert");
    K.at("live-starten", "click", launch);
    K.at("simulieren", "click", simulate);
    K.at("live-beenden", "click", finish);
    K.at("soc-melden", "click", reportSoc);
    K.at("dongle-an", "click", connectDongle);
    // Erst wenn die Fahrzeugliste steht - sonst fehlt die Akkugrösse.
    setTimeout(resumeSession, 800);
    K.at("dongle-pause", "click", donglePausieren);
    setInterval(examineVoltage, VOLTAGE_TICK_MS);
    const autoCheckbox = document.getElementById("dongle-auto");
    if (autoCheckbox) {
      autoCheckbox.checked = autoMode;
      autoCheckbox.addEventListener("change", () => {
        autoMode = autoCheckbox.checked;
        try { localStorage.setItem("jolt-dongle-auto", autoMode ? "1" : "0"); }
        catch (e) { /* nur diese Sitzung */ }
        if (autoMode) driveState = "steht";
        else reconnectDongle();
      });
    }
    // Auf dem Telefon ist die Eingabetaste der kürzere Weg als das Zielen auf
    // einen Knopf - `enterkeyhint="send"` beschriftet sie passend.
    K.at("ist-soc", "keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); reportSoc(); }
    });
  }

  return { set_up, launch, finish, link, positionTrace,
           dongleUse, drawHistory,
           driving_state: () => driveState, drivingStateStart, readAllowed, connectDongle,
           examineVoltage, notificationsSetUp, handshakeSafe,
           reconnectDongle,
           setAuto: (at) => { autoMode = !!at; } };
})();
