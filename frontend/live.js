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
  // Die gefahrene Spur einer Aufzeichnung, [[lon, lat], ...].
  let spur = [];
  // Die entlang dieser Spur zurückgelegte Strecke. Sie wird **fortlaufend**
  // mitgeführt und an jedem Verlaufspunkt festgehalten, statt sie beim
  // Zeichnen aus der Spur nachzurechnen: Spur und Verlauf wachsen unter
  // verschiedenen Bedingungen (die Spur bei jeder neuen Position, der
  // Verlauf bei jedem neuen Ladestand), also gehört `spur[i]` nicht zu
  // `verlauf[i]`. Beim Aufzeichnen mit Dongle ist der Unterschied gewaltig -
  // der Ladestand ändert sich alle paar Minuten, die Position im Sekundentakt.
  let gefahrenKm = 0;
  // Der gemessene Verlauf: [{km, soc, gemeldet}, ...] für die Kurve.
  let verlauf = [];
  // Die zuletzt aus dem Auto gelesenen Werte. Der Server schickt sie nicht
  // zurück - er speichert sie nur -, also hält die Anzeige sie selbst.
  let letzteRohwerte = null;
  let letzteRohwerteZeit = 0;   // wann der letzte vollständige Satz ankam
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
  let werteStand = {};          // name -> {wert, zeit}
  // Anfang der Fahrt für den laufenden Verbrauch: {soc, km} aus der ersten
  // Runde, in der beides zugleich vorlag.
  let verbrauchAnfang = null;
  // Ob wegen der Stille schon gewarnt wurde. Einmal genügt: Eine Meldung,
  // die alle zwölf Sekunden kommt, schaltet man ab.
  let stilleGemeldet = false;
  /* Messpunkte für den Verbrauchsplot: [{zeit, kw, km, soc}, ...].
   *
   * Roh gesammelt und erst beim Zeichnen zu Abschnitten verrechnet - so
   * lässt sich die Abschnittsbreite ändern, ohne die Messung zu verlieren. */
  let verbrauchsspur = [];
  let nieGekommen = new Set();  // Kennungen, die dieses Auto nicht beantwortet
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
  let nebenverbrauch = null;   // {kw, zeit}

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
  const MELDEABSTAND_MS = 12000;

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
        + (haltekosten ? `&stopp_fixkosten_min=${haltekosten.value}` : "")
        + (function () {
            const p = document.getElementById("ladepark");
            return p ? `&ladepark_bonus_min=${p.value}` : "";
          })(),
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
  let neuVerbindenUhr = null;

  function neuVerbinden(sitzungId, versuch) {
    if (neuVerbindenUhr) clearTimeout(neuVerbindenUhr);
    if (versuch > 8 || K.zustand.sitzungId !== sitzungId) return;
    const warten = Math.min(30000, 2000 * Math.pow(2, versuch - 1));
    verbindungAnzeigen(`getrennt – neuer Versuch in ${warten / 1000} s`,
                       "#e8804f");
    neuVerbindenUhr = setTimeout(() => {
      neuVerbindenUhr = null;
      if (K.zustand.sitzungId === sitzungId) verbinden(sitzungId, versuch);
    }, warten);
  }

  function verbinden(sitzungId, versuch = 1) {
    if (steckdose) {
      // Den alten Zuhörer abhängen, bevor geschlossen wird: Sonst löst
      // dieses Schliessen selbst einen Wiederaufbau aus.
      try { steckdose.onclose = null; steckdose.close(); } catch (e) {}
    }
    const schema = location.protocol === "https:" ? "wss" : "ws";
    steckdose = new WebSocket(`${schema}://${location.host}/api/live/${sitzungId}/ws`);

    steckdose.onopen = () => {
      verbindungAnzeigen("verbunden", "#57c98a");
      versuch = 0;   // eine stehende Verbindung setzt die Wartezeit zurück
    };
    steckdose.onclose = () => {
      if (K.zustand.sitzungId === sitzungId) neuVerbinden(sitzungId, versuch + 1);
      else verbindungAnzeigen("getrennt", "#8a97a5");
    };
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

    /* Die Antwort zuerst, und die Antwort ist nicht der Ladestand.
     *
     * Der Ladestand ist eine Eingabe - die Frage im Auto lautet "reicht
     * es?", und die beantwortet der Ankunftswert. Solange ein Ladeplan
     * steht, ist der nächste Stopp die nähere und damit dringlichere
     * Antwort; ohne Plan zählt das Ziel. */
    antwortZeigen(z, reserve);

    /* Darunter nur das, was eine Entscheidung ändert. Ladestand und
     * Abweichung stehen bewusst hier und nicht oben: Sie sind Beleg, nicht
     * Antwort - man liest sie, wenn man der grossen Zahl nachgehen will. */
    document.getElementById("live-werte").innerHTML = [
      // Eine Nachkommastelle: Der Dongle liefert den Ladestand in Schritten
      // von 0,4 Prozentpunkten (ein Byte durch 2,5). Auf ganze Prozent
      // gerundet steht die Zahl minutenlang still, obwohl sie sich bewegt -
      // und gerade die Bewegung will man sehen.
      K.wertKachel(z.soc_gemeldet === false ? "Ladestand (gerechnet)" : "Ladestand",
        K.zahl(z.ist_soc, 1) + " %"),
      K.wertKachel("Abweichung",
        (z.abweichung_pp === null ? "–"
          : (z.abweichung_pp > 0 ? "+" : "") + K.zahl(z.abweichung_pp, 1) + " pp"),
        abweichungArt),
      // Die Ankunftszeit ist die zweite Grösse, die sich unterwegs
      // verschiebt - und die einzige, die ein Stau bewegt, ohne den
      // Verbrauch anzufassen.
      K.wertKachel("Ankunft",
        (z.ankunft_verschiebung_min === null ? "–"
          : (Math.abs(z.ankunft_verschiebung_min) < 1 ? "nach Plan"
            : (z.ankunft_verschiebung_min > 0 ? "+" : "–")
              + K.dauer(Math.abs(z.ankunft_verschiebung_min)))),
        (z.ankunft_verschiebung_min || 0) >= 10 ? "warnung" : ""),
      K.wertKachel("Noch", K.zahl(z.rest_km) + " km"),
    ].join("");

    /* Jeder Messpunkt kommt **zweimal** hier an: einmal als Antwort auf den
     * eigenen POST, einmal über den WebSocket, der ihn an alle Zuschauer
     * zurückspiegelt - und der eigene Browser ist einer davon. Ohne diese
     * Prüfung stünde jeder Punkt doppelt im Verlauf und in der Spur; über
     * eine Langstrecke wären das tausend Einträge zu viel. */
    // Position und Strecke **vor** dem Verlauf: Der Verlaufspunkt soll
    // wissen, wie weit gefahren wurde, als er entstand.
    const ort = messort(z);
    if (ort) {
      const zuletzt = spur[spur.length - 1];
      if (!zuletzt || zuletzt[0] !== ort[0] || zuletzt[1] !== ort[1]) {
        if (zuletzt) gefahrenKm += abstandKm(zuletzt, ort);
        spur.push(ort);
      }
    }

    // Den letzten Verbrauchspunkt mit der jetzt bekannten GPS-Strecke
    // versehen. Er entstand beim Auslesen des Dongles, also bevor die
    // Position durch war.
    const letzterV = verbrauchsspur[verbrauchsspur.length - 1];
    if (letzterV && letzterV.gps === null) letzterV.gps = gefahrenKm;

    const vorheriger = verlauf[verlauf.length - 1];
    // Auch die gefahrene Strecke zählt beim Vergleich: Bei einer
    // Aufzeichnung ist `km_auf_route` für jeden Punkt null (es gibt noch
    // keine Route), und ohne diesen Teil galt jeder Punkt mit unverändertem
    // Ladestand als Dublette. Beim Aufzeichnen mit Dongle sind das fast
    // alle - der Ladestand ändert sich alle paar Minuten.
    const istNeu = z.ist_soc !== null && z.ist_soc !== undefined
      && !(vorheriger && vorheriger.km === (z.km_auf_route || 0)
           && vorheriger.gefahren_km === gefahrenKm
           && vorheriger.soc === z.ist_soc);
    if (istNeu) {
      verlauf.push({ km: z.km_auf_route || 0, gefahren_km: gefahrenKm,
                     soc: z.ist_soc, gemeldet: z.soc_gemeldet !== false });
    }
    verlaufZeichnen();
    verbrauchZeichnen();
    autoZeile(z);

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
    /* Die Karte auch **ohne** geplante Route bedienen.
     *
     * Hier stand `if (fahrt && ...)`, und `K.zustand.fahrt` ist nur gesetzt,
     * wenn vorher eine Route gerechnet wurde. Bei einer Aufzeichnung gibt es
     * keine - also wurde der ganze Block übersprungen und die Karte blieb
     * leer, obwohl die Position längst hereinkam. Die eigene Position hat
     * mit dem Vorhandensein einer Route nichts zu tun.
     */
    if (window.joltKarte) {
      const hier = ort || [z_lon(z), z_lat(z)];
      const marker = [{ lat: hier[1], lon: hier[0], typ: "auto", text: "hier" }];
      // Bei einer Aufzeichnung ist die gefahrene Spur das, was es zu sehen
      // gibt: Sie wächst mit und zeigt, dass wirklich mitgeschrieben wird.
      // Gefüllt wird sie weiter oben, zusammen mit der Strecke.
      if (!fahrt) {
        window.joltKarte.routeSetzen(spur);
        // Nur beim ersten Punkt zentrieren - danach würde die Karte bei
        // jeder Meldung springen, und niemand könnte sie verschieben.
        if (spur.length === 1) window.joltKarte.aufPunkt(hier[1], hier[0], 12);
      }
      if (fahrt && z.reserve_bei_km !== null && fahrt.profil) {
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


  /* ---------- Die Antwort ---------- */

  function antwortZeigen(z, reserve) {
    const kasten = document.getElementById("live-antwort");
    const zahl = document.getElementById("live-antwort-zahl");
    const text = document.getElementById("live-antwort-text");
    if (!kasten) return;

    const stopp = z.naechster_stopp;
    let wert = null, wo = "", art = "";
    if (stopp && stopp.erwartet_soc !== null && stopp.erwartet_soc !== undefined) {
      wert = stopp.erwartet_soc;
      wo = `an ${stopp.name || "nächster Stopp"} · km ${K.zahl(stopp.km_auf_route)}`;
    } else if (z.prognose_soc_am_ziel !== null) {
      wert = z.prognose_soc_am_ziel;
      wo = "am Ziel, ohne Nachladen";
    }

    if (wert === null) {
      zahl.textContent = K.zahl(z.ist_soc) + " %";
      text.textContent = "Ladestand – noch keine Prognose";
      kasten.className = "";
      return;
    }
    // Ein negativer Wert ist keine Aussage über den Akku, sondern darüber,
    // dass es so nicht reicht. Genau das gehört dann da zu stehen.
    if (wert < 0) {
      zahl.textContent = "reicht nicht";
      art = "schlecht";
    } else {
      zahl.textContent = K.zahl(wert) + " %";
      art = wert < reserve ? "schlecht" : (wert < reserve + 8 ? "warnung" : "gut");
    }
    text.textContent = wo;
    kasten.className = art;
  }

  /* Luftlinie zwischen zwei [lon, lat] in Kilometern. Für eine gefahrene
   * Spur mit Punkten alle dreissig Sekunden ist der Unterschied zur
   * Strassenlänge vernachlässigbar - und für die Achse einer Kurve zählt
   * ohnehin nur, dass sie monoton wächst. */
  function abstandKm(a, b) {
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
  function verlaufZeichnen() {
    const leinwand = document.getElementById("live-verlauf");
    if (!leinwand) return;
    const dpr = window.devicePixelRatio || 1;
    const breite = leinwand.clientWidth, hoehe = leinwand.clientHeight;
    if (!breite || !hoehe) return;
    leinwand.width = breite * dpr;
    leinwand.height = hoehe * dpr;
    const stift = leinwand.getContext("2d");
    stift.setTransform(dpr, 0, 0, dpr, 0, 0);
    stift.clearRect(0, 0, breite, hoehe);

    const fahrt = K.zustand.fahrt;
    const profil = (fahrt && fahrt.profil) || [];
    const reserve = fahrt ? fahrt.fahrzeug.reserve_soc : 10;

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
    const eigeneKm = !profil.length;
    const streckeVon = (v) => eigeneKm ? (v.gefahren_km || 0) : v.km;
    const maxKm = profil.length
      ? (profil[profil.length - 1].km || 1)
      : Math.max(1, ...verlauf.map(streckeVon));
    const links = 4, rechts = breite - 4, oben = 8, unten = hoehe - 16;
    const x = (km) => links + (km / maxKm) * (rechts - links);
    const y = (soc) => unten - (Math.max(0, Math.min(100, soc)) / 100)
      * (unten - oben);

    // Höhenprofil im Hintergrund. Es erklärt die Knicke in beiden Kurven -
    // ohne diese Erklärung wirken sie wie Messfehler.
    if (profil.length > 1) {
      let maxHoehe = 1;
      for (const p of profil) maxHoehe = Math.max(maxHoehe, p.hoehe || 0);
      stift.beginPath();
      stift.moveTo(x(0), unten);
      for (const p of profil) {
        stift.lineTo(x(p.km), unten - ((p.hoehe || 0) / maxHoehe) * (unten - oben) * 0.3);
      }
      stift.lineTo(x(maxKm), unten);
      stift.closePath();
      stift.fillStyle = "rgba(138,151,165,.12)";
      stift.fill();
    }

    // Die Reserve als Linie, nicht als Zahl: Man sieht sofort, wo die
    // gemessene Kurve auf sie zuläuft.
    stift.beginPath();
    stift.setLineDash([4, 4]);
    stift.moveTo(links, y(reserve));
    stift.lineTo(rechts, y(reserve));
    stift.strokeStyle = "rgba(226,89,106,.6)";
    stift.lineWidth = 1;
    stift.stroke();
    stift.setLineDash([]);
    stift.fillStyle = "rgba(226,89,106,.75)";
    stift.font = "10px system-ui, sans-serif";
    stift.fillText("Reserve", links + 2, y(reserve) - 3);

    // Geplante Kurve: gedämpft, sie ist der Bezug und nicht die Nachricht.
    if (profil.length > 1) {
      stift.beginPath();
      profil.forEach((p, i) => {
        const px = x(p.km), py = y(p.soc);
        if (i === 0) stift.moveTo(px, py); else stift.lineTo(px, py);
      });
      stift.strokeStyle = "rgba(138,151,165,.55)";
      stift.lineWidth = 1.5;
      stift.stroke();
    }

    // Die Ladestopps als Marken - sie erklären die Sprünge, die gleich
    // kommen, und zeigen, wie weit der nächste noch weg ist.
    for (const stopp of (plan && plan.stopps) || []) {
      const px = x(stopp.km_auf_route);
      stift.beginPath();
      stift.moveTo(px, oben);
      stift.lineTo(px, unten);
      stift.strokeStyle = "rgba(255,201,60,.35)";
      stift.lineWidth = 1;
      stift.stroke();
    }

    // Die gemessene Kurve. Sie ist die Nachricht, also kräftig.
    if (verlauf.length > 1) {
      stift.beginPath();
      verlauf.forEach((v, i) => {
        const px = x(streckeVon(v)), py = y(v.soc);
        if (i === 0) stift.moveTo(px, py); else stift.lineTo(px, py);
      });
      stift.strokeStyle = "#ffc93c";
      stift.lineWidth = 2.5;
      stift.lineJoin = "round";
      stift.stroke();
    }

    // Wo das Auto gerade ist. Ein gerechneter Ladestand bekommt einen
    // hohlen Punkt - man soll ihm ansehen, dass er nicht gemessen ist.
    const jetzt = verlauf[verlauf.length - 1];
    if (jetzt) {
      stift.beginPath();
      stift.arc(x(streckeVon(jetzt)), y(jetzt.soc), 4.5, 0, Math.PI * 2);
      if (jetzt.gemeldet) { stift.fillStyle = "#ffc93c"; stift.fill(); }
      else { stift.strokeStyle = "#ffc93c"; stift.lineWidth = 2; stift.stroke(); }
    }

    stift.fillStyle = "rgba(138,151,165,.8)";
    stift.fillText("0", links, hoehe - 4);
    const beschriftung = K.zahl(maxKm) + " km";
    stift.fillText(beschriftung, rechts - stift.measureText(beschriftung).width,
                   hoehe - 4);
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
  function messort(z) {
    if (typeof z.lat === "number" && typeof z.lon === "number"
        && (z.lat !== 0 || z.lon !== 0)) {
      return [z.lon, z.lat];
    }
    const lat = z_lat(z), lon = z_lon(z);
    return (lat === 0 && lon === 0) ? null : [lon, lat];
  }

  /* Der Rückfall: über das Profil hängt an jedem Kilometerstand eine
   * Position. Gilt nur für geplante Fahrten. */
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
    bildschirmWachHalten();
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
    bildschirmFreigeben();
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
  let wachhalter = null;

  async function bildschirmWachHalten() {
    if (!("wakeLock" in navigator) || wachhalter) return;
    try {
      wachhalter = await navigator.wakeLock.request("screen");
      wachhalter.addEventListener("release", () => { wachhalter = null; });
    } catch (fehler) {
      // Kein Grund, die Fahrt nicht aufzuzeichnen - nur einer, den
      // Bildschirm in den Einstellungen an zu lassen.
      console.log("[live] Bildschirm wachhalten ging nicht:", fehler.message);
    }
  }

  function bildschirmFreigeben() {
    if (!wachhalter) return;
    try { wachhalter.release(); } catch (e) {}
    wachhalter = null;
  }

  /* Zurück im Vordergrund: aufholen, was im Hintergrund liegengeblieben ist.
   *
   * iOS friert eine Seite im Hintergrund ein. Der Abriss der
   * Bluetooth-Verbindung wird dann zwar gemeldet, aber der Wiederaufbau
   * hängt an einem Zeitgeber, und der läuft erst weiter, wenn die Seite
   * wieder sichtbar ist. Ohne dieses Nachfassen bliebe der Dongle getrennt,
   * bis der nächste Abriss kommt - und der kommt nicht mehr. */
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible" || !K.zustand.sitzungId) return;
    bildschirmWachHalten();
    if (dongle && window.joltObd && !window.joltObd.verbunden()) {
      window.joltObd.wiederverbinden(1, () => !!K.zustand.sitzungId);
    }
    // Sofort einen Punkt melden, statt bis zum nächsten Takt zu warten:
    // Nach einer Pause im Hintergrund ist gerade der erste Punkt danach der
    // wichtige - er schliesst die Lücke.
    letzteMeldung = 0;
  });

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

  /* Leistung aus Spannung mal Strom. Das Vorzeichen ist **nicht** bestätigt:
   * Der Rohwert des Stroms ist um 150000 versetzt, positiv heisst also
   * entweder Entladen oder Laden - welches davon, zeigt die erste Messung
   * am Fahrzeug. Deshalb wird der Betrag angezeigt und die Richtung
   * benannt, statt eine Annahme zu treffen, die man nicht sieht. */
  function leistungKw(roh) {
    if (!roh || typeof roh.spannung_v !== "number"
        || typeof roh.strom_a !== "number") return null;
    return roh.spannung_v * roh.strom_a / 1000;
  }

  function nebenverbrauchMerken(roh) {
    // Liegt der gemessene Wert vor, braucht es die Näherung nicht.
    if (typeof roh.nebenverbrauch_kw === "number") return;
    const kw = leistungKw(roh);
    if (kw === null) return;
    const tempo = typeof roh.tempo_kmh === "number" ? roh.tempo_kmh : null;
    // Nur im Stand, und nur wenn Energie entnommen wird - beim Laden misst
    // man den Lader, nicht die Heizung.
    if (tempo !== null && tempo < 5 && kw > 0) {
      nebenverbrauch = { kw, zeit: Date.now() };
    }
  }

  /* Was das Auto misst - als **Zeile**, nicht als Kachelreihe.
   *
   * Sechs weitere Kacheln hätten dieselbe Grösse gehabt wie die vier
   * darüber, und damit hätte alles gleich wichtig ausgesehen. Diese Werte
   * braucht man aber nur gelegentlich: Man sieht hin, wenn man wissen will,
   * *warum* der Verbrauch hoch ist - nicht, um zu erfahren, dass er es ist.
   */
  function autoZeile(z) {
    const block = document.getElementById("live-auto");
    const roh = letzteRohwerte;
    if (!block) return;
    if (!roh) {
      block.hidden = true;
      const zu = document.getElementById("live-roh");
      if (zu) zu.hidden = true;
      return;
    }
    block.hidden = false;

    const kw = leistungKw(roh);
    const tempo = typeof roh.tempo_kmh === "number" ? roh.tempo_kmh : null;
    const momentan = (kw !== null && tempo !== null && tempo >= 5)
      ? Math.abs(kw) / tempo * 100 : null;

    const teile = [];
    /* Der Verbrauch der Fahrt zuerst: Das ist die Zahl, wegen der man
     * aufzeichnet. Die momentane Leistung darunter ist Beiwerk - und beim
     * ID.Buzz ohnehin nicht zu haben, weil der Batteriestrom nicht
     * antwortet. */
    const bisher = laufenderVerbrauch(roh, z && z.ist_soc);
    if (bisher) {
      teile.push(`<b>${K.zahl(bisher.kwh100, 1)}</b> kWh/100 auf `
                 + `${K.zahl(bisher.km, 0)} km`);
    }
    if (momentan !== null) {
      teile.push(`<b>${K.zahl(momentan, 1)}</b> kWh/100 gerade`);
    }
    if (kw !== null) {
      teile.push(`<b>${K.zahl(Math.abs(kw), 0)}</b> kW${kw < 0 ? " zurück" : ""}`);
    }
    if (typeof roh.nebenverbrauch_kw === "number") {
      let n = `<b>${K.zahl(roh.nebenverbrauch_kw, 1)}</b> kW Nebenverbraucher`;
      if (typeof roh.ptc_strom_a === "number"
          && typeof roh.spannung_v === "number") {
        n += `, davon <b>${K.zahl(roh.ptc_strom_a * roh.spannung_v / 1000, 1)}</b> Heizung`;
      }
      teile.push(n);
    } else if (nebenverbrauch) {
      const alter = Math.round((Date.now() - nebenverbrauch.zeit) / 60000);
      teile.push(`<b>${K.zahl(nebenverbrauch.kw, 1)}</b> kW Nebenverbraucher`
                 + ` (im Stand${alter > 0 ? `, vor ${alter} min` : ""})`);
    }
    /* Aussentemperatur und Kilometerstand standen hier auch. Sie sind
     * richtig und interessant, aber nicht **im Fahren** - und eine Zeile
     * mit sechs Angaben liest man gar nicht mehr. Beide stehen in der
     * Tabelle unter der Klappe, wo man sie sucht, wenn man sie sucht. */

    // Zeile und Tabelle stehen jetzt an verschiedenen Stellen: die Zeile
    // oben bei den Kacheln, die Tabelle unten hinter der Klappe.
    document.getElementById("live-auto-zeile").innerHTML = teile.join(" · ");
    const klappe = document.getElementById("live-roh");
    if (klappe) {
      klappe.hidden = false;
      document.getElementById("live-auto-werte").innerHTML =
        rohwerteTabelle(roh);
    }

    /* Wie alt der letzte Satz ist - die Frage, die man am Steuer wirklich
     * hat. "3 s" heisst, der Dongle antwortet; "4 min" heisst, er ist weg,
     * und die Zahlen darunter sind Erinnerungen. Ohne diese Angabe sieht
     * eine eingefrorene Anzeige genauso aus wie eine laufende. */
    const alter = letzteRohwerteZeit
      ? Math.round((Date.now() - letzteRohwerteZeit) / 1000) : null;
    const stand = document.getElementById("live-auto-stand");
    if (alter === null) {
      stand.textContent = "";
    } else if (alter < 90) {
      stand.textContent = `vor ${alter} s`;
      stand.style.color = "";
    } else {
      stand.textContent = `seit ${K.dauer(alter / 60)} keine Antwort`;
      stand.style.color = "#e8804f";
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
      if (!stilleGemeldet && alter > 180) {
        stilleGemeldet = true;
        K.melden("Seit drei Minuten kommt nichts mehr aus dem Auto. jolt "
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
  function werteMerken(roh) {
    const jetzt = Date.now();
    // Für den Verbrauchsplot: Kilometerstand und Ladestand mit Zeitstempel.
    // Die Leistung steht bewusst nicht dabei - siehe verbrauchsabschnitte().
    if (typeof roh.km_stand === "number") {
      /* `netto` ist der Zählerstand: entladen minus geladen. Seine
       * Differenz über ein Stück Fahrt **ist** die verbrauchte Energie -
       * ohne Umweg über Ladestand und Akkugrösse, und mit 0,117 Wh
       * Auflösung statt 339. */
      const netto = (typeof roh.entladen_kwh === "number")
        ? roh.entladen_kwh - (typeof roh.geladen_kwh === "number"
                              ? roh.geladen_kwh : 0)
        : null;
      verbrauchsspur.push({
        zeit: jetzt, km: roh.km_stand, netto,
        // Die GPS-Strecke wird in `zustandAnzeigen` nachgetragen, sobald
        // die Position dieses Punktes bekannt ist.
        gps: null,
        soc: typeof roh.soc_roh === "number" ? roh.soc_roh / 2.5 : null });
      // Grosszuegig: 20 000 Punkte sind bei Zwoelf-Sekunden-Takt rund
      // 66 Stunden. Bei 3000 waeren nach zehn Stunden die ersten Punkte
      // herausgefallen - und mit ihnen der Anfang der Fahrt.
      if (verbrauchsspur.length > 20000) verbrauchsspur.shift();
    }
    for (const [name, wert] of Object.entries(roh)) {
      if (typeof wert === "number") {
        werteStand[name] = { wert, zeit: jetzt };
        nieGekommen.delete(name);
      }
    }
    // "Geantwortet, aber ohne brauchbaren Wert" heisst: Die Datenkennung
    // passt für dieses Fahrzeug nicht. Das bleibt so, bis doch einmal ein
    // Wert kommt - deshalb gemerkt und nicht je Runde neu entschieden.
    for (const name of roh._leer || []) {
      if (!werteStand[name]) nieGekommen.add(name);
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
  const VERBRAUCH_AB_KM = 5.0;
  /* Ab wie viel Energie **hinein** bei stehendem Auto es ein Ladevorgang
   * ist. Rekuperation gibt es nur in Fahrt; wer steht und trotzdem Energie
   * aufnimmt, hängt am Kabel. */
  const LADEN_STAND_KWH = 0.05;

  function laufenderVerbrauch(roh, soc) {
    const fz = K.zustand.aufzFahrzeug
      || (K.zustand.fahrt && K.zustand.fahrt.fahrzeug);
    const akku = fz && (fz.kapazitaet_kwh || fz.akku_netto_kwh);
    if (verbrauchsspur.length < 2) return null;

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
    for (let i = 1; i < verbrauchsspur.length; i++) {
      const a = verbrauchsspur[i - 1], b = verbrauchsspur[i];
      const dkm = (b.km ?? 0) - (a.km ?? 0);
      let d = null;
      if (a.netto !== null && b.netto !== null) d = b.netto - a.netto;
      else if (akku && a.soc !== null && b.soc !== null) {
        d = (a.soc - b.soc) / 100 * akku;
      }
      if (d === null) continue;
      if (dkm <= 0 && d < -LADEN_STAND_KWH) continue;   // an der Säule
      kwh += d;
      km += Math.max(0, dkm);
    }
    if (km < VERBRAUCH_AB_KM) return null;
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
  const ABSCHNITT_MIT_ZAEHLER_S = 60;
  const ABSCHNITT_AUS_SOC_S = 300;
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
  const BREITEN_MIN = [1, 2, 5, 10, 15, 30, 60];
  const BALKEN_HOECHSTENS = 60;

  function breiteWaehlen(dauer_ms, mindest_s) {
    for (const min of BREITEN_MIN) {
      if (min * 60 < mindest_s) continue;
      if (dauer_ms / (min * 60000) <= BALKEN_HOECHSTENS) return min * 60000;
    }
    return BREITEN_MIN[BREITEN_MIN.length - 1] * 60000;
  }
  // Unter dieser Strecke ist kWh/100 km nicht sinnvoll - das Auto stand.
  const BALKEN_MIND_KM = 0.3;

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
  function verbrauchsabschnitte() {
    const punkte = verbrauchsspur.filter((p) => typeof p.gps === "number");
    if (punkte.length < 2) return null;
    const mitZaehler = punkte.every((p) => typeof p.netto === "number");
    const fz = K.zustand.aufzFahrzeug
      || (K.zustand.fahrt && K.zustand.fahrt.fahrzeug) || {};
    const akku = fz.kapazitaet_kwh || fz.akku_netto_kwh;
    if (!mitZaehler && !akku) return null;

    const beginn = punkte[0].zeit;
    const breite = breiteWaehlen(
      punkte[punkte.length - 1].zeit - beginn,
      mitZaehler ? ABSCHNITT_MIT_ZAEHLER_S : ABSCHNITT_AUS_SOC_S);
    const eimer = new Map();
    for (const p of punkte) {
      const n = Math.floor((p.zeit - beginn) / breite);
      if (!eimer.has(n)) eimer.set(n, []);
      eimer.get(n).push(p);
    }

    const balken = [];
    for (const [n, gruppe] of [...eimer.entries()].sort((a, b) => a[0] - b[0])) {
      if (gruppe.length < 2) continue;
      const erst = gruppe[0], letzt = gruppe[gruppe.length - 1];
      const km = letzt.gps - erst.gps;
      if (km < BALKEN_MIND_KM) continue;
      const kwh = mitZaehler ? (letzt.netto - erst.netto)
        : ((erst.soc !== null && letzt.soc !== null)
           ? (erst.soc - letzt.soc) / 100 * akku : null);
      if (kwh === null || !Number.isFinite(kwh)) continue;
      balken.push({ n, kwh100: kwh / km * 100, km });
    }
    return balken.length ? { balken, breite, mitZaehler } : null;
  }

  function verbrauchZeichnen() {
    const leinwand = document.getElementById("live-verbrauch");
    const fuss = document.getElementById("live-verbrauch-fuss");
    if (!leinwand || !fuss) return;
    const daten = verbrauchsabschnitte();
    if (!daten) { leinwand.hidden = true; fuss.hidden = true; return; }
    leinwand.hidden = false; fuss.hidden = false;

    const dpr = window.devicePixelRatio || 1;
    const breite = leinwand.clientWidth, hoehe = leinwand.clientHeight;
    if (!breite || !hoehe) return;
    leinwand.width = breite * dpr;
    leinwand.height = hoehe * dpr;
    const stift = leinwand.getContext("2d");
    stift.setTransform(dpr, 0, 0, dpr, 0, 0);
    stift.clearRect(0, 0, breite, hoehe);

    const werte = daten.balken.map((b) => b.kwh100);
    // Die Skala nach oben grosszuegig, damit ein Ausreisser die uebrigen
    // Balken nicht platt drueckt, und mit Nulllinie: Rekuperation geht
    // unter null, und genau das soll man sehen.
    const oben = Math.max(40, ...werte) * 1.1;
    const unten = Math.min(0, ...werte) * 1.1;
    const spanne = oben - unten || 1;
    const rand = 6, fussHoehe = 16;
    const flaeche = hoehe - fussHoehe - rand;
    const y = (v) => rand + (oben - v) / spanne * flaeche;

    /* Beschriftete Achse. Ohne sie ist ein Balkendiagramm eine Form ohne
     * Aussage - man sieht, dass eine Minute teurer war als die andere, aber
     * nicht, ob es um zwanzig oder um vierzig kWh/100 km geht. Und genau
     * das ist die Zahl, die man mit dem eigenen Gefühl vergleicht.
     *
     * Beschriftet wird links, in die Fläche hinein: Eine eigene Spalte
     * dafür wäre auf dem Telefon zu teuer. Drei Linien reichen - null, ein
     * runder Wert dazwischen und das Maximum. */
    const achse = 30;
    const teilung = [0];
    const schritt = oben > 60 ? 25 : (oben > 25 ? 10 : 5);
    for (let w = schritt; w < oben; w += schritt) teilung.push(w);
    for (let w = -schritt; w > unten; w -= schritt) teilung.push(w);

    stift.font = "10px system-ui, sans-serif";
    stift.textBaseline = "middle";
    for (const w of teilung) {
      const yy = y(w);
      stift.strokeStyle = w === 0 ? "#3a4652" : "#222c36";
      stift.lineWidth = 1;
      stift.beginPath();
      stift.moveTo(achse, yy); stift.lineTo(breite - rand, yy);
      stift.stroke();
      stift.fillStyle = "#8a97a5";
      stift.textAlign = "right";
      stift.fillText(String(w), achse - 4, yy);
    }
    // Die Einheit einmal oben links, nicht an jeden Strich.
    stift.fillStyle = "#8a97a5";
    stift.textAlign = "left";
    stift.fillText("kWh/100", achse + 3, rand + 4);

    const feld = breite - rand - achse;
    const b = Math.max(2, feld / daten.balken.length - 2);
    daten.balken.forEach((balken, i) => {
      const x = achse + i * (feld / daten.balken.length);
      const hoch = y(balken.kwh100) - y(0);
      // Farbe nach Höhe: was deutlich über dem Schnitt liegt, fällt auf.
      stift.fillStyle = balken.kwh100 < 0 ? "#57c98a"
        : (balken.kwh100 > 35 ? "#e8804f" : "#ffc93c");
      stift.fillRect(x, hoch < 0 ? y(balken.kwh100) : y(0),
                     b, Math.max(1, Math.abs(hoch)));
    });

    const schnitt = werte.reduce((a, v) => a + v, 0) / werte.length;
    fuss.children[0].textContent =
      `Verbrauch je ${daten.breite / 60000} min`
      + (daten.mitZaehler ? "" : " (aus dem Ladestand)");
    fuss.children[1].textContent = `Ø ${K.zahl(schnitt, 1)} kWh/100`;
  }

  function alterText(sekunden) {
    if (sekunden < 60) return `vor ${Math.round(sekunden)} s`;
    return `vor ${Math.round(sekunden / 60)} min`;
  }

  function rohwerteTabelle(roh) {
    const felder = (window.joltObd && window.joltObd.FELDER) || [];
    if (!felder.length) return "";
    const fehlend = new Set(roh._fehlend || []);
    const jetzt = Date.now();

    const zeilen = felder.map((f) => {
      const stand = werteStand[f.name];
      if (!stand) {
        // Noch nie ein Wert. Der Grund unterscheidet sich, und der
        // Unterschied ist beim Einrichten die eigentliche Information.
        const grund = nieGekommen.has(f.name) ? "antwortet nicht"
          : (fehlend.has(f.name) ? "keine Antwort" : "–");
        return `<tr class="leer"><th>${f.titel}</th><td>${grund}</td></tr>`;
      }
      const alter = (jetzt - stand.zeit) / 1000;
      const zahl = K.zahl(stand.wert, f.stellen)
        + (f.einheit ? " " + f.einheit : "");
      // Frisch heisst: in dieser Runde gekommen. Alles andere bekommt sein
      // Alter danebengeschrieben und wird blasser, je älter es ist - so
      // sieht man auf einen Blick, welche Zeile noch lebt.
      if (typeof roh[f.name] === "number") {
        return `<tr><th>${f.titel}</th><td>${zahl}</td></tr>`;
      }
      const klasse = alter > 120 ? "alt sehr" : "alt";
      return `<tr class="${klasse}"><th>${f.titel}</th>`
        + `<td>${zahl}<span class="wann">${alterText(alter)}</span></td></tr>`;
    });
    return `<table class="rohwerte"><tbody>${zeilen.join("")}</tbody></table>`;
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
        letzteRohwerte = roh;
        letzteRohwerteZeit = Date.now();
        stilleGemeldet = false;
        werteMerken(roh);
        nebenverbrauchMerken(roh);
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
    // Die Fahrten-Ansicht hat die Liste zwischengespeichert; eine gerade
    // beendete Fahrt gehört hinein.
    K.zustand.fahrtenVeraltet = true;
    positionAufgeben();
    dongle = false;
    spur = [];
    gefahrenKm = 0;
    verlauf = [];
    letzteRohwerte = null;
    letzteRohwerteZeit = 0;
    stilleGemeldet = false;
    werteStand = {};
    nieGekommen = new Set();
    verbrauchAnfang = null;
    verbrauchsspur = [];
    nebenverbrauch = null;
    if (neuVerbindenUhr) { clearTimeout(neuVerbindenUhr); neuVerbindenUhr = null; }
    if (steckdose) {
      try { steckdose.onclose = null; steckdose.close(); } catch (e) {}
    }
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
  /* Woher die Höhen kamen - und zwar nur, wenn es nicht die Karte war.
   *
   * Ein Verbrauch ohne Höhenprofil ist nicht deutbar: Ob 22 kWh/100 km am
   * Fahrstil lagen oder an vierhundert Höhenmetern, lässt sich aus dem
   * Verbrauch allein nicht trennen. Fällt die Kartenabfrage aus, geht die
   * Fahrt trotzdem durch - aber dann soll man es wissen, statt die Zahl
   * später für bare Münze zu nehmen. */
  function hoehenMelden(gebaut) {
    if (!gebaut || !gebaut.ok) return;
    if (gebaut.hoehen === "gps") {
      K.melden("Die Höhen dieser Fahrt kommen aus dem GPS, nicht aus der "
        + "Karte – geglättet, aber ungenauer. Der gelernte Faktor ist "
        + "entsprechend weicher.", "hinweis");
    } else if (gebaut.hoehen === "flach") {
      K.melden("Für diese Fahrt gab es keine Höhendaten; sie wurde flach "
        + "gerechnet. Auf einer Runde macht das wenig aus, auf einer Fahrt "
        + "ins Gebirge viel.", "hinweis");
    }
  }

  function gelerntesMelden(ergebnis) {
    if (!ergebnis) {
      K.melden("Live-Fahrt beendet.", "hinweis");
      return;
    }
    hoehenMelden(ergebnis.aufzeichnung);
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
    // Wie bei der Karte: Im versteckten Abschnitt hat das Canvas die Breite
    // null, und nach dem Einblenden oder Drehen muss neu gezeichnet werden.
    window.addEventListener("resize", verlaufZeichnen);
    window.addEventListener("resize", verbrauchZeichnen);
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
           dongleNutzen, verlaufZeichnen };
})();
