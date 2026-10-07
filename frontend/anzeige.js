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
  const MIN_ABSTAND_MS = 15000;
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

  /* ---------- Was das Auto nicht anzeigt ----------
   *
   * Der Bordcomputer zeigt Verbrauch seit Start und seit dem Tanken. Hier
   * stehen Dinge, die er nicht zeigt: der Verbrauch der letzten Minute, der
   * letzten fünf, dreissig, sechzig - und was die Nebenverbraucher ziehen.
   *
   * Alles kommt aus Grössen, die die Oberfläche ohnehin führt: die
   * Verbrauchsspur (Zeit, Energiezähler, GPS-Strecke) und die letzten
   * Messwerte. Energie aus den Zählern (0,117 Wh Auflösung), Strecke aus dem
   * GPS - aus demselben Grund wie im Verlaufsdiagramm: der Kilometerstand
   * löst nur in ganzen Kilometern auf und taugt nicht für eine Minute. */
  const FENSTER_MIN = [1, 5, 30, 60];
  // Ein Fenster zählt nur, wenn die Spur es auch abdeckt: Wer nach zwölf
  // Minuten Fahrt einen "30-Minuten-Schnitt" zeigt, zeigt einen
  // Zwölf-Minuten-Schnitt unter falschem Namen.
  const FENSTER_ABDECKUNG = 0.7;
  // Älter als das darf der letzte Punkt nicht sein, sonst ist die Spur
  // stehengeblieben (Dongle weg) und das "Jetzt" ein altes.
  const SPUR_FRISCH_MS = 45000;
  const MIND_KM = 0.3;
  // So alt darf ein gelesener Wert sein. Selten gelesene Werte (Klima) kommen
  // nur alle paar Minuten.
  const WERT_ALT_MS = 15 * 60000;

  const MIN_MS = 60000;
  const BALKEN_ZAHL = 6;      // sechs Balken zu je fünf Minuten = die letzten dreissig

  function energieUndStrecke(punkte, vonMs) {
    const p = punkte.filter((x) => x.zeit >= vonMs && ist(x.gps) && ist(x.netto));
    if (p.length < 2) return null;
    const erst = p[0], letzt = p[p.length - 1];
    const dauer = letzt.zeit - erst.zeit;
    if (dauer <= 0) return null;
    return { erst, letzt, dauer, km: letzt.gps - erst.gps,
             kwh: letzt.netto - erst.netto };
  }

  function verlaufModell(spur, jetzt) {
    if (!Array.isArray(spur) || spur.length < 2) return null;
    const letzter = spur[spur.length - 1];
    if (!letzter || !ist(letzter.zeit) || jetzt - letzter.zeit > SPUR_FRISCH_MS) return null;

    const fenster = FENSTER_MIN.map((min) => {
      const e = energieUndStrecke(spur, jetzt - min * MIN_MS);
      const leer = { min, kwh100: null, kw: null, text: "–", kwText: "–" };
      if (!e || e.dauer < FENSTER_ABDECKUNG * min * MIN_MS) return leer;
      const kw = e.kwh / (e.dauer / 3600000);
      const kwh100 = e.km >= MIND_KM ? e.kwh / e.km * 100 : null;
      return { min,
               kwh100: kwh100 === null ? null : Math.round(kwh100 * 10) / 10,
               kw: Math.round(kw * 10) / 10,
               text: kwh100 === null ? "–" : zahl(kwh100, 1),
               kwText: zahl(kw, 1) };
    });

    // Die letzten dreissig Minuten in Balken zu fünf Minuten, ältester
    // zuerst. Ein Balken ohne Strecke (Stand, Ampel) ist eine Lücke, kein Null.
    const balken = [];
    for (let i = BALKEN_ZAHL - 1; i >= 0; i--) {
      const bis = jetzt - i * 5 * MIN_MS;
      const von = bis - 5 * MIN_MS;
      const p = spur.filter((x) => x.zeit >= von && x.zeit <= bis && ist(x.gps) && ist(x.netto));
      let wert = null;
      if (p.length >= 2) {
        const km = p[p.length - 1].gps - p[0].gps;
        const dauer = p[p.length - 1].zeit - p[0].zeit;
        if (km >= MIND_KM && dauer >= 2 * MIN_MS) {
          wert = Math.round((p[p.length - 1].netto - p[0].netto) / km * 1000) / 10;
        }
      }
      balken.push(wert);
    }
    const hatBalken = balken.some((b) => b !== null);

    // Rekuperation: wie viel von der entnommenen Energie zurückkam, über die
    // letzte Stunde (oder so lange, wie es die Spur hergibt, mindestens fünf
    // Minuten).
    let rekup = null;
    const r = spur.filter((x) => x.zeit >= jetzt - 60 * MIN_MS && ist(x.entl) && ist(x.gel));
    if (r.length >= 2) {
      const dauer = r[r.length - 1].zeit - r[0].zeit;
      const entl = r[r.length - 1].entl - r[0].entl;
      const gel = r[r.length - 1].gel - r[0].gel;
      if (dauer >= 5 * MIN_MS && entl > 0.2 && gel >= 0) {
        rekup = { prozent: Math.min(100, Math.round(gel / entl * 100)),
                  minuten: Math.round(dauer / MIN_MS) };
      }
    }

    if (!fenster.some((f) => f.kw !== null) && !hatBalken && !rekup) return null;
    return { fenster, balken: hatBalken ? balken : null, rekup };
  }

  function wertFrisch(werte, name, jetzt) {
    const w = werte && werte[name];
    return w && ist(w.wert) && jetzt - w.zeit <= WERT_ALT_MS ? w.wert : null;
  }

  /* Die Nebenverbraucher: was das Auto zieht, ohne zu fahren. Der gemessene
   * Wert (`nebenverbrauch_kw`) schlägt die Näherung aus dem Stand. Heizung
   * (PTC) und Klimakompressor kommen dazu, wenn sie gelesen wurden: Sie sind
   * die beiden grossen Verbraucher, die man selbst beeinflusst. Die Leistung
   * der Heizung ist Strom mal Packspannung - eine Näherung, kein Messwert. */
  function nebenModell(werte, naeherung, jetzt) {
    let kw = wertFrisch(werte, "nebenverbrauch_kw", jetzt);
    let quelle = "gemessen";
    if (kw === null && naeherung && ist(naeherung.kw) && jetzt - naeherung.zeit <= WERT_ALT_MS) {
      kw = naeherung.kw;
      quelle = "geschaetzt";
    }
    const ptcA = wertFrisch(werte, "ptc_strom_a", jetzt);
    const spannung = wertFrisch(werte, "spannung_v", jetzt);
    const heizung = (ptcA !== null && spannung !== null) ? ptcA * spannung / 1000 : null;
    const kompressorW = wertFrisch(werte, "kompressor_w", jetzt);
    const klima = kompressorW !== null ? kompressorW / 1000 : null;
    const batterie = wertFrisch(werte, "batterie_c", jetzt);

    if (kw === null && heizung === null && klima === null && batterie === null) return null;
    const rund = (x) => (x === null ? null : Math.round(x * 10) / 10);
    return {
      kw: rund(kw), text: kw === null ? null : `${zahl(kw, 1)} kW`, quelle,
      heizungKw: rund(heizung),
      heizungText: heizung === null ? null : `${zahl(heizung, 1)} kW`,
      klimaKw: rund(klima),
      klimaText: klima === null ? null : `${zahl(klima, 1)} kW`,
      batterieC: batterie === null ? null : Math.round(batterie),
      batterieText: batterie === null ? null : `${zahl(Math.round(batterie))} °C`,
    };
  }

  /* Die Ladestopps der Fahrt, die noch vor einem liegen - für die CarPlay-Liste.
   *
   * Entfernung vom jetzigen Standort aus, nicht vom Start: Wer am Steuer sitzt,
   * fragt "wie weit noch", nicht "bei welchem Kilometer". Ohne Position auf der
   * Route gibt es keine Entfernung, und eine erfundene waere schlimmer als
   * keine Liste. Hoechstens acht: Mehr passt in keine Vorlage. */
  const STOPPS_MAX = 8;

  function stoppListeModell(plan, km) {
    if (km === null || !plan || !Array.isArray(plan.stopps)) return null;
    const aus = [];
    for (const s of plan.stopps) {
      if (!s || !ist(s.km_auf_route)) continue;
      if (s.km_auf_route < km - 0.5) continue;          // schon vorbei
      const bis = Math.max(0, s.km_auf_route - km);
      const an = ist(s.ankunft_soc) ? Math.round(s.ankunft_soc) : null;
      const ab = ist(s.abfahrt_soc) ? Math.round(s.abfahrt_soc) : null;
      const min = ist(s.ladezeit_minuten) ? Math.round(s.ladezeit_minuten) : null;
      aus.push({
        name: typeof s.name === "string" && s.name ? s.name : "Ladestopp",
        km: Math.round(bis * 10) / 10, kmText: kmText(bis),
        ankunftSoc: an, ankunftSocText: an === null ? null : `${zahl(an)} %`,
        abfahrtSocText: ab === null ? null : `${zahl(ab)} %`,
        ladezeitMin: min, ladezeitText: min === null ? null : `${zahl(min)} min`,
        betreiber: typeof s.betreiber === "string" && s.betreiber ? s.betreiber : null,
        leistungKw: ist(s.max_kw) ? Math.round(s.max_kw) : null,
      });
      if (aus.length >= STOPPS_MAX) break;
    }
    return aus.length ? aus : null;
  }

  /* Das Modell zu einem Zustand - oder null, wenn es nichts zu zeigen gibt.
   *
   * Fehlt etwas, fehlt es im Modell: kein Ladestopp bei einer Aufzeichnung,
   * keine Ankunft ohne Plan. Nichts wird ersetzt oder geschätzt - eine
   * Anzeige im Auto, die ein Feld erfindet, ist schlimmer als eine, die es
   * weglässt. */
  function modell(z, jetzt, extras) {
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

    // Verlauf und Nebenverbraucher: nur, wenn die Oberfläche sie mitgibt.
    // Fehlt etwas, fehlt das Feld.
    const zusatz = extras || {};
    m.verlauf = verlaufModell(zusatz.spur, stand);
    m.neben = nebenModell(zusatz.werte, zusatz.neben, stand);
    m.stoppListe = hatPlan ? stoppListeModell(zusatz.plan, km) : null;
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

      melden(z, extras) {
        const jetzt = jetztFn();
        const m = modell(z, jetzt, extras);
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

  /* ---------- Verläufe für die Kacheln ----------
   *
   * Die Kacheln "Ladestand", "Nebenverbraucher" und "Rekuperation" zeigen eine
   * Linie: die letzten dreissig Minuten. Das Modell trägt nur den Wert von
   * jetzt, also merkt sich dieser Teil die Stichproben. Eine Lücke von mehr
   * als drei Minuten beginnt die Reihe neu - das ist eine andere Fahrt.
   * Reine Funktion auf einem Zustand, den der Aufrufer hält: so lässt sie sich
   * ohne Uhr prüfen. */
  const REIHE_FENSTER_MS = 30 * 60000;
  const REIHE_LUECKE_MS = 3 * 60000;

  function reihenAnfuegen(reihen, m, jetzt) {
    const r = reihen || { punkte: [] };
    const letzter = r.punkte[r.punkte.length - 1];
    if (letzter && jetzt - letzter.zeit > REIHE_LUECKE_MS) r.punkte = [];
    r.punkte.push({
      zeit: jetzt,
      soc: m && m.soc && ist(m.soc.prozent) ? m.soc.prozent : null,
      neben: m && m.neben && ist(m.neben.kw) ? m.neben.kw : null,
      rekup: m && m.verlauf && m.verlauf.rekup && ist(m.verlauf.rekup.prozent)
        ? m.verlauf.rekup.prozent : null,
    });
    r.punkte = r.punkte.filter((p) => jetzt - p.zeit <= REIHE_FENSTER_MS);
    return r;
  }

  /* Die Reihen je Kachel; weniger als zwei Punkte sind keine Linie. */
  function reihenAuszug(reihen) {
    const aus = {};
    for (const name of ["soc", "neben", "rekup"]) {
      const w = ((reihen && reihen.punkte) || []).map((p) => p[name]).filter(ist);
      aus[name] = w.length >= 2 ? w : null;
    }
    return aus;
  }

  /* ---------- Stil der CarPlay-Kacheln ----------
   *
   * "klassisch": Swift zeichnet wie bisher. "a" und "b": die Bilder kommen aus
   * kacheln.js und gehen im Modell mit. */
  const STIL_SCHLUESSEL = "jolt-carplay-stil";
  const STILE = ["klassisch", "a", "b"];

  function stilLesen() {
    try {
      const s = window.localStorage.getItem(STIL_SCHLUESSEL);
      return STILE.includes(s) ? s : "klassisch";
    } catch (e) { return "klassisch"; }
  }

  let stilWahl = stilLesen();
  let reihenZustand = null;
  let letztesModell = null;

  /* Das Modell mit Stil und Kachelbildern - was das Plugin bekommt. Die
   * Bilder stehen nicht im Modell des Senders: Der Vergleich "hat sich etwas
   * geändert" soll nicht an Pixeln hängen, und die Live Activity trägt
   * höchstens 4 KB (das Plugin lässt sie dort weg). */
  function mitBildern(m, jetzt) {
    reihenZustand = reihenAnfuegen(reihenZustand, m, jetzt);
    const aus = Object.assign({}, m, { stil: stilWahl });
    if (stilWahl !== "klassisch" && window.joltKacheln) {
      try {
        aus.kachelBilder = window.joltKacheln.bilder(stilWahl, m, reihenAuszug(reihenZustand));
      } catch (fehler) {
        // Ohne Bilder zeichnet Swift selbst - eine Kachel, die nicht gelingt,
        // darf die Anzeige nicht kosten.
        console.log("[anzeige] Kacheln nicht gezeichnet:", fehler && fehler.message);
      }
    }
    return aus;
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
    if (m === null) {
      letztesModell = null;
      reihenZustand = null;
      return huelle.JoltAnzeige.beenden();
    }
    letztesModell = m;
    return huelle.JoltAnzeige.aktualisieren(
      { json: JSON.stringify(mitBildern(m, Date.now())) });
  }

  /* Den Stil wählen - und die Anzeige gleich neu schicken, damit die
   * Umstellung im Auto zu sehen ist und nicht erst nach der nächsten Meldung. */
  function stilSetzen(stil) {
    if (!STILE.includes(stil)) return false;
    stilWahl = stil;
    try { window.localStorage.setItem(STIL_SCHLUESSEL, stil); } catch (e) { /* nur diese Sitzung */ }
    if (letztesModell) {
      try { nativesZiel(letztesModell); } catch (e) { /* kein Ziel */ }
    }
    return true;
  }

  /* Der Sender der Oberfläche: `live.js` meldet hier jeden Zustand an. */
  const standard = sender({ ziel: nativesZiel });

  return {
    modell, sender, reihenAnfuegen, reihenAuszug, mitBildern,
    stil: () => stilWahl, stilSetzen, STILE,
    melden: (z, extras) => standard.melden(z, extras),
    beenden: () => standard.beenden(),
    zielSetzen: (f) => standard.zielSetzen(f),
    MIN_ABSTAND_MS, HERZSCHLAG_MS,
  };
})();
