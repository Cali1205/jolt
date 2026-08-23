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
      const antwort = await K.api(`/api/live/start/${fahrt.fahrt_id}`
        + `?min_kw=${document.getElementById("min-kw").value}`
        + `&radius_km=${document.getElementById("radius").value}`,
        { method: "POST" });
      K.zustand.sitzungId = antwort.sitzung_id;
      document.getElementById("live-leer").hidden = true;
      document.getElementById("live-inhalt").hidden = false;
      plan = antwort.plan || null;
      planZeichnen();
      verbinden(antwort.sitzung_id);
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

    const abweichungArt = z.abweichung_pp === null ? ""
      : (z.abweichung_pp <= -5 ? "schlecht"
        : (z.abweichung_pp <= -2 ? "warnung" : "gut"));
    const prognoseArt = z.prognose_soc_am_ziel === null ? ""
      : (z.prognose_soc_am_ziel < reserve ? "schlecht"
        : (z.prognose_soc_am_ziel < reserve + 10 ? "warnung" : "gut"));

    document.getElementById("live-werte").innerHTML = [
      K.wertKachel("Ladestand", K.zahl(z.ist_soc) + " %"),
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

  async function socMelden() {
    if (!K.zustand.sitzungId) { K.melden("Keine Live-Fahrt.", "fehler"); return; }
    const knopf = document.getElementById("soc-melden");
    const soc = Number(document.getElementById("ist-soc").value);
    if (!(soc >= 0 && soc <= 100)) {
      K.melden("Ladestand zwischen 0 und 100 % angeben.", "fehler");
      return;
    }

    knopf.disabled = true;
    try {
      const ort = await standortHolen();
      const zustand = await K.api(`/api/live/${K.zustand.sitzungId}/punkt`,
        { method: "POST", body: { lat: ort.lat, lon: ort.lon, soc: soc,
                                  tempo_kmh: ort.tempo_kmh } });
      zustandAnzeigen(zustand);
    } catch (fehler) {
      K.melden(fehler.message, "fehler");
    } finally {
      knopf.disabled = false;
    }
  }

  async function beenden() {
    if (!K.zustand.sitzungId) return;
    try {
      await K.api(`/api/live/${K.zustand.sitzungId}/ende`, { method: "POST" });
    } catch (fehler) { /* eine bereits beendete Fahrt ist kein Problem */ }
    if (steckdose) { try { steckdose.close(); } catch (e) {} }
    K.zustand.sitzungId = null;
    plan = null;
    const kasten = document.getElementById("live-aenderung");
    if (kasten) kasten.hidden = true;
    document.getElementById("live-inhalt").hidden = true;
    document.getElementById("live-leer").hidden = false;
    K.melden("Live-Fahrt beendet.", "hinweis");
  }

  function einrichten() {
    K.reglerKoppeln("mehrverbrauch", "mehrverbrauch-wert");
    K.reglerKoppeln("stau", "stau-wert");
    K.an("live-starten", "click", starten);
    K.an("simulieren", "click", simulieren);
    K.an("live-beenden", "click", beenden);
    K.an("soc-melden", "click", socMelden);
  }

  return { einrichten, starten, beenden };
})();
