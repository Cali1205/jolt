/* Die Kacheln der CarPlay-Übersicht, gezeichnet in der Oberfläche.
 *
 * CarPlay zeichnet für eine Kachel nur ein Bild und einen Titel - alles
 * andere muss ins Bild. Bisher hat Swift die Bilder gezeichnet
 * (JoltCarPlaySceneDelegate.kachelBild). Hier entstehen sie stattdessen im
 * Canvas und gehen als PNG im Anzeigemodell mit:
 *
 *  - Man sieht sie, bevor man ins Auto steigt: Die Einstellungen zeigen jede
 *    Kachel in jedem Stil mit Probewerten, im Browser genauso wie in der App.
 *  - Eine Änderung am Aussehen ist eine Änderung an dieser Datei. Die
 *    Oberfläche kommt vom Server (server.url), ein neuer App-Bau ist dafür
 *    nicht nötig.
 *  - Fehlt dem Telefon das Bild (ältere App, anderer Stil), zeichnet Swift
 *    wie bisher selbst.
 *
 * Zwei Stile, beide dunkel und mit eigenem Grund, damit sie bei Tag und Nacht
 * gleich aussehen (CarPlay färbt nur den Titel unter der Kachel um):
 *
 *   "a"  Instrument   Runde Anzeigen mit Skala: Ladestand und Rekuperation
 *                     als Zeiger-Bogen, Verbrauch als Balken über einer
 *                     Mittellinie, Ankunft mit Pfeil, Reserve mit Reichweiten-
 *                     leiste.
 *   "b"  Telemetrie   Zahlen links, Segmentleisten wie bei einem Rennwagen:
 *                     Ladestand als LED-Reihe, Ankunft als Abweichung von
 *                     Null, Stopps als Streckenband, Verläufe als Fläche.
 *
 * Gezeichnet wird in einem Quadrat von 120 Einheiten; die Pixelzahl kommt aus
 * `massstab` (2 = 240 Pixel). Nichts hier berührt das DOM ausser dem
 * Canvas, den der Aufrufer liefert - die Zeichenfunktionen lassen sich mit
 * einem Aufzeichner statt eines Canvas prüfen (tools/check_kacheln.js).
 */
