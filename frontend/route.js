/* Die Planen-Ansicht: Orte suchen, Route rechnen, Ergebnis zeigen. */
window.joltRoute = (function () {
  "use strict";

  const K = window.jolt;
  const gewaehlt = { start: null, ziel: null };
  // Was gerade auf der Karte liegt. Säulenliste und Ladeplan werden getrennt
  // geladen, zeichnen aber in dieselbe Karte - ohne diesen gemeinsamen Stand
  // löscht der eine die Marker des anderen.
  let letzteSaeulen = [];
  let letzterPlan = null;
  // Die Varianten der letzten Berechnung (schnellste/kürzeste/empfohlene,
  // ggf. zusammengelegt) - für die Wechsel-Karten und um beim Tippen auf eine
  // Karte zu wissen, welcher fahrt_id sie entspricht.
  let letzteVarianten = [];

  /* ---------- Ortssuche ---------- */

  function ortsucheEinrichten(feldId, trefferId, schluessel) {
    const feld = document.getElementById(feldId);
    const liste = document.getElementById(trefferId);
    if (!feld || !liste) return;
    let warten = null;

    feld.addEventListener("input", () => {
      gewaehlt[schluessel] = null;
      clearTimeout(warten);
      const text = feld.value.trim();
      if (text.length < 2) { liste.innerHTML = ""; return; }
      // Getippt wird schneller als geantwortet. Ohne diese Pause schickt
      // jedes Zeichen eine Anfrage - beim freien ORS-Kontingent von 2500
      // Anfragen am Tag ist das der schnellste Weg, es aufzubrauchen.
      warten = setTimeout(() => suchen(text, liste, feld, schluessel), 400);
    });
  }

  async function suchen(text, liste, feld, schluessel) {
    try {
      const antwort = await K.api("/api/orte?text=" + encodeURIComponent(text));
      liste.innerHTML = "";
      if (!antwort.treffer.length) {
        liste.innerHTML = '<li class="leer">Nichts gefunden.</li>';
        return;
      }
      for (const ort of antwort.treffer) {
        const eintrag = document.createElement("li");
        const knopf = document.createElement("button");
        knopf.textContent = ort.name;
        knopf.addEventListener("click", () => {
          gewaehlt[schluessel] = ort;
          feld.value = ort.name;
          liste.innerHTML = "";
        });
        eintrag.appendChild(knopf);
        liste.appendChild(eintrag);
      }
    } catch (fehler) {
      liste.innerHTML = "";
      K.melden("Ortssuche: " + fehler.message, "fehler");
    }
  }

  /* ---------- Route rechnen ---------- */

  async function rechnen() {
    const knopf = document.getElementById("rechnen");
    const fahrzeugId = Number(document.getElementById("fahrzeug-wahl").value);
    if (!fahrzeugId) { K.melden("Erst ein Fahrzeug anlegen.", "fehler"); return; }
    if (!gewaehlt.start || !gewaehlt.ziel) {
      K.melden("Start und Ziel aus der Vorschlagsliste auswählen.", "fehler");
      return;
    }

    K.meldungenLeeren();
    knopf.disabled = true;
    knopf.textContent = "rechnet …";
    try {
      const antwort = await K.api("/api/route", { method: "POST", body: {
        fahrzeug_id: fahrzeugId,
        start: { lat: gewaehlt.start.lat, lon: gewaehlt.start.lon,
                 text: gewaehlt.start.name },
        ziel: { lat: gewaehlt.ziel.lat, lon: gewaehlt.ziel.lon,
                text: gewaehlt.ziel.name },
        start_soc: Number(document.getElementById("start-soc").value),
        tempo_faktor: Number(document.getElementById("tempo").value) / 100,
        luftwiderstand_faktor: Number(document.getElementById("anbau").value),
        // Der Regler steht ganz links auf einem negativen Wert - das ist
        // "nicht gesetzt", und dann gilt das Fahrzeugprofil. Ein eigener
        // Schalter daneben wäre ein zweites Bedienelement für eine Frage,
        // die der Regler schon beantwortet.
        zuladung_kg: zuladungWert(),
      }});
      letzteVarianten = antwort.varianten || [];
      variantenZeichnen();
      // Die sparsamste Variante ist jolts Grundhaltung - sie wird
      // vorausgewählt, ein Tippen auf eine andere Karte wechselt.
      const vorschlag = letzteVarianten.find((v) => v.etiketten.includes("sparsamste"))
        || letzteVarianten[0];
      if (vorschlag) await varianteWaehlen(vorschlag);
    } catch (fehler) {
      K.melden("Route: " + fehler.message, "fehler");
    } finally {
      knopf.disabled = false;
      knopf.textContent = "Route rechnen";
    }
  }

  async function varianteWaehlen(variante) {
    K.zustand.fahrt = variante;
    anzeigen(variante);
    variantenZeichnen();
    await saeulenLaden();
    await ladeplanLaden();
  }

  /* Eine gespeicherte Fahrt laden und zeichnen.
   *
   * Der Endpunkt liefert dieselbe Form wie eine frische Variante, deshalb
   * genügt derselbe Weg wie nach dem Rechnen. Die Varianten-Karten bleiben
   * leer: Zu einer einzeln geladenen Fahrt gibt es keine Geschwister mehr -
   * die anderen Varianten von damals sind eigene Fahrten mit eigener ID. */
  async function fahrtLaden(fahrtId) {
    const fahrt = await K.api("/api/fahrten/" + fahrtId);
    letzteVarianten = [];
    variantenZeichnen();
    K.zustand.fahrt = fahrt;
    anzeigen(fahrt);
    await saeulenLaden();
    await ladeplanLaden();
  }

  /* ---------- Varianten-Karten ---------- */

  function variantenZeichnen() {
    const block = document.getElementById("varianten-block");
    const liste = document.getElementById("varianten");
    if (!block || !liste) return;
    block.hidden = letzteVarianten.length < 2;
    if (letzteVarianten.length < 2) { liste.innerHTML = ""; return; }

    const aktiveId = K.zustand.fahrt && K.zustand.fahrt.fahrt_id;
    liste.innerHTML = "";
    for (const v of letzteVarianten) {
      const knopf = document.createElement("button");
      knopf.className = "variante";
      knopf.type = "button";
      knopf.setAttribute("aria-pressed", String(v.fahrt_id === aktiveId));
      const etiketten = v.etiketten.map((e) =>
        `<span class="etikett ${e === "sparsamste" ? "sparsamste" : ""}">${e}</span>`
      ).join("");
      knopf.innerHTML = `
        <span class="etiketten">${etiketten}</span>
        <span class="kennwerte">${K.zahl(v.strecke_km)} km ·
          ${K.dauer(v.fahrzeit_minuten)} · ${K.zahl(v.kwh_gesamt, 1)} kWh</span>`;
      knopf.addEventListener("click", () => {
        if (v.fahrt_id !== (K.zustand.fahrt && K.zustand.fahrt.fahrt_id)) {
          varianteWaehlen(v);
        }
      });
      liste.appendChild(knopf);
    }
  }

  /* ---------- Ergebnis ---------- */

  function anzeigen(fahrt) {
    document.getElementById("ergebnis").hidden = false;

    const reserveArt = fahrt.reicht ? "gut" : "schlecht";
    document.getElementById("kennzahlen").innerHTML = [
      K.wertKachel("Strecke", K.zahl(fahrt.strecke_km) + " km"),
      K.wertKachel("Fahrzeit", K.dauer(fahrt.fahrzeit_minuten)),
      K.wertKachel("Energie", K.zahl(fahrt.kwh_gesamt, 1) + " kWh"),
      K.wertKachel("Verbrauch", K.zahl(fahrt.verbrauch_kwh_100km, 1) + " kWh/100"),
      fahrt.reicht
        ? K.wertKachel("Am Ziel", K.zahl(fahrt.soc_am_ziel) + " %", "gut")
        : K.wertKachel("Reserve bei", K.zahl(fahrt.reserve_bei_km) + " km",
                       reserveArt),
      K.wertKachel("Gerechnet bei", K.zahl(fahrt.wetter.temp_c, 1) + " °C"),
    ].join("");

    if (!fahrt.reicht) {
      K.melden(`Die Strecke ist ohne Nachladen nicht zu schaffen: Die Reserve `
        + `von ${K.zahl(fahrt.fahrzeug.reserve_soc)} % wird nach `
        + `${K.zahl(fahrt.reserve_bei_km)} km erreicht. Der Ladeplan steht `
        + `unten.`, "warnung");
    }

    profilZeichnen(fahrt);

    window.joltKarte.routeSetzen(fahrt.geometrie);
    letzteSaeulen = [];
    letzterPlan = null;
    markerSetzen();
    window.joltKarte.aufRoutePassen();
  }

  function markerSetzen() {
    const fahrt = K.zustand.fahrt;
    const punkte = (fahrt && fahrt.geometrie) || [];
    if (punkte.length < 2) return;
    const liste = [
      { lat: punkte[0][1], lon: punkte[0][0], typ: "start", text: "Start" },
      { lat: punkte[punkte.length - 1][1], lon: punkte[punkte.length - 1][0],
        typ: "ziel", text: "Ziel" },
    ];
    if (fahrt.reserve_punkt) {
      liste.push({ lat: fahrt.reserve_punkt.lat, lon: fahrt.reserve_punkt.lon,
                   typ: "reserve",
                   text: "Reserve " + K.zahl(fahrt.reserve_punkt.km) + " km" });
    }

    // Geplante Stopps zuletzt und beschriftet: Sie sollen die übrigen
    // Ladepunkte überdecken, nicht umgekehrt.
    const geplant = new Set((letzterPlan && letzterPlan.stopps || [])
      .map((s) => s.id));
    for (const s of letzteSaeulen) {
      if (geplant.has(s.id)) continue;
      liste.push({ lat: s.lat, lon: s.lon,
                   typ: s.belegt_gemeldet ? "saeuleBelegt" : "saeule" });
    }
    (letzterPlan && letzterPlan.stopps || []).forEach((s, i) => {
      liste.push({ lat: s.lat, lon: s.lon, typ: "stopp",
                   text: `${i + 1}. ${K.dauer(s.ladezeit_minuten)}` });
    });
    window.joltKarte.markerSetzen(liste);
  }

  /* ---------- Ladestand über der Strecke ---------- */

  function profilZeichnen(fahrt) {
    const leinwand = document.getElementById("profil");
    if (!leinwand) return;
    const verhaeltnis = window.devicePixelRatio || 1;
    const breite = leinwand.clientWidth, hoehe = leinwand.clientHeight;
    leinwand.width = breite * verhaeltnis;
    leinwand.height = hoehe * verhaeltnis;
    const stift = leinwand.getContext("2d");
    stift.setTransform(verhaeltnis, 0, 0, verhaeltnis, 0, 0);
    stift.clearRect(0, 0, breite, hoehe);

    const profil = fahrt.profil || [];
    if (profil.length < 2) return;
    const maxKm = profil[profil.length - 1].km || 1;
    const reserve = fahrt.fahrzeug.reserve_soc;

    const x = (km) => (km / maxKm) * (breite - 8) + 4;
    // Unter null wird nicht gezeichnet: Ein negativer Ladestand ist keine
    // Aussage über den Akku, sondern über die fehlende Ladeplanung.
    const y = (soc) => hoehe - 6 - (Math.max(0, Math.min(100, soc)) / 100)
      * (hoehe - 24);

    // Höhenprofil als gedämpfter Hintergrund - es erklärt die Knicke in der
    // SoC-Kurve, und ohne diese Erklärung wirken sie wie Messfehler.
    let maxHoehe = 1;
    for (const p of profil) maxHoehe = Math.max(maxHoehe, p.hoehe || 0);
    stift.beginPath();
    stift.moveTo(x(0), hoehe);
    for (const p of profil) {
      stift.lineTo(x(p.km), hoehe - ((p.hoehe || 0) / maxHoehe) * (hoehe * 0.35));
    }
    stift.lineTo(x(maxKm), hoehe);
    stift.closePath();
    stift.fillStyle = "#2a333d66";
    stift.fill();

    // Reservelinie
    stift.beginPath();
    stift.moveTo(4, y(reserve));
    stift.lineTo(breite - 4, y(reserve));
    stift.strokeStyle = "#e2596a88";
    stift.setLineDash([4, 4]);
    stift.lineWidth = 1;
    stift.stroke();
    stift.setLineDash([]);

    // Ladestand
    stift.beginPath();
    profil.forEach((p, i) => {
      const px = x(p.km), py = y(p.soc);
      if (i === 0) stift.moveTo(px, py); else stift.lineTo(px, py);
    });
    stift.strokeStyle = "#ffc93c";
    stift.lineWidth = 2;
    stift.stroke();

    stift.font = "11px system-ui, sans-serif";
    stift.fillStyle = "#8a97a5";
    stift.fillText(`Reserve ${reserve} %`, 6, y(reserve) - 4);
    document.getElementById("profil-legende").textContent =
      `0 – ${K.zahl(maxKm)} km · bis ${K.zahl(maxHoehe)} m`;
  }

  /* ---------- Ladepunkte ---------- */

  async function saeulenLaden() {
    const fahrt = K.zustand.fahrt;
    const liste = document.getElementById("saeulen");
    if (!fahrt || !liste) return;

    liste.innerHTML = '<li class="leer">sucht …</li>';
    try {
      const antwort = await K.api(`/api/saeulen/entlang/${fahrt.fahrt_id}`
        + `?min_kw=${document.getElementById("min-kw").value}`
        + `&radius_km=${document.getElementById("radius").value}`);
      zeichneSaeulen(antwort, liste);
      letzteSaeulen = antwort.kandidaten || [];
      markerSetzen();
    } catch (fehler) {
      liste.innerHTML = "";
      K.melden("Ladepunkte: " + fehler.message, "fehler");
    }
  }

  /* ---------- Ladeplan ---------- */

  /* Was ein Halt kostet, bevor geladen wird - der Regler in der
   * Ladeplan-Ansicht. Fehlt er (alte Oberfläche im Cache), gilt die Vorgabe
   * des Servers, statt eine Null zu schicken und den Plan zu zersplittern. */
  function haltekosten() { return regler("haltekosten", 5); }

  function regler(id, vorgabe) {
    const el = document.getElementById(id);
    return el ? el.value : vorgabe;
  }

  async function ladeplanLaden() {
    const fahrt = K.zustand.fahrt;
    const liste = document.getElementById("ladeplan");
    const werte = document.getElementById("ladeplan-werte");
    if (!fahrt || !liste || !werte) return;

    liste.innerHTML = '<li class="leer">plant …</li>';
    werte.innerHTML = "";
    try {
      const plan = await K.api(`/api/fahrten/${fahrt.fahrt_id}/ladeplan`
        + `?min_kw=${document.getElementById("min-kw").value}`
        + `&radius_km=${document.getElementById("radius").value}`
        + `&stopp_fixkosten_min=${haltekosten()}`
        + `&ladepark_bonus_min=${regler("ladepark", 4)}`,
        { method: "POST" });
      letzterPlan = plan;
      zeichnePlan(plan, liste, werte);
      markerSetzen();
    } catch (fehler) {
      liste.innerHTML = "";
      letzterPlan = null;
      K.melden("Ladeplan: " + fehler.message, "fehler");
    }
  }

  function zeichnePlan(plan, liste, werte) {
    if (!plan.machbar) {
      liste.innerHTML = `<li class="leer">${entschaerfen(plan.grund)}</li>`;
      return;
    }

    werte.innerHTML = [
      K.wertKachel("Gesamt", K.dauer(plan.gesamt_minuten)),
      K.wertKachel("davon Laden", K.dauer(plan.ladezeit_minuten)),
      K.wertKachel("davon Umwege", K.dauer(plan.umwegzeit_minuten)),
      // Sichtbar machen, was die blosse Anzahl der Halte kostet - sonst ist
      // der Regler daneben eine Zahl ohne Wirkung, die man sehen kann.
      K.wertKachel("davon Halte", K.dauer(plan.haltekosten_minuten)),
      K.wertKachel("Stopps", String(plan.anzahl_stopps)),
      K.wertKachel("Am Ziel", K.zahl(plan.soc_am_ziel) + " %",
                   plan.soc_am_ziel >= 15 ? "gut" : ""),
    ].join("");

    if (!plan.anzahl_stopps) {
      liste.innerHTML = '<li class="leer">Kein Ladestopp nötig – die Strecke '
        + 'reicht mit dem Ladestand beim Start.</li>';
      return;
    }

    liste.innerHTML = "";
    plan.stopps.forEach((s, i) => {
      const eintrag = document.createElement("li");
      // Der Ausweichstandort ist der Grund, warum man vor einer belegten Säule
      // nicht neu suchen muss - er gehört sichtbar an den Stopp, nicht in ein
      // Untermenü. Fehlt er, ist auch das eine Aussage.
      //
      // Wie dringend sie ist, hängt am Stopp selbst: Ein Ladepark mit zwölf
      // Punkten braucht kaum einen Rückfallplan, ein einzelner Lader schon.
      // Deshalb ist die fehlende Ausweichmöglichkeit nur dort rot, wo sie
      // wirklich weh tut - sonst wäre die Warnung an jedem Stopp zu lesen und
      // damit an keinem.
      const knapp = (s.anzahl_punkte || 1) < 4;
      const ausweich = s.ausweich
        ? `<div class="unter">Ausweich: ${entschaerfen(s.ausweich.name
            || s.ausweich.betreiber || "Ladepunkt")} bei km
            ${K.zahl(s.ausweich.km_auf_route)} – Ankunft mit
            ${K.zahl(s.ausweich.ankunft_soc)} %</div>`
        : `<div class="unter"${knapp ? ' style="color:#e2596a"' : ""}>Kein
            Ausweichstandort ohne Nachladen erreichbar${knapp
              ? " – und hier stehen nur wenige Ladepunkte" : ""}</div>`;

      eintrag.innerHTML = `
        <div class="haupt">
          <div class="titel">${i + 1}. ${entschaerfen(s.name || s.betreiber
            || "Ladepunkt")}</div>
          <div class="unter">km ${K.zahl(s.km_auf_route)} · nach
            ${K.dauer(s.ankunft_minute)} · ${K.zahl(s.ankunft_soc)} %
            → ${K.zahl(s.abfahrt_soc)} % · ${K.zahl(s.kwh_geladen, 1)} kWh
            · Umweg ${K.zahl(s.umweg_minuten, 1)} min
            · ${s.anzahl_punkte} Ladepunkte</div>
          ${ausweich}
        </div>
        <div class="kw">${K.dauer(s.ladezeit_minuten)}</div>`;
      liste.appendChild(eintrag);
    });
  }

  function zeichneSaeulen(antwort, liste) {
    if (!antwort.anzahl) {
      liste.innerHTML = '<li class="leer">Keine passenden Ladepunkte gefunden. '
        + 'Ist das Ladesäulenregister schon importiert? '
        + '(tools/import_bnetza.py)</li>';
      return;
    }

    liste.innerHTML = "";
    for (const k of antwort.kandidaten.slice(0, 60)) {
      const eintrag = document.createElement("li");
      const belegt = k.belegt_gemeldet
        ? ' · <span style="color:#e2596a">als belegt gemeldet</span>' : "";
      eintrag.innerHTML = `
        <div class="haupt">
          <div class="titel">${entschaerfen(k.name || k.betreiber || "Ladepunkt")}</div>
          <div class="unter">km ${K.zahl(k.km_auf_route)} · Umweg
            ${K.zahl(k.umweg_minuten, 1)} min · ${k.anzahl_punkte} Ladepunkte
            · ${entschaerfen(k.steckertypen)}${belegt}</div>
        </div>
        <div class="kw">${K.zahl(k.max_kw)} kW</div>`;

      const knopf = document.createElement("button");
      knopf.className = "tat neben";
      knopf.style.width = "auto";
      knopf.style.margin = "0";
      knopf.style.padding = "6px 10px";
      knopf.style.fontSize = "12px";
      knopf.textContent = k.belegt_gemeldet ? "frei" : "belegt";
      knopf.addEventListener("click", async () => {
        try {
          await K.api(`/api/saeulen/${k.id}/belegt`,
                      { method: k.belegt_gemeldet ? "DELETE" : "POST" });
          await saeulenLaden();
          // "Hier ist alles voll" ist die einzige Verfügbarkeitsinformation,
          // die wirklich stimmt - sie muss den Plan ändern, nicht nur die
          // Liste einfärben.
          await ladeplanLaden();
        } catch (fehler) { K.melden(fehler.message, "fehler"); }
      });
      eintrag.appendChild(knopf);
      liste.appendChild(eintrag);
    }
  }

  /* Die Namen kommen aus fremden Datenquellen und landen in innerHTML. */
  function entschaerfen(text) {
    const hilfe = document.createElement("div");
    hilfe.textContent = text || "";
    return hilfe.innerHTML;
  }

  /* ---------- Zuladung ---------- */

  /* Der Regler kennt einen Wert unterhalb seines Minimums als "nicht
   * gesetzt". Das erspart einen zweiten Schalter für die Frage "eigene
   * Zuladung oder die aus dem Profil?" - ganz links heisst Profil. */
  function zuladungWert() {
    const regler = document.getElementById("zuladung");
    if (!regler) return null;
    const wert = Number(regler.value);
    return wert < 0 ? null : wert;
  }

  function zuladungKoppeln() {
    const regler = document.getElementById("zuladung");
    const anzeige = document.getElementById("zuladung-wert");
    if (!regler || !anzeige) return;
    const aktualisieren = () => {
      const wert = zuladungWert();
      anzeige.textContent = wert === null ? "wie im Profil" : wert + " kg";
    };
    regler.addEventListener("input", aktualisieren);
    aktualisieren();
  }

  /* ---------- Einrichten ---------- */

  function einrichten() {
    ortsucheEinrichten("start", "start-treffer", "start");
    ortsucheEinrichten("ziel", "ziel-treffer", "ziel");
    K.reglerKoppeln("start-soc", "start-soc-wert");
    K.reglerKoppeln("tempo", "tempo-wert");
    zuladungKoppeln();

    let warten = null;
    let wartenPlan = null;
    const neuLaden = () => {
      clearTimeout(warten);
      // Beide Regler wirken auf denselben Kandidatensatz - der Ladeplan muss
      // mitziehen, sonst zeigt die Karte Stopps, die es nach der neuen
      // Filterung gar nicht mehr gibt.
      warten = setTimeout(async () => {
        await saeulenLaden();
        await ladeplanLaden();
      }, 350);
    };
    K.reglerKoppeln("min-kw", "min-kw-wert", neuLaden);
    K.reglerKoppeln("radius", "radius-wert", neuLaden);
    // Der Aufwand je Halt ändert nur die Planung, nicht die Kandidaten -
    // deshalb ohne saeulenLaden(), sonst flackert die Säulenliste ohne Grund.
    // Beide Regler ändern nur die Planung, nicht die Kandidaten - deshalb
    // ohne saeulenLaden(), sonst flackert die Säulenliste ohne Grund.
    const planNachziehen = () => {
      clearTimeout(wartenPlan);
      wartenPlan = setTimeout(ladeplanLaden, 350);
    };
    K.reglerKoppeln("haltekosten", "haltekosten-wert", planNachziehen);
    K.reglerKoppeln("ladepark", "ladepark-wert", planNachziehen);

    K.an("rechnen", "click", rechnen);
    window.addEventListener("resize", () => {
      if (K.zustand.fahrt) profilZeichnen(K.zustand.fahrt);
    });
  }

  return { einrichten, anzeigen, saeulenLaden, ladeplanLaden, varianteWaehlen,
           fahrtLaden };
})();
