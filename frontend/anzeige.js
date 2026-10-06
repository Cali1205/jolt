/* Das Anzeigemodell: der Zustand der Fahrt als wenige Zahlen und Texte.
 *
 * Für alles, was nicht die Oberfläche selbst ist - eine Live Activity im
 * CarPlay-Dashboard, ein Widget, eine CarPlay-Vorlage (siehe
 * konzept-ios-app.md, Abschnitt "CarPlay"). Diese Anzeigen haben Platz für
 * fünf Zeilen, nicht für ein Diagramm, und sie rechnen nichts: Sie zeigen,
 * was hier steht.
 *
 * Bewusst ohne DOM, ohne `window.jolt` und ohne Netz: Eine reine Funktion
 * `Zustand -> Modell` lässt sich ohne Swift, ohne Mac und ohne Apple prüfen
 * (tools/check_anzeige.js). Das Swift-Plugin kommt später und ruft nur noch
 * `zielSetzen`.
 *
 * Quelle ist der Zustand, den `GET /api/live/{id}` und der WebSocket liefern -
 * nichts, was nicht schon gerechnet wäre. Der Verkehr gehört nicht dazu: Er
 * steht nur in der Antwort der Planung und wird nicht gespeichert.
 */
window.joltAnzeige = (function () {
  "use strict";

  // Apple erlaubt einer CarPlay-App der Kategorie "Driving task" höchstens alle
  // zehn Sekunden eine Aktualisierung der Anzeige. Für Live Activities gelten
  // eigene Grenzen; zehn Sekunden sind dafür ohnehin mehr als genug.
  const MIN_ABSTAND_MS = 10000;
  // Auch wenn sich nichts ändert, kommt regelmässig eine Meldung: Das Modell
  // trägt `stand`, und eine Anzeige, die ihr Alter zeigt, darf nicht "alt"
  // aussehen, nur weil der Ladestand gerade ruhig bleibt.
  const HERZSCHLAG_MS = 60000;

  const MINUS = "−";

  function zahl(wert, stellen) {
    return Number(wert).toLocaleString("de-DE", {
      minimumFractionDigits: stellen || 0, maximumFractionDigits: stellen || 0 });
  }

  const ist = (x) => typeof x === "number" && Number.isFinite(x);

  /* "+12 min", "nach Plan", "−5 min", "+1 h 05". */
  function verschiebungText(minuten) {
    const betrag = Math.round(Math.abs(minuten));
    if (betrag < 1) return "nach Plan";
    const vorzeichen = minuten > 0 ? "+" : MINUS;
    if (betrag < 60) return `${vorzeichen}${betrag} min`;
    return `${vorzeichen}${Math.floor(betrag / 60)} h ${String(betrag % 60).padStart(2, "0")}`;
  }

  function kmText(km) {
    if (km < 1) return "gleich";
    return km < 10 ? `${zahl(km, 1)} km` : `${zahl(Math.round(km))} km`;
  }

  /* Das Modell zu einem Zustand - oder null, wenn es nichts zu zeigen gibt.
   *
   * Fehlt etwas, fehlt es im Modell: kein Ladestopp bei einer Aufzeichnung,
   * keine Ankunft ohne Plan. Nichts wird ersetzt oder geschätzt - eine
   * Anzeige im Auto, die ein Feld erfindet, ist schlimmer als eine, die es
   * weglässt. */
  function modell(z, jetzt) {
    // Ein Array ist in JavaScript auch ein Objekt - und kein Zustand.
    if (!z || typeof z !== "object" || Array.isArray(z)) return null;
    const stand = ist(jetzt) ? jetzt : Date.now();
    const m = { version: 1, stand,
                soc: null, reserve: null, stopp: null, ankunft: null, rest: null };

    if (ist(z.ist_soc)) {
      // Woher die Zahl kommt, gehört dazu: gemessen, gerechnet oder die
      // letzte Messung. Wer sie am Steuer liest, soll wissen, was sie ist.
      const quelle = z.soc_quelle || (z.soc_gemeldet === false ? "gerechnet" : "gemessen");
      m.soc = { prozent: Math.round(z.ist_soc * 10) / 10,
                text: `${zahl(Math.round(z.ist_soc))} %`, quelle };
    }

    const km = ist(z.km_auf_route) ? z.km_auf_route : null;
    // `soll_soc` gibt es nur mit Plan. Ohne ihn (Aufzeichnung) sind Rest,
    // Reserve, Stopp und Ankunft keine Zahlen, sondern Lücken.
    const hatPlan = ist(z.soll_soc);

    if (hatPlan && km !== null && ist(z.reserve_bei_km) && z.reserve_bei_km >= km) {
      const bis = z.reserve_bei_km - km;
      m.reserve = { km: Math.round(bis * 10) / 10, text: kmText(bis) };
    }

    const s = z.naechster_stopp;
    if (hatPlan && km !== null && s && ist(s.km_auf_route) && s.km_auf_route >= km) {
      const bis = s.km_auf_route - km;
      // Erwartet (mit dem gemessenen Verbrauch hochgerechnet), sonst geplant.
      const soc = ist(s.erwartet_soc) ? s.erwartet_soc
                : (ist(s.geplant_soc) ? s.geplant_soc : null);
      m.stopp = { name: typeof s.name === "string" && s.name ? s.name : "Ladestopp",
                  km: Math.round(bis * 10) / 10, kmText: kmText(bis),
                  ankunftSoc: soc === null ? null : Math.round(soc),
                  ankunftSocText: soc === null ? null : `${zahl(Math.round(soc))} %`,
                  geplant: !ist(s.erwartet_soc) };
    }

    if (hatPlan && ist(z.ankunft_verschiebung_min)) {
      m.ankunft = { min: Math.round(z.ankunft_verschiebung_min),
                    text: verschiebungText(z.ankunft_verschiebung_min) };
    }

    if (hatPlan && ist(z.rest_km)) {
      m.rest = { km: Math.round(z.rest_km * 10) / 10,
                 text: `${zahl(Math.round(z.rest_km))} km` };
    }

    // Eine Zeile für die kleinste Anzeige (Dynamic Island, Apple-Watch-Format):
    // "72 % · Stopp in 41 km (18 %)".
    const teile = [];
    if (m.soc) teile.push(m.soc.text);
    if (m.stopp) {
      teile.push(`Stopp in ${m.stopp.kmText}` +
                 (m.stopp.ankunftSocText ? ` (${m.stopp.ankunftSocText})` : ""));
    } else if (m.reserve) {
      teile.push(`Reserve in ${m.reserve.text}`);
    }
    m.kurz = teile.length ? teile.join(" · ") : "Keine Werte";
    return m;
  }

  /* Der Sender: gibt Modelle an ein Ziel (das Swift-Plugin) weiter, aber nicht
   * öfter als erlaubt und nicht, ohne dass es etwas Neues gäbe.
   *
   *  - höchstens alle `abstandMs`; was dazwischen kommt, wartet, und nur das
   *    Neueste wird gesendet,
   *  - unverändert (bis auf `stand`) wird nicht gesendet - ausser als
   *    Herzschlag nach `herzschlagMs`,
   *  - `beenden()` meldet `null`: Die Fahrt ist zu Ende, die Anzeige soll
   *    verschwinden,
   *  - ein Ziel, das fehlt oder scheitert, bricht nichts ab: Die Oberfläche
   *    läuft weiter, auch wenn das Auto nichts davon sieht. */
  function sender(optionen) {
    const opt = optionen || {};
    const abstandMs = ist(opt.abstandMs) ? opt.abstandMs : MIN_ABSTAND_MS;
    const herzschlagMs = ist(opt.herzschlagMs) ? opt.herzschlagMs : HERZSCHLAG_MS;
    const planen = opt.planen || ((f, ms) => setTimeout(f, ms));
    const loeschen = opt.loeschen || ((t) => clearTimeout(t));
    const jetztFn = opt.jetzt || (() => Date.now());

    let ziel = typeof opt.ziel === "function" ? opt.ziel : null;
    let letzteZeit = null;
    let letzterInhalt = null;
    let wartend = null;
    let uhr = null;
    let beendet = true;           // erst die erste Meldung beginnt eine Anzeige
    let fehlerGemeldet = false;

    function inhalt(m) {
      const { stand, ...rest } = m;
      return JSON.stringify(rest);
    }

    function senden(m) {
      letzteZeit = m === null ? jetztFn() : m.stand;
      letzterInhalt = m === null ? null : inhalt(m);
      if (!ziel) return;
      try {
        const antwort = ziel(m);
        if (antwort && typeof antwort.catch === "function") {
          antwort.catch((f) => fehlerMerken(f));
        }
      } catch (f) { fehlerMerken(f); }
    }

    function fehlerMerken(f) {
      // Einmal sagen, nicht bei jeder Meldung: Alle zehn Sekunden dieselbe
      // Zeile füllte das Protokoll einer langen Fahrt.
      if (fehlerGemeldet) return;
      fehlerGemeldet = true;
      console.log("[anzeige] Ziel meldet einen Fehler:", f && f.message ? f.message : f);
    }

    function wartendSenden() {
      uhr = null;
      const m = wartend;
      wartend = null;
      if (m && !beendet) senden(m);
    }

    return {
      zielSetzen(f) { ziel = typeof f === "function" ? f : null; },

      melden(z) {
        const jetzt = jetztFn();
        const m = modell(z, jetzt);
        if (m === null) return;
        beendet = false;
        const gleich = letzterInhalt !== null && inhalt(m) === letzterInhalt;
        const seit = letzteZeit === null ? Infinity : jetzt - letzteZeit;

        if (gleich && seit < herzschlagMs) { wartend = null; return; }
        if (seit >= abstandMs) {
          if (uhr !== null) { loeschen(uhr); uhr = null; }
          wartend = null;
          senden(m);
          return;
        }
        // Zu früh: das Neueste merken und zur erlaubten Zeit senden.
        wartend = m;
        if (uhr === null) uhr = planen(wartendSenden, abstandMs - seit);
      },

      beenden() {
        if (uhr !== null) { loeschen(uhr); uhr = null; }
        wartend = null;
        if (beendet) return;
        beendet = true;
        senden(null);
      },
    };
  }

  /* Das Ziel in der iOS-App: die Live Activity (plugins/jolt-anzeige).
   *
   * Im Browser und in Bluefy gibt es kein solches Plugin; dort tut das Ziel
   * nichts, und der Sender meldet nichts weiter. Das Plugin wird erst beim
   * Senden gesucht, nicht beim Laden: ble-plugin.js steht zwar vor dieser
   * Datei, aber was beim Laden nicht da ist, soll später nicht fehlen. */
  function nativesZiel(m) {
    const huelle = window.joltBlePlugin;
    if (!huelle || !huelle.JoltAnzeige || !huelle.Capacitor
        || !huelle.Capacitor.isNativePlatform()) return undefined;
    if (m === null) return huelle.JoltAnzeige.beenden();
    return huelle.JoltAnzeige.aktualisieren({ json: JSON.stringify(m) });
  }

  /* Der Sender der Oberfläche: `live.js` meldet hier jeden Zustand an. */
  const standard = sender({ ziel: nativesZiel });

  return {
    modell, sender,
    melden: (z) => standard.melden(z),
    beenden: () => standard.beenden(),
    zielSetzen: (f) => standard.zielSetzen(f),
    MIN_ABSTAND_MS, HERZSCHLAG_MS,
  };
})();
