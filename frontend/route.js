/* Die Planen-Ansicht: Orte suchen, Route rechnen, Ergebnis zeigen. */
window.joltRoute = (function () {
  "use strict";

  const K = window.jolt;
  const gewaehlt = { start: null, ziel: null };

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
      }});
      K.zustand.fahrt = antwort;
      anzeigen(antwort);
      await saeulenLaden();
    } catch (fehler) {
      K.melden("Route: " + fehler.message, "fehler");
    } finally {
      knopf.disabled = false;
      knopf.textContent = "Route rechnen";
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
        + `${K.zahl(fahrt.reserve_bei_km)} km erreicht. Die Ladestopp-Planung `
        + `folgt in Stufe 2 – bis dahin zeigt die Karte, wo es eng wird.`,
        "warnung");
    }

    profilZeichnen(fahrt);

    window.joltKarte.routeSetzen(fahrt.geometrie);
    markerSetzen(fahrt, []);
    window.joltKarte.aufRoutePassen();
  }

  function markerSetzen(fahrt, saeulen) {
    const punkte = fahrt.geometrie || [];
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
    for (const s of saeulen) {
      liste.push({ lat: s.lat, lon: s.lon,
                   typ: s.belegt_gemeldet ? "saeuleBelegt" : "saeule" });
    }
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
      markerSetzen(fahrt, antwort.kandidaten);
    } catch (fehler) {
      liste.innerHTML = "";
      K.melden("Ladepunkte: " + fehler.message, "fehler");
    }
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

  /* ---------- Einrichten ---------- */

  function einrichten() {
    ortsucheEinrichten("start", "start-treffer", "start");
    ortsucheEinrichten("ziel", "ziel-treffer", "ziel");
    K.reglerKoppeln("start-soc", "start-soc-wert");
    K.reglerKoppeln("tempo", "tempo-wert");

    let warten = null;
    const neuLaden = () => { clearTimeout(warten); warten = setTimeout(saeulenLaden, 350); };
    K.reglerKoppeln("min-kw", "min-kw-wert", neuLaden);
    K.reglerKoppeln("radius", "radius-wert", neuLaden);

    K.an("rechnen", "click", rechnen);
    window.addEventListener("resize", () => {
      if (K.zustand.fahrt) profilZeichnen(K.zustand.fahrt);
    });
  }

  return { einrichten, anzeigen, saeulenLaden };
})();