window.joltKacheln = (function () {
  "use strict";

  const SEITE = 120;
  const ist = (x) => typeof x === "number" && Number.isFinite(x);

  const FARBE = {
    grund1: "#141f2b", grund2: "#0a1119",
    rand: "#27384a", spur: "#1c2937", raster: "#233343",
    text: "#f2f7fb", gedaempft: "#8696a6",
    akzent: "#37d4ff", gut: "#36e08b", warn: "#ffb52e", schlecht: "#ff4f5f",
  };
  const ZAHL = '"SF Mono", ui-monospace, Menlo, Consolas, "DejaVu Sans Mono", monospace';
  const TEXT = '-apple-system, "SF Pro Text", system-ui, "Segoe UI", Roboto, sans-serif';

  /* ---------- Was welche Farbe heisst ---------- */

  /* Ladestand: unter 20 % ist es Zeit zu handeln, unter 10 % dringend. */
  function farbeSoc(p) {
    return p >= 35 ? FARBE.gut : (p >= 20 ? FARBE.warn : FARBE.schlecht);
  }
  /* Ankunft: Minuten gegenüber dem Plan. */
  function farbeAnkunft(min) {
    return min <= 2 ? FARBE.gut : (min <= 15 ? FARBE.warn : FARBE.schlecht);
  }
  /* Reserve: Kilometer bis zum Ladestand, den der Plan nicht unterschreiten will. */
  function farbeReserve(km) {
    return km >= 100 ? FARBE.gut : (km >= 40 ? FARBE.warn : FARBE.schlecht);
  }

  /* ---------- Zeichenhilfen ---------- */

  function rund(c, x, y, w, h, r) {
    c.beginPath();
    c.moveTo(x + r, y);
    c.lineTo(x + w - r, y); c.arcTo(x + w, y, x + w, y + r, r);
    c.lineTo(x + w, y + h - r); c.arcTo(x + w, y + h, x + w - r, y + h, r);
    c.lineTo(x + r, y + h); c.arcTo(x, y + h, x, y + h - r, r);
    c.lineTo(x, y + r); c.arcTo(x, y, x + r, y, r);
    c.closePath();
  }

  function grund(c) {
    const lauf = c.createLinearGradient(0, 0, 0, SEITE);
    lauf.addColorStop(0, FARBE.grund1);
    lauf.addColorStop(1, FARBE.grund2);
    rund(c, 0.5, 0.5, SEITE - 1, SEITE - 1, 17);
    c.fillStyle = lauf;
    c.fill();
    c.lineWidth = 1;
    c.strokeStyle = FARBE.rand;
    c.stroke();
  }

  function schreiben(c, s, x, y, groesse, farbe, opt) {
    const o = opt || {};
    c.font = `${o.gewicht || 600} ${groesse}px ${o.schrift || TEXT}`;
    c.fillStyle = farbe;
    c.textAlign = o.ausrichtung || "left";
    c.textBaseline = o.grundlinie || "alphabetic";
    c.fillText(s, x, y);
  }

  /* Eine Zahl so gross wie möglich, ohne `breite` zu überschreiten. */
  function zahlPassend(c, s, x, y, startGroesse, breite, farbe, ausrichtung) {
    let g = startGroesse;
    do {
      c.font = `700 ${g}px ${ZAHL}`;
      g -= 1;
    } while (c.measureText(s).width > breite && g > 10);
    schreiben(c, s, x, y, g + 1, farbe,
              { schrift: ZAHL, gewicht: 700, ausrichtung });
  }

  function leuchten(c, farbe, staerke) {
    c.shadowColor = farbe;
    c.shadowBlur = staerke;
  }
  function ohneLeuchten(c) { c.shadowBlur = 0; c.shadowColor = "transparent"; }

  /* Ein Bogen von `von` nach `bis` (Bogenmass, im Uhrzeigersinn). */
  function bogen(c, mx, my, r, von, bis, breite, farbe, kappe) {
    c.beginPath();
    c.arc(mx, my, r, von, bis);
    c.lineWidth = breite;
    c.lineCap = kappe || "round";
    c.strokeStyle = farbe;
    c.stroke();
  }

  function leer(c, titel) {
    grund(c);
    c.setLineDash([3, 4]);
    c.strokeStyle = FARBE.raster;
    c.lineWidth = 1.5;
    rund(c, 12, 12, SEITE - 24, SEITE - 24, 10);
    c.stroke();
    c.setLineDash([]);
    schreiben(c, "–", SEITE / 2, SEITE / 2 + 12, 36, FARBE.gedaempft,
              { ausrichtung: "center", schrift: ZAHL });
  }

  /* Verlauf als Linie mit Fläche darunter. `reihe` in beliebiger Einheit. */
  function verlauf(c, reihe, x, y, b, h, farbe, optionen) {
    const o = optionen || {};
    const tief = o.tief !== undefined ? o.tief : Math.min(...reihe);
    let hoch = o.hoch !== undefined ? o.hoch : Math.max(...reihe);
    if (hoch - tief < 1e-6) hoch = tief + 1;
    const px = (i) => x + (i / (reihe.length - 1)) * b;
    const py = (w) => y + h - ((w - tief) / (hoch - tief)) * h;

    if (o.raster) {
      c.strokeStyle = FARBE.raster; c.lineWidth = 1;
      for (let k = 0; k <= 2; k++) {
        const yy = Math.round(y + (h * k) / 2) + 0.5;
        c.beginPath(); c.moveTo(x, yy); c.lineTo(x + b, yy); c.stroke();
      }
    }
    const flaeche = c.createLinearGradient(0, y, 0, y + h);
    flaeche.addColorStop(0, farbe + "66");
    flaeche.addColorStop(1, farbe + "00");
    c.beginPath();
    c.moveTo(px(0), y + h);
    reihe.forEach((w, i) => c.lineTo(px(i), py(w)));
    c.lineTo(px(reihe.length - 1), y + h);
    c.closePath();
    c.fillStyle = flaeche;
    c.fill();

    c.beginPath();
    reihe.forEach((w, i) => (i ? c.lineTo(px(i), py(w)) : c.moveTo(px(i), py(w))));
    c.lineWidth = 2.5; c.lineJoin = "round"; c.lineCap = "round";
    c.strokeStyle = farbe;
    leuchten(c, farbe, 6);
    c.stroke();
    ohneLeuchten(c);

    const ex = px(reihe.length - 1), ey = py(reihe[reihe.length - 1]);
    c.beginPath(); c.arc(ex, ey, 3.4, 0, Math.PI * 2);
    c.fillStyle = FARBE.text; c.fill();
  }

  /* ---------- Stil A: Instrument ---------- */

  const START = Math.PI * 0.75;          // 135 Grad: links unten
  const SPANNE = Math.PI * 1.5;          // 270 Grad

  /* Zeigerbogen mit Skala: Ladestand und Rekuperation. */
  function zeigerA(c, wert, farbe, einheit, nebenzeile) {
    grund(c);
    const mx = SEITE / 2, my = 61, r = 44;
    // Skala: elf Striche, alle 10 %.
    for (let i = 0; i <= 10; i++) {
      const w = START + (SPANNE * i) / 10;
      const gross = i % 5 === 0;
      c.beginPath();
      c.moveTo(mx + Math.cos(w) * (r - (gross ? 13 : 11)), my + Math.sin(w) * (r - (gross ? 13 : 11)));
      c.lineTo(mx + Math.cos(w) * (r - 8), my + Math.sin(w) * (r - 8));
      c.lineWidth = gross ? 2 : 1;
      c.strokeStyle = gross ? FARBE.gedaempft : FARBE.raster;
      c.stroke();
    }
    bogen(c, mx, my, r, START, START + SPANNE, 9, FARBE.spur);
    const anteil = Math.max(0, Math.min(1, wert / 100));
    if (anteil > 0.004) {
      leuchten(c, farbe, 9);
      bogen(c, mx, my, r, START, START + SPANNE * anteil, 9, farbe);
      ohneLeuchten(c);
      // Zeigerkopf: heller Punkt am Ende des Bogens.
      const w = START + SPANNE * anteil;
      c.beginPath(); c.arc(mx + Math.cos(w) * r, my + Math.sin(w) * r, 2.6, 0, Math.PI * 2);
      c.fillStyle = FARBE.text; c.fill();
    }
    zahlPassend(c, String(Math.round(wert)), mx, my + 11, 36, 58, FARBE.text, "center");
    schreiben(c, einheit, mx, my + 28, 15, FARBE.gedaempft,
              { ausrichtung: "center", gewicht: 600 });
    if (nebenzeile) {
      schreiben(c, nebenzeile, mx, SEITE - 5, 12, FARBE.gedaempft,
                { ausrichtung: "center", gewicht: 500 });
    }
  }

  function balkenA(c, d) {
    grund(c);
    const farbe = FARBE.akzent;
    zahlPassend(c, d.text, SEITE / 2, 34, 30, 92, FARBE.text, "center");
    schreiben(c, "kWh/100 km", SEITE / 2, 50, 12.5, FARBE.gedaempft,
              { ausrichtung: "center", gewicht: 600 });
    const x0 = 13, y0 = 58, b = SEITE - 26, h = 40;
    schreiben(c, "−30 min", x0, 114, 11.5, FARBE.gedaempft, { gewicht: 500 });
    schreiben(c, "jetzt", x0 + b, 114, 11.5, FARBE.gedaempft,
              { ausrichtung: "right", gewicht: 500 });
    const werte = (d.balken || []).map((w) => (ist(w) ? w : null));
    const echte = werte.filter((w) => w !== null);
    if (!echte.length) return;
    const mittel = echte.reduce((a, w) => a + w, 0) / echte.length;
    const spitze = Math.max(...echte, mittel * 1.15, 1);
    const n = werte.length, luecke = 4;
    const bb = (b - luecke * (n - 1)) / n;
    // Grundlinie und Mittellinie.
    c.strokeStyle = FARBE.gedaempft; c.lineWidth = 1;
    c.beginPath(); c.moveTo(x0, y0 + h + 0.5); c.lineTo(x0 + b, y0 + h + 0.5); c.stroke();
    werte.forEach((w, i) => {
      const x = x0 + i * (bb + luecke);
      if (w === null) {
        c.fillStyle = FARBE.raster; c.fillRect(x, y0 + h - 2, bb, 2);
        return;
      }
      const hh = Math.max(3, (w / spitze) * h);
      const f = w > mittel * 1.12 ? FARBE.warn : farbe;
      const lauf = c.createLinearGradient(0, y0 + h - hh, 0, y0 + h);
      lauf.addColorStop(0, f); lauf.addColorStop(1, f + "55");
      rund(c, x, y0 + h - hh, bb, hh, 2.2);
      c.fillStyle = lauf; c.fill();
    });
    const ym = y0 + h - (mittel / spitze) * h;
    c.setLineDash([3, 3]); c.strokeStyle = FARBE.text + "aa"; c.lineWidth = 1;
    c.beginPath(); c.moveTo(x0 - 3, ym); c.lineTo(x0 + b + 3, ym); c.stroke();
    c.setLineDash([]);
  }

  function verlaufA(c, d, einheit, farbe) {
    grund(c);
    zahlPassend(c, d.text, SEITE / 2, 38, 32, 90, FARBE.text, "center");
    schreiben(c, einheit, SEITE / 2, 54, 14, FARBE.gedaempft,
              { ausrichtung: "center", gewicht: 600 });
    if (d.reihe && d.reihe.length >= 2) {
      verlauf(c, d.reihe, 12, 66, SEITE - 24, 38, farbe, { raster: false });
    }
  }

  function ankunftA(c, d) {
    grund(c);
    const farbe = farbeAnkunft(d.min);
    const nachPlan = Math.abs(d.min) < 1;
    const spaeter = d.min > 0;
    // Symbol: Pfeil nach oben (später) oder unten (früher), Haken bei "nach Plan".
    const mx = SEITE / 2;
    c.lineWidth = 4.5; c.lineCap = "round"; c.lineJoin = "round"; c.strokeStyle = farbe;
    leuchten(c, farbe, 9);
    c.beginPath();
    if (nachPlan) {
      c.moveTo(mx - 12, 28); c.lineTo(mx - 3, 37); c.lineTo(mx + 13, 18);
    } else if (spaeter) {
      c.moveTo(mx - 12, 32); c.lineTo(mx, 19); c.lineTo(mx + 12, 32);
    } else {
      c.moveTo(mx - 12, 19); c.lineTo(mx, 32); c.lineTo(mx + 12, 19);
    }
    c.stroke();
    ohneLeuchten(c);
    const lang = Math.abs(d.min) >= 60;
    const gross = nachPlan ? "im Plan"
      : (lang ? d.text.replace("−", "-") : `${spaeter ? "+" : "−"}${Math.abs(d.min)}`);
    zahlPassend(c, gross, mx, 76, 38, 96, FARBE.text, "center");
    schreiben(c, nachPlan ? "Ankunft" : (lang ? (spaeter ? "später" : "früher")
              : (spaeter ? "min später" : "min früher")),
              mx, 100, 15, farbe, { ausrichtung: "center", gewicht: 600 });
  }

  function reserveA(c, d) {
    grund(c);
    const farbe = farbeReserve(d.km);
    zahlPassend(c, String(Math.round(d.km)), SEITE / 2, 54, 38, 90, FARBE.text, "center");
    schreiben(c, "km bis Reserve", SEITE / 2, 72, 12, FARBE.gedaempft,
              { ausrichtung: "center", gewicht: 600 });
    // Reichweitenleiste: 0 bis 300 km.
    const x = 14, y = 87, b = SEITE - 28, h = 10;
    rund(c, x, y, b, h, 4.5); c.fillStyle = FARBE.spur; c.fill();
    const anteil = Math.max(0.02, Math.min(1, d.km / 300));
    leuchten(c, farbe, 7);
    rund(c, x, y, b * anteil, h, 4.5); c.fillStyle = farbe; c.fill();
    ohneLeuchten(c);
    c.strokeStyle = FARBE.gedaempft; c.lineWidth = 1;
    [0.33, 0.66].forEach((t) => {
      c.beginPath(); c.moveTo(x + b * t, y + h + 2); c.lineTo(x + b * t, y + h + 6); c.stroke();
    });
  }

  function stoppsA(c, d) {
    grund(c);
    zahlPassend(c, String(d.anzahl), SEITE / 2, 56, 46, 60, FARBE.text, "center");
    // Punkte: ein Punkt je Stopp, der nächste hell.
    const n = Math.min(d.anzahl, 8), abstand = 14;
    const x0 = SEITE / 2 - ((n - 1) * abstand) / 2;
    for (let i = 0; i < n; i++) {
      c.beginPath(); c.arc(x0 + i * abstand, 78, i === 0 ? 4.4 : 3.2, 0, Math.PI * 2);
      c.fillStyle = i === 0 ? FARBE.akzent : FARBE.gedaempft;
      if (i === 0) leuchten(c, FARBE.akzent, 7);
      c.fill(); ohneLeuchten(c);
    }
    if (d.naechster) {
      schreiben(c, "in " + d.naechster, SEITE / 2, 103, 15, FARBE.akzent,
                { ausrichtung: "center", gewicht: 600 });
    }
  }

  /* ---------- Stil B: Telemetrie ---------- */

  /* Die senkrechte Statusleiste links: Farbe sagt, wie es steht. */
  function streifen(c, farbe) {
    leuchten(c, farbe, 6);
    rund(c, 7, 14, 4, SEITE - 28, 2);
    c.fillStyle = farbe; c.fill();
    ohneLeuchten(c);
  }

  /* LED-Leiste: `n` Segmente, `gefuellt` davon leuchten. */
  function segmente(c, x, y, b, h, n, anteil, farbeFn) {
    const luecke = 1.8, sb = (b - luecke * (n - 1)) / n;
    const aktiv = Math.round(Math.max(0, Math.min(1, anteil)) * n);
    for (let i = 0; i < n; i++) {
      const sx = x + i * (sb + luecke);
      const an = i < aktiv;
      const f = farbeFn(i / (n - 1));
      rund(c, sx, y, sb, h, 1.2);
      if (an) { leuchten(c, f, 4); c.fillStyle = f; } else { c.fillStyle = FARBE.spur; }
      c.fill();
      ohneLeuchten(c);
    }
  }

  /* Zahl links, Einheit klein daneben auf derselben Grundlinie. */
  function zahlLinks(c, zahl, einheit, y, startGroesse, breite) {
    c.font = `700 ${startGroesse}px ${ZAHL}`;
    let g = startGroesse;
    const einheitBreite = einheit ? einheit.length * 6.2 + 4 : 0;
    while (c.measureText(zahl).width + einheitBreite > breite && g > 12) {
      g -= 1; c.font = `700 ${g}px ${ZAHL}`;
    }
    const w = c.measureText(zahl).width;
    schreiben(c, zahl, 17, y, g, FARBE.text, { schrift: ZAHL, gewicht: 700 });
    if (einheit) {
      schreiben(c, einheit, 17 + w + 4, y, 12, FARBE.gedaempft, { gewicht: 600 });
    }
  }

  function leisteB(c, wert, farbe, einheit, skala) {
    grund(c);
    streifen(c, farbe);
    zahlLinks(c, String(Math.round(wert)), einheit, 56, 44, 92);
    segmente(c, 17, 70, SEITE - 31, 17, 16, wert / 100, () => farbe);
    // Skala unter der Leiste.
    const marken = skala || ["0", "50", "100"];
    marken.forEach((m, i) => {
      const x = 17 + ((SEITE - 31) * i) / (marken.length - 1);
      schreiben(c, m, x, 108, 11.5, FARBE.gedaempft,
                { ausrichtung: i === 0 ? "left" : (i === marken.length - 1 ? "right" : "center"),
                  gewicht: 500, schrift: ZAHL });
    });
  }

  function socB(c, d) {
    const farbe = farbeSoc(d.wert);
    grund(c);
    streifen(c, farbe);
    zahlLinks(c, String(Math.round(d.wert)), "%", 56, 44, 92);
    // Verlauf: Pfeil für den Trend gegenüber der Reihe.
    if (d.reihe && d.reihe.length >= 2) {
      const diff = d.reihe[d.reihe.length - 1] - d.reihe[0];
      if (Math.abs(diff) >= 0.5) {
        const auf = diff > 0;
        c.beginPath();
        // Spitze oben bei steigendem, unten bei fallendem Ladestand.
        c.moveTo(SEITE - 20, auf ? 21 : 31); c.lineTo(SEITE - 13, auf ? 31 : 21); c.lineTo(SEITE - 27, auf ? 31 : 21);
        c.closePath();
        c.fillStyle = auf ? FARBE.gut : FARBE.warn; c.fill();
      }
    }
    // Segmentleiste mit Farbverlauf nach Füllstand: unten rot, mitte gelb, oben grün.
    segmente(c, 17, 70, SEITE - 31, 17, 20, d.wert / 100, (t) => farbeSoc(t * 100));
    ["0", "50", "100"].forEach((m, i) => {
      schreiben(c, m, 17 + ((SEITE - 31) * i) / 2, 108, 11.5, FARBE.gedaempft,
                { ausrichtung: i === 0 ? "left" : (i === 2 ? "right" : "center"),
                  gewicht: 500, schrift: ZAHL });
    });
  }

  function balkenB(c, d) {
    grund(c);
    streifen(c, FARBE.akzent);
    zahlLinks(c, d.text, "", 40, 34, 92);
    schreiben(c, "kWh/100 km", 17, 54, 12.5, FARBE.gedaempft, { gewicht: 600 });
    const x0 = 17, y0 = 62, b = SEITE - 31, h = 38;
    schreiben(c, "−30", x0, 114, 11.5, FARBE.gedaempft, { gewicht: 500 });
    schreiben(c, "jetzt", x0 + b, 114, 11.5, FARBE.gedaempft,
              { ausrichtung: "right", gewicht: 500 });
    const werte = (d.balken || []).map((w) => (ist(w) ? w : null));
    const echte = werte.filter((w) => w !== null);
    if (!echte.length) return;
    const spitze = Math.max(...echte, 1);
    c.strokeStyle = FARBE.raster; c.lineWidth = 1;
    for (let k = 0; k <= 2; k++) {
      const yy = Math.round(y0 + (h * k) / 2) + 0.5;
      c.beginPath(); c.moveTo(x0, yy); c.lineTo(x0 + b, yy); c.stroke();
    }
    const n = werte.length, luecke = 3.5, bb = (b - luecke * (n - 1)) / n;
    werte.forEach((w, i) => {
      const x = x0 + i * (bb + luecke);
      if (w === null) { c.fillStyle = FARBE.raster; c.fillRect(x, y0 + h - 2, bb, 2); return; }
      const hh = Math.max(3, (w / spitze) * h);
      const letzter = i === werte.length - 1;
      c.fillStyle = letzter ? FARBE.text : FARBE.akzent + "cc";
      c.fillRect(x, y0 + h - hh, bb, hh);
      c.fillStyle = letzter ? FARBE.text : FARBE.akzent;
      c.fillRect(x, y0 + h - hh, bb, 2.2);
    });
  }

  function verlaufB(c, d, einheit, farbe) {
    grund(c);
    streifen(c, farbe);
    zahlLinks(c, d.text, einheit, 44, 34, 92);
    if (d.reihe && d.reihe.length >= 2) {
      verlauf(c, d.reihe, 17, 62, SEITE - 31, 38, farbe, { raster: true });
      const hoch = Math.max(...d.reihe), tief = Math.min(...d.reihe);
      const fmt = (w) => (Math.abs(w) < 10 ? w.toFixed(1) : String(Math.round(w))).replace(".", ",");
      schreiben(c, fmt(hoch), SEITE - 14, 59, 11.5, FARBE.gedaempft,
                { ausrichtung: "right", gewicht: 500, schrift: ZAHL });
      schreiben(c, fmt(tief), SEITE - 14, 114, 11.5, FARBE.gedaempft,
                { ausrichtung: "right", gewicht: 500, schrift: ZAHL });
    }
  }

  function ankunftB(c, d) {
    const farbe = farbeAnkunft(d.min);
    grund(c);
    streifen(c, farbe);
    const nachPlan = Math.abs(d.min) < 1;
    const vorzeichen = d.min > 0 ? "+" : (d.min < 0 ? "−" : "");
    zahlLinks(c, nachPlan ? "Plan" : `${vorzeichen}${Math.abs(d.min) < 60 ? Math.abs(d.min) : d.text}`,
              nachPlan || Math.abs(d.min) >= 60 ? "" : "min", 56, 42, 92);
    // Abweichung von Null: Mitte = Plan, rechts = später, links = früher.
    const x = 17, y = 70, b = SEITE - 31, h = 14, mitte = x + b / 2;
    rund(c, x, y, b, h, 3); c.fillStyle = FARBE.spur; c.fill();
    const bereich = 30;
    const anteil = Math.max(-1, Math.min(1, d.min / bereich));
    if (!nachPlan) {
      leuchten(c, farbe, 6);
      c.fillStyle = farbe;
      if (anteil > 0) c.fillRect(mitte, y, (b / 2) * anteil, h);
      else c.fillRect(mitte + (b / 2) * anteil, y, (b / 2) * -anteil, h);
      ohneLeuchten(c);
    }
    c.fillStyle = FARBE.text; c.fillRect(mitte - 1, y - 4, 2, h + 8);
    schreiben(c, "früher", x, 108, 12, FARBE.gedaempft, { gewicht: 500 });
    schreiben(c, "später", x + b, 108, 12, FARBE.gedaempft, { ausrichtung: "right", gewicht: 500 });
  }

  function reserveB(c, d) {
    const farbe = farbeReserve(d.km);
    grund(c);
    streifen(c, farbe);
    zahlLinks(c, String(Math.round(d.km)), "km", 56, 44, 92);
    // Lineal: Teilstriche alle 50 km bis 300, eine Marke an der Reichweite.
    const x = 17, y = 68, b = SEITE - 31;
    c.strokeStyle = FARBE.raster; c.lineWidth = 1;
    c.beginPath(); c.moveTo(x, y + 12.5); c.lineTo(x + b, y + 12.5); c.stroke();
    for (let k = 0; k <= 6; k++) {
      const xx = Math.round(x + (b * k) / 6) + 0.5;
      c.beginPath(); c.moveTo(xx, y + 7); c.lineTo(xx, y + (k % 2 === 0 ? 20 : 16)); c.stroke();
    }
    const t = Math.max(0, Math.min(1, d.km / 300));
    leuchten(c, farbe, 7);
    c.beginPath();
    c.moveTo(x + b * t, y + 12); c.lineTo(x + b * t - 6, y - 2); c.lineTo(x + b * t + 6, y - 2);
    c.closePath(); c.fillStyle = farbe; c.fill();
    ohneLeuchten(c);
    schreiben(c, "bis Reserve", x, 108, 12.5, FARBE.gedaempft, { gewicht: 500 });
  }

  function stoppsB(c, d) {
    grund(c);
    streifen(c, FARBE.akzent);
    zahlLinks(c, String(d.anzahl), d.anzahl === 1 ? "Stopp" : "Stopps", 56, 44, 92);
    // Streckenband: Start links, Ziel rechts, ein Punkt je Stopp.
    const x = 17, y = 82, b = SEITE - 31;
    c.strokeStyle = FARBE.raster; c.lineWidth = 3; c.lineCap = "round";
    c.beginPath(); c.moveTo(x, y); c.lineTo(x + b, y); c.stroke();
    const kms = d.kms || [];
    const ende = Math.max(...kms, 1);
    kms.forEach((km, i) => {
      const xx = x + Math.min(1, km / (ende * 1.12)) * b;
      c.beginPath(); c.arc(xx, y, i === 0 ? 5 : 3.6, 0, Math.PI * 2);
      c.fillStyle = i === 0 ? FARBE.akzent : FARBE.gedaempft;
      if (i === 0) leuchten(c, FARBE.akzent, 8);
      c.fill(); ohneLeuchten(c);
    });
    c.fillStyle = FARBE.text; c.fillRect(x + b - 2, y - 7, 3, 14);
    if (d.naechster) {
      schreiben(c, "in " + d.naechster, x, 108, 14, FARBE.akzent, { gewicht: 600 });
    }
  }

  /* ---------- Zuordnung ---------- */

  const SLOTS = ["soc", "ankunft", "reserve", "verbrauch", "neben", "rekup", "stopps"];

  const ZEICHNER = {
    a: {
      soc: (c, d) => zeigerA(c, d.wert, farbeSoc(d.wert), "%", d.quelle === "gerechnet" ? "gerechnet" : null),
      ankunft: ankunftA,
      reserve: reserveA,
      verbrauch: balkenA,
      neben: (c, d) => verlaufA(c, d, "kW", FARBE.akzent),
      rekup: (c, d) => zeigerA(c, d.wert, FARBE.akzent, "%", null),
      stopps: stoppsA,
    },
    b: {
      soc: socB,
      ankunft: ankunftB,
      reserve: reserveB,
      verbrauch: balkenB,
      neben: (c, d) => verlaufB(c, d, "kW", FARBE.akzent),
      rekup: (c, d) => leisteB(c, d.wert, FARBE.akzent, "%", ["0", "50", "100"]),
      stopps: stoppsB,
    },
  };

  const STILE = [
    { id: "klassisch", name: "Klassisch (Swift zeichnet)" },
    { id: "a", name: "A – Instrument" },
    { id: "b", name: "B – Telemetrie" },
  ];

  /* Aus dem Anzeigemodell die Werte je Kachel - oder null, wenn dort etwas
   * fehlt. Es wird nichts ersetzt: Eine Kachel ohne Wert bleibt leer. */
  function daten(m, reihen) {
    const r = reihen || {};
    const aus = {};
    if (!m) return aus;
    aus.soc = m.soc && ist(m.soc.prozent)
      ? { wert: m.soc.prozent, quelle: m.soc.quelle, reihe: r.soc || null } : null;
    aus.ankunft = m.ankunft && ist(m.ankunft.min)
      ? { min: m.ankunft.min, text: m.ankunft.text } : null;
    aus.reserve = m.reserve && ist(m.reserve.km) ? { km: m.reserve.km, text: m.reserve.text } : null;

    const v = m.verlauf;
    const fenster = v && Array.isArray(v.fenster) ? v.fenster.filter((f) => f && ist(f.kwh100)) : [];
    const hatBalken = !!(v && Array.isArray(v.balken) && v.balken.some(ist));
    aus.verbrauch = fenster.length || hatBalken
      ? { text: fenster.length ? fenster[0].text : "–", balken: hatBalken ? v.balken : null } : null;

    const neben = m.neben;
    const nebenKw = neben && ist(neben.kw) ? neben.kw : null;
    aus.neben = nebenKw !== null
      ? { wert: nebenKw, text: String(nebenKw.toFixed(1)).replace(".", ","), reihe: r.neben || null } : null;

    aus.rekup = v && v.rekup && ist(v.rekup.prozent)
      ? { wert: v.rekup.prozent, reihe: r.rekup || null } : null;

    const liste = Array.isArray(m.stoppListe) ? m.stoppListe : [];
    aus.stopps = liste.length ? {
      anzahl: liste.length,
      naechster: liste[0] && liste[0].kmText ? liste[0].kmText : null,
      kms: liste.map((s) => s.km).filter(ist),
    } : null;
    return aus;
  }

  /* Eine Kachel auf einen vorbereiteten 2D-Kontext zeichnen. */
  function zeichnen(stil, slot, d, c) {
    const tabelle = ZEICHNER[stil];
    c.save();
    try {
      if (!tabelle || !tabelle[slot] || !d) leer(c, slot);
      else tabelle[slot](c, d);
    } finally { c.restore(); }
  }

  /* Eine Kachel als PNG (Base64 ohne Kopf).
   * `erzeuger(pixel)` liefert einen Canvas der Kantenlänge `pixel`. */
  function kachelPng(stil, slot, d, erzeuger, massstab) {
    const ms = massstab || 2;
    const leinwand = erzeuger(SEITE * ms);
    const c = leinwand.getContext("2d");
    c.scale(ms, ms);
    zeichnen(stil, slot, d, c);
    const url = leinwand.toDataURL("image/png");
    return String(url).replace(/^data:image\/png;base64,/, "");
  }

  function browserCanvas(pixel) {
    const k = document.createElement("canvas");
    k.width = pixel; k.height = pixel;
    return k;
  }

  /* Alle sieben Kacheln eines Stils. Eine Kachel ohne Wert wird als leere
   * gezeichnet (gestrichelter Rahmen, Strich): Sie soll zum Stil passen, nicht
   * wie ein Fremdkörper aus Swift aussehen. Feste Plätze - ein fehlender Wert
   * lässt die übrigen nicht nachrücken. */
  function bilder(stil, m, reihen, erzeuger) {
    if (!ZEICHNER[stil]) return null;
    const alle = daten(m, reihen);
    const aus = {};
    for (const slot of SLOTS) {
      aus[slot] = kachelPng(stil, slot, alle[slot] || null, erzeuger || browserCanvas, 2);
    }
    return aus;
  }

  /* Probewerte für die Vorschau in den Einstellungen und für Prüfungen. */
  function probe() {
    return {
      m: {
        soc: { prozent: 68, text: "68 %", quelle: "gemessen" },
        ankunft: { min: 12, text: "+12 min" },
        reserve: { km: 134, text: "134 km" },
        verlauf: {
          fenster: [{ min: 5, kwh100: 17.4, kw: 14.1, text: "17,4", kwText: "14,1" }],
          balken: [15.2, 18.9, 16.4, 21.7, 17.8, 17.4],
          rekup: { prozent: 23, minuten: 60 },
        },
        neben: { kw: 1.8, text: "1,8 kW" },
        stoppListe: [
          { name: "Fastned A7", km: 41, kmText: "41 km" },
          { name: "Ionity Hildesheim", km: 188, kmText: "188 km" },
        ],
      },
      reihen: {
        soc: [74, 73.6, 73.1, 72.4, 71.9, 71.2, 70.6, 70.1, 69.2, 68.6, 68],
        neben: [1.1, 1.3, 1.2, 1.9, 2.4, 2.1, 1.8, 1.6, 1.9, 1.8],
        rekup: [18, 19, 21, 20, 22, 23],
      },
    };
  }

  return { SEITE, SLOTS, STILE, daten, zeichnen, kachelPng, bilder, probe, browserCanvas,
           farbeSoc, farbeAnkunft, farbeReserve };
})();
