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
      const antwort = await K.api(`/api/live/start/${fahrt.fahrt_id}`,
                                  { method: "POST" });
      K.zustand.sitzungId = antwort.sitzung_id;
      document.getElementById("live-leer").hidden = true;
      document.getElementById("live-inhalt").hidden = false;
      verbinden(antwort.sitzung_id);
      window.joltApp.ansichtZeigen("live");
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
      K.wertKachel("Noch", K.zahl(z.rest_km) + " km"),
      // Ein negativer Ladestand ist keine Aussage über den Akku, sondern
      // darüber, dass es ohne Nachladen nicht reicht. Genau das gehört dann
      // auch dort zu stehen - "-188 %" liest niemand als Antwort.
      K.wertKachel("Am Ziel",
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
      window.joltKarte.markerSetzen(marker);
    }
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
    try {
      await K.api(`/api/live/${K.zustand.sitzungId}/simulieren`
        + `?mehrverbrauch=${mehr}&takt_s=0.3`, { method: "POST" });
      K.melden(`Simulation läuft mit ${Math.round(mehr * 100)} % Verbrauch.`,
               "hinweis");
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
    document.getElementById("live-inhalt").hidden = true;
    document.getElementById("live-leer").hidden = false;
    K.melden("Live-Fahrt beendet.", "hinweis");
  }

  function einrichten() {
    K.reglerKoppeln("mehrverbrauch", "mehrverbrauch-wert");
    K.an("live-starten", "click", starten);
    K.an("simulieren", "click", simulieren);
    K.an("live-beenden", "click", beenden);
    K.an("soc-melden", "click", socMelden);
  }

  return { einrichten, starten, beenden };
})();
