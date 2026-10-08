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
 * (tools/check_display.js). Das Swift-Plugin kommt später und ruft nur noch
 * `zielSetzen`.
 *
 * Quelle ist der Zustand, den `GET /api/live/{id}` und der WebSocket liefern -
 * nichts, was nicht schon gerechnet wäre. Der Verkehr gehört nicht dazu: Er
 * steht nur in der Antwort der Planung und wird nicht gespeichert.
 */
window.joltDisplay = (function () {
  "use strict";

  // Apple erlaubt einer CarPlay-App der Kategorie "Driving task" höchstens alle
  // zehn Sekunden eine Aktualisierung der Anzeige. Für Live Activities gelten
  // eigene Grenzen; zehn Sekunden sind dafür ohnehin mehr als genug.
  const MIN_SPACING_MS = 15000;
  // Auch wenn sich nichts ändert, kommt regelmässig eine Meldung: Das Modell
  // trägt `stand`, und eine Anzeige, die ihr Alter zeigt, darf nicht "alt"
  // aussehen, nur weil der Ladestand gerade ruhig bleibt.
  const HEARTBEAT_MS = 60000;

  const MINUS = "−";

  function num(val, put) {
    return Number(val).toLocaleString("de-DE", {
      minimumFractionDigits: put || 0, maximumFractionDigits: put || 0 });
  }

  const actual = (x) => typeof x === "number" && Number.isFinite(x);

  /* "+12 min", "nach Plan", "−5 min", "+1 h 05". */
  function shiftText(mins) {
    const amount = Math.round(Math.abs(mins));
    if (amount < 1) return "nach Plan";
    const sign = mins > 0 ? "+" : MINUS;
    if (amount < 60) return `${sign}${amount} min`;
    return `${sign}${Math.floor(amount / 60)} h ${String(amount % 60).padStart(2, "0")}`;
  }

  function kmText(km) {
    if (km < 1) return "gleich";
    return km < 10 ? `${num(km, 1)} km` : `${num(Math.round(km))} km`;
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
  const TIMEFRAME_MIN = [1, 5, 30, 60];
  // Ein Fenster zählt nur, wenn die Spur es auch abdeckt: Wer nach zwölf
  // Minuten Fahrt einen "30-Minuten-Schnitt" zeigt, zeigt einen
  // Zwölf-Minuten-Schnitt unter falschem Namen.
  const TIMEFRAME_COVERAGE = 0.7;
  // Älter als das darf der letzte Punkt nicht sein, sonst ist die Spur
  // stehengeblieben (Dongle weg) und das "Jetzt" ein altes.
  const TRACK_FRESH_MS = 45000;
  const MIN_KM = 0.3;
  // So alt darf ein gelesener Wert sein. Selten gelesene Werte (Klima) kommen
  // nur alle paar Minuten.
  const VALUE_OLD_MS = 15 * 60000;

  const MIN_MS = 60000;
  const BAR_NUMBER = 6;      // sechs Balken zu je fünf Minuten = die letzten dreissig

  function energyAndDistance(points, fromMs) {
    const p = points.filter((x) => x.timestamp >= fromMs && actual(x.gps) && actual(x.net));
    if (p.length < 2) return null;
    const at_first = p[0], final = p[p.length - 1];
    const duration = final.timestamp - at_first.timestamp;
    if (duration <= 0) return null;
    return { at_first, final, duration, km: final.gps - at_first.gps,
             kwh: final.net - at_first.net };
  }

  function historyModel(track, now_ts) {
    if (!Array.isArray(track) || track.length < 2) return null;
    const last = track[track.length - 1];
    if (!last || !actual(last.timestamp) || now_ts - last.timestamp > TRACK_FRESH_MS) return null;

    const timeframe = TIMEFRAME_MIN.map((min) => {
      const e = energyAndDistance(track, now_ts - min * MIN_MS);
      const empty = { min, kwh100: null, kw: null, text: "–", kwText: "–" };
      if (!e || e.duration < TIMEFRAME_COVERAGE * min * MIN_MS) return empty;
      const kw = e.kwh / (e.duration / 3600000);
      const kwh100 = e.km >= MIN_KM ? e.kwh / e.km * 100 : null;
      return { min,
               kwh100: kwh100 === null ? null : Math.round(kwh100 * 10) / 10,
               kw: Math.round(kw * 10) / 10,
               text: kwh100 === null ? "–" : num(kwh100, 1),
               kwText: num(kw, 1) };
    });

    // Die letzten dreissig Minuten in Balken zu fünf Minuten, ältester
    // zuerst. Ein Balken ohne Strecke (Stand, Ampel) ist eine Lücke, kein Null.
    const bar = [];
    for (let i = BAR_NUMBER - 1; i >= 0; i--) {
      const upto = now_ts - i * 5 * MIN_MS;
      const begin = upto - 5 * MIN_MS;
      const p = track.filter((x) => x.timestamp >= begin && x.timestamp <= upto && actual(x.gps) && actual(x.net));
      let val = null;
      if (p.length >= 2) {
        const km = p[p.length - 1].gps - p[0].gps;
        const duration = p[p.length - 1].timestamp - p[0].timestamp;
        if (km >= MIN_KM && duration >= 2 * MIN_MS) {
          val = Math.round((p[p.length - 1].net - p[0].net) / km * 1000) / 10;
        }
      }
      bar.push(val);
    }
    const hasBar = bar.some((b) => b !== null);

    // Rekuperation: wie viel von der entnommenen Energie zurückkam, über die
    // letzte Stunde (oder so lange, wie es die Spur hergibt, mindestens fünf
    // Minuten).
    let regen = null;
    const r = track.filter((x) => x.timestamp >= now_ts - 60 * MIN_MS && actual(x.disch) && actual(x.chg));
    if (r.length >= 2) {
      const duration = r[r.length - 1].timestamp - r[0].timestamp;
      const disch = r[r.length - 1].disch - r[0].disch;
      const chg = r[r.length - 1].chg - r[0].chg;
      if (duration >= 5 * MIN_MS && disch > 0.2 && chg >= 0) {
        regen = { percent: Math.min(100, Math.round(chg / disch * 100)),
                  mins: Math.round(duration / MIN_MS) };
      }
    }

    if (!timeframe.some((f) => f.kw !== null) && !hasBar && !regen) return null;
    return { timeframe, bar: hasBar ? bar : null, regen };
  }

  function valueFresh(vals, name, now_ts) {
    const w = vals && vals[name];
    return w && actual(w.val) && now_ts - w.timestamp <= VALUE_OLD_MS ? w.val : null;
  }

  /* Die Nebenverbraucher: was das Auto zieht, ohne zu fahren. Der gemessene
   * Wert (`nebenverbrauch_kw`) schlägt die Näherung aus dem Stand. Heizung
   * (PTC) und Klimakompressor kommen dazu, wenn sie gelesen wurden: Sie sind
   * die beiden grossen Verbraucher, die man selbst beeinflusst. Die Leistung
   * der Heizung ist Strom mal Packspannung - eine Näherung, kein Messwert. */
  function auxModel(vals, approximation, now_ts) {
    let kw = valueFresh(vals, "aux_load_kw", now_ts);
    let source = "gemessen";
    if (kw === null && approximation && actual(approximation.kw) && now_ts - approximation.timestamp <= VALUE_OLD_MS) {
      kw = approximation.kw;
      source = "geschaetzt";
    }
    const ptcA = valueFresh(vals, "ptc_current_a", now_ts);
    const voltage = valueFresh(vals, "voltage_v", now_ts);
    const heating = (ptcA !== null && voltage !== null) ? ptcA * voltage / 1000 : null;
    const compressorW = valueFresh(vals, "compressor_w", now_ts);
    const climate = compressorW !== null ? compressorW / 1000 : null;
    const batterie = valueFresh(vals, "batterie_c", now_ts);

    if (kw === null && heating === null && climate === null && batterie === null) return null;
    const rounded = (x) => (x === null ? null : Math.round(x * 10) / 10);
    return {
      kw: rounded(kw), text: kw === null ? null : `${num(kw, 1)} kW`, source,
      heatingKw: rounded(heating),
      heatingText: heating === null ? null : `${num(heating, 1)} kW`,
      climateKw: rounded(climate),
      climateText: climate === null ? null : `${num(climate, 1)} kW`,
      batterieC: batterie === null ? null : Math.round(batterie),
      batterieText: batterie === null ? null : `${num(Math.round(batterie))} °C`,
    };
  }

  /* Die Ladestopps der Fahrt, die noch vor einem liegen - für die CarPlay-Liste.
   *
   * Entfernung vom jetzigen Standort aus, nicht vom Start: Wer am Steuer sitzt,
   * fragt "wie weit noch", nicht "bei welchem Kilometer". Ohne Position auf der
   * Route gibt es keine Entfernung, und eine erfundene waere schlimmer als
   * keine Liste. Hoechstens acht: Mehr passt in keine Vorlage. */
  const STOPS_MAX = 8;

  function stopListModel(plan, km) {
    if (km === null || !plan || !Array.isArray(plan.stops)) return null;
    const origin_of = [];
    for (const s of plan.stops) {
      if (!s || !actual(s.km_on_route)) continue;
      if (s.km_on_route < km - 0.5) continue;          // schon vorbei
      const upto = Math.max(0, s.km_on_route - km);
      const at = actual(s.arrival_soc) ? Math.round(s.arrival_soc) : null;
      const downhill = actual(s.departure_soc) ? Math.round(s.departure_soc) : null;
      const min = actual(s.charge_time_minutes) ? Math.round(s.charge_time_minutes) : null;
      origin_of.push({
        name: typeof s.name === "string" && s.name ? s.name : "Ladestopp",
        km: Math.round(upto * 10) / 10, kmText: kmText(upto),
        arrivalSoc: at, arrivalSocText: at === null ? null : `${num(at)} %`,
        departureSocText: downhill === null ? null : `${num(downhill)} %`,
        chargeTimeMin: min, chargeTimeText: min === null ? null : `${num(min)} min`,
        operator: typeof s.operator === "string" && s.operator ? s.operator : null,
        powerKw: actual(s.max_kw) ? Math.round(s.max_kw) : null,
      });
      if (origin_of.length >= STOPS_MAX) break;
    }
    return origin_of.length ? origin_of : null;
  }

  /* Das Modell zu einem Zustand - oder null, wenn es nichts zu zeigen gibt.
   *
   * Fehlt etwas, fehlt es im Modell: kein Ladestopp bei einer Aufzeichnung,
   * keine Ankunft ohne Plan. Nichts wird ersetzt oder geschätzt - eine
   * Anzeige im Auto, die ein Feld erfindet, ist schlimmer als eine, die es
   * weglässt. */
  function model(z, now_ts, extras) {
    // Ein Array ist in JavaScript auch ein Objekt - und kein Zustand.
    if (!z || typeof z !== "object" || Array.isArray(z)) return null;
    const as_of = actual(now_ts) ? now_ts : Date.now();
    const m = { version: 1, as_of,
                soc: null, reserve: null, stop: null, arrival: null, rest: null };

    if (actual(z.actual_soc)) {
      // Woher die Zahl kommt, gehört dazu: gemessen, gerechnet oder die
      // letzte Messung. Wer sie am Steuer liest, soll wissen, was sie ist.
      const source = z.soc_source || (z.soc_reported === false ? "gerechnet" : "gemessen");
      m.soc = { percent: Math.round(z.actual_soc * 10) / 10,
                text: `${num(Math.round(z.actual_soc))} %`, source };
    }

    const km = actual(z.km_on_route) ? z.km_on_route : null;
    // `soll_soc` gibt es nur mit Plan. Ohne ihn (Aufzeichnung) sind Rest,
    // Reserve, Stopp und Ankunft keine Zahlen, sondern Lücken.
    const hasPlan = actual(z.plan_soc);

    if (hasPlan && km !== null && actual(z.reserve_at_km) && z.reserve_at_km >= km) {
      const upto = z.reserve_at_km - km;
      m.reserve = { km: Math.round(upto * 10) / 10, text: kmText(upto) };
    }

    const s = z.next_stop;
    if (hasPlan && km !== null && s && actual(s.km_on_route) && s.km_on_route >= km) {
      const upto = s.km_on_route - km;
      // Erwartet (mit dem gemessenen Verbrauch hochgerechnet), sonst geplant.
      const soc = actual(s.expected_soc) ? s.expected_soc
                : (actual(s.planned_soc) ? s.planned_soc : null);
      m.stop = { name: typeof s.name === "string" && s.name ? s.name : "Ladestopp",
                  km: Math.round(upto * 10) / 10, kmText: kmText(upto),
                  arrivalSoc: soc === null ? null : Math.round(soc),
                  arrivalSocText: soc === null ? null : `${num(Math.round(soc))} %`,
                  planned: !actual(s.expected_soc) };
    }

    if (hasPlan && actual(z.arrival_shift_min)) {
      m.arrival = { min: Math.round(z.arrival_shift_min),
                    text: shiftText(z.arrival_shift_min) };
    }

    if (hasPlan && actual(z.remaining_km)) {
      m.rest = { km: Math.round(z.remaining_km * 10) / 10,
                 text: `${num(Math.round(z.remaining_km))} km` };
    }

    // Eine Zeile für die kleinste Anzeige (Dynamic Island, Apple-Watch-Format):
    // "72 % · Stopp in 41 km (18 %)".
    const parts = [];
    if (m.soc) parts.push(m.soc.text);
    if (m.stop) {
      parts.push(`Stopp in ${m.stop.kmText}` +
                 (m.stop.arrivalSocText ? ` (${m.stop.arrivalSocText})` : ""));
    } else if (m.reserve) {
      parts.push(`Reserve in ${m.reserve.text}`);
    }
    m.short = parts.length ? parts.join(" · ") : "Keine Werte";

    // Verlauf und Nebenverbraucher: nur, wenn die Oberfläche sie mitgibt.
    // Fehlt etwas, fehlt das Feld.
    const extra = extras || {};
    m.history = historyModel(extra.track, as_of);
    m.aux = auxModel(extra.vals, extra.aux, as_of);
    m.stopList = hasPlan ? stopListModel(extra.plan, km) : null;
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
  function sender(options) {
    const opt = options || {};
    const spacingMs = actual(opt.spacingMs) ? opt.spacingMs : MIN_SPACING_MS;
    const heartbeatMs = actual(opt.heartbeatMs) ? opt.heartbeatMs : HEARTBEAT_MS;
    const schedule = opt.schedule || ((f, ms) => setTimeout(f, ms));
    const remove = opt.remove || ((t) => clearTimeout(t));
    const nowFn = opt.now_ts || (() => Date.now());

    let destination = typeof opt.destination === "function" ? opt.destination : null;
    let latestTime = null;
    let lastContent = null;
    let waiting = null;
    let clock = null;
    let ended_at = true;           // erst die erste Meldung beginnt eine Anzeige
    let errorReported = false;

    function contents(m) {
      const { as_of, ...rest } = m;
      return JSON.stringify(rest);
    }

    function send(m) {
      latestTime = m === null ? nowFn() : m.as_of;
      lastContent = m === null ? null : contents(m);
      if (!destination) return;
      try {
        const response = destination(m);
        if (response && typeof response.catch === "function") {
          response.catch((f) => errorRemember(f));
        }
      } catch (f) { errorRemember(f); }
    }

    function errorRemember(f) {
      // Einmal sagen, nicht bei jeder Meldung: Alle zehn Sekunden dieselbe
      // Zeile füllte das Protokoll einer langen Fahrt.
      if (errorReported) return;
      errorReported = true;
      console.log("[anzeige] Ziel meldet einen Fehler:", f && f.message ? f.message : f);
    }

    function sendWaiting() {
      clock = null;
      const m = waiting;
      waiting = null;
      if (m && !ended_at) send(m);
    }

    return {
      setTarget(f) { destination = typeof f === "function" ? f : null; },

      report(z, extras) {
        const now_ts = nowFn();
        const m = model(z, now_ts, extras);
        if (m === null) return;
        ended_at = false;
        const same = lastContent !== null && contents(m) === lastContent;
        const since = latestTime === null ? Infinity : now_ts - latestTime;

        if (same && since < heartbeatMs) { waiting = null; return; }
        if (since >= spacingMs) {
          if (clock !== null) { remove(clock); clock = null; }
          waiting = null;
          send(m);
          return;
        }
        // Zu früh: das Neueste merken und zur erlaubten Zeit senden.
        waiting = m;
        if (clock === null) clock = schedule(sendWaiting, spacingMs - since);
      },

      finish() {
        if (clock !== null) { remove(clock); clock = null; }
        waiting = null;
        if (ended_at) return;
        ended_at = true;
        send(null);
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
  const SERIES_TIMEFRAME_MS = 30 * 60000;
  const SERIES_GAP_MS = 3 * 60000;

  function seriesListAppend(series_list, m, now_ts) {
    const r = series_list || { points: [] };
    const last = r.points[r.points.length - 1];
    if (last && now_ts - last.timestamp > SERIES_GAP_MS) r.points = [];
    r.points.push({
      timestamp: now_ts,
      soc: m && m.soc && actual(m.soc.percent) ? m.soc.percent : null,
      aux: m && m.aux && actual(m.aux.kw) ? m.aux.kw : null,
      regen: m && m.history && m.history.regen && actual(m.history.regen.percent)
        ? m.history.regen.percent : null,
    });
    r.points = r.points.filter((p) => now_ts - p.timestamp <= SERIES_TIMEFRAME_MS);
    return r;
  }

  /* Die Reihen je Kachel; weniger als zwei Punkte sind keine Linie. */
  function seriesListExcerpt(series_list) {
    const origin_of = {};
    for (const name of ["soc", "aux", "regen"]) {
      const w = ((series_list && series_list.points) || []).map((p) => p[name]).filter(actual);
      origin_of[name] = w.length >= 2 ? w : null;
    }
    return origin_of;
  }

  /* ---------- Stil der CarPlay-Kacheln ----------
   *
   * "klassisch": Swift zeichnet wie bisher. "a" und "b": die Bilder kommen aus
   * tiles.js und gehen im Modell mit. */
  const STYLE_KEY = "jolt-carplay-stil";
  const STYLES = ["klassisch", "a", "b"];

  function readStyle() {
    try {
      const s = window.localStorage.getItem(STYLE_KEY);
      return STYLES.includes(s) ? s : "klassisch";
    } catch (e) { return "klassisch"; }
  }

  let styleChoice = readStyle();
  let seriesListState = null;
  let lastModel = null;

  /* Das Modell mit Stil und Kachelbildern - was das Plugin bekommt. Die
   * Bilder stehen nicht im Modell des Senders: Der Vergleich "hat sich etwas
   * geändert" soll nicht an Pixeln hängen, und die Live Activity trägt
   * höchstens 4 KB (das Plugin lässt sie dort weg). */
  function withImages(m, now_ts) {
    seriesListState = seriesListAppend(seriesListState, m, now_ts);
    const origin_of = Object.assign({}, m, { look: styleChoice });
    if (styleChoice !== "klassisch" && window.joltTiles) {
      try {
        origin_of.tileImages = window.joltTiles.pictures(styleChoice, m, seriesListExcerpt(seriesListState));
      } catch (failure) {
        // Ohne Bilder zeichnet Swift selbst - eine Kachel, die nicht gelingt,
        // darf die Anzeige nicht kosten.
        console.log("[anzeige] Kacheln nicht gezeichnet:", failure && failure.message);
      }
    }
    return origin_of;
  }

  /* The native plugin (plugins/jolt-anzeige) still reads the German keys of the
   * display model. toNative() translates them at the border, so the rest of
   * this file can stay English. */
  const NATIVE_KEYS = {
    "arrival": "ankunft",
    "arrivalSoc": "ankunftSoc",
    "arrivalSocText": "ankunftSocText",
    "as_of": "stand",
    "aux": "neben",
    "bar": "balken",
    "chargeTimeMin": "ladezeitMin",
    "chargeTimeText": "ladezeitText",
    "climateKw": "klimaKw",
    "climateText": "klimaText",
    "consumption": "verbrauch",
    "departureSocText": "abfahrtSocText",
    "heatingKw": "heizungKw",
    "heatingText": "heizungText",
    "history": "verlauf",
    "look": "stil",
    "mins": "minuten",
    "operator": "betreiber",
    "percent": "prozent",
    "planned": "geplant",
    "powerKw": "leistungKw",
    "regen": "rekup",
    "short": "kurz",
    "source": "quelle",
    "stop": "stopp",
    "stopList": "stoppListe",
    "stops": "stopps",
    "tileImages": "kachelBilder",
    "timeframe": "fenster"
  };
  function toNative(value) {
    if (Array.isArray(value)) return value.map(toNative);
    if (value && typeof value === "object") {
      const out = {};
      for (const key of Object.keys(value)) out[NATIVE_KEYS[key] || key] = toNative(value[key]);
      return out;
    }
    return value;
  }

  /* Das Ziel in der iOS-App: die Live Activity (plugins/jolt-anzeige).
   *
   * Im Browser und in Bluefy gibt es kein solches Plugin; dort tut das Ziel
   * nichts, und der Sender meldet nichts weiter. Das Plugin wird erst beim
   * Senden gesucht, nicht beim Laden: ble-plugin.js steht zwar vor dieser
   * Datei, aber was beim Laden nicht da ist, soll später nicht fehlen. */
  function nativeTarget(m) {
    const shell = window.joltBlePlugin;
    if (!shell || !shell.JoltAnzeige || !shell.Capacitor
        || !shell.Capacitor.isNativePlatform()) return undefined;
    if (m === null) {
      lastModel = null;
      seriesListState = null;
      return shell.JoltAnzeige.beenden();
    }
    lastModel = m;
    return shell.JoltAnzeige.aktualisieren(
      { json: JSON.stringify(toNative(withImages(m, Date.now()))) });
  }

  /* Den Stil wählen - und die Anzeige gleich neu schicken, damit die
   * Umstellung im Auto zu sehen ist und nicht erst nach der nächsten Meldung. */
  function setStyle(look) {
    if (!STYLES.includes(look)) return false;
    styleChoice = look;
    try { window.localStorage.setItem(STYLE_KEY, look); } catch (e) { /* nur diese Sitzung */ }
    if (lastModel) {
      try { nativeTarget(lastModel); } catch (e) { /* kein Ziel */ }
    }
    return true;
  }

  /* Der Sender der Oberfläche: `live.js` meldet hier jeden Zustand an. */
  const std_default = sender({ destination: nativeTarget });

  return {
    model, sender, seriesListAppend, seriesListExcerpt, withImages, toNative,
    look: () => styleChoice, setStyle, STYLES,
    report: (z, extras) => std_default.report(z, extras),
    finish: () => std_default.finish(),
    setTarget: (f) => std_default.setTarget(f),
    MIN_SPACING_MS, HEARTBEAT_MS,
  };
})();
