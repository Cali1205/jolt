/* Web Bluetooth gegen einen ELM327-Dongle.
 *
 * Der Dongle ist eine serielle Schnittstelle in BLE-Verkleidung: Man schreibt
 * ASCII-Befehle auf eine Charakteristik und bekommt die Antwort als
 * Notifications auf einer zweiten zurück, abgeschlossen von einem '>' als
 * Eingabeaufforderung. Mehr Protokoll gibt es nicht.
 *
 * Diese Seite rät bewusst wenig und zeigt viel: Jeder Befehl und jede Antwort
 * stehen im Protokoll. Ob die Annahmen über die PIDs des ID.Buzz stimmen,
 * entscheidet sich an dem, was das Auto zurückschickt - nicht an dem, was
 * hier steht.
 */
(function () {
  "use strict";

  /* Welchen GATT-Dienst ein ELM327-Klon anbietet, ist nicht genormt. Web
   * Bluetooth verlangt aber, dass man alle Dienste, die man anfassen will,
   * **vorher** anmeldet - man kann nicht erst verbinden und dann nachsehen.
   * Deshalb die Liste der gebräuchlichen. Der Vgate iCar Pro nutzt nach
   * verbreiteter Auskunft 0xFFF0; die anderen kosten nichts. */
  const DIENSTE = [
    0xfff0,   // Vgate, Veepeak, viele Klone
    0xffe0,   // HM-10-basiert
    0xffe5,
    0xfee7,
    0x18f0,
    "6e400001-b5a3-f393-e0a9-e50e24dcca9e",   // Nordic UART
  ];

  // Der Handshake. ATZ setzt zurück und braucht am längsten; ATE0 schaltet
  // das Echo aus, sonst kommt jeder Befehl als erste Zeile der Antwort
  // zurück. ATSP6 ist CAN 11 bit / 500 kBit - das Protokoll des MEB. ATCAF1
  // lässt den Dongle mehrteilige ISO-TP-Antworten selbst zusammensetzen;
  // ohne das kämen Rahmen einzeln und müssten hier sortiert werden.
  const HANDSHAKE = ["ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATSP6", "ATCAF1"];

  const el = (id) => document.getElementById(id);
  let schreiben = null;      // Charakteristik zum Senden
  let puffer = "";
  let warteAuf = null;       // {erfuellen, ablehnen, uhr}
  let letzterSoc = null;

  /* ---------- Protokoll ---------- */

  function log(text, art) {
    const zeit = new Date().toLocaleTimeString("de-DE");
    const zeichen = art === "raus" ? "→" : (art === "rein" ? "←" : " ");
    el("log").textContent += `${zeit} ${zeichen} ${text}\n`;
    el("log").scrollTop = el("log").scrollHeight;
  }

  function stand(text, art) {
    const k = el("verbindung");
    k.textContent = text;
    k.className = "stand " + (art || "");
  }

  function knoepfe(an) {
    for (const id of ["init", "soc", "senden", "melden"]) el(id).disabled = !an;
  }

  /* ---------- Verbinden ---------- */

  async function verbinden() {
    try {
      // acceptAllDevices statt eines Filters: Die Dongles heissen je nach
      // Charge anders, und ein Filter, der den eigenen nicht trifft, sieht
      // aus wie ein defekter Dongle.
      const geraet = await navigator.bluetooth.requestDevice({
        acceptAllDevices: true, optionalServices: DIENSTE });
      log(`Gerät gewählt: ${geraet.name || "(ohne Namen)"}`);
      geraet.addEventListener("gattserverdisconnected", () => {
        stand("Verbindung getrennt", "schlecht");
        knoepfe(false);
        log("Verbindung getrennt.");
      });

      stand("verbinde …");
      const server = await geraet.gatt.connect();

      // Den brauchbaren Dienst suchen: einer, der eine beschreibbare und eine
      // benachrichtigende Charakteristik hat. Bei manchen Dongles ist das
      // dieselbe.
      let notify = null;
      for (const dienst of await server.getPrimaryServices()) {
        const chars = await dienst.getCharacteristics();
        const w = chars.find((c) => c.properties.write
                                 || c.properties.writeWithoutResponse);
        const n = chars.find((c) => c.properties.notify);
        log(`Dienst ${dienst.uuid}: ${chars.length} Charakteristiken`);
        if (w && n) { schreiben = w; notify = n; break; }
      }
      if (!schreiben || !notify) {
        throw new Error("Kein Dienst mit Schreiben und Benachrichtigen "
                        + "gefunden. Die UUID des Dongles steht oben im "
                        + "Protokoll - sie gehört in die Liste DIENSTE.");
      }

      await notify.startNotifications();
      notify.addEventListener("characteristicvaluechanged", beiDaten);
      stand(`verbunden mit ${geraet.name || "Dongle"}`, "gut");
      knoepfe(true);
      log(`Bereit. Schreiben auf ${schreiben.uuid}, Lesen auf ${notify.uuid}`);
    } catch (fehler) {
      stand("Fehler: " + fehler.message, "schlecht");
      log("FEHLER " + fehler.message);
    }
  }

  /* ---------- Befehle ---------- */

  function beiDaten(e) {
    puffer += new TextDecoder().decode(e.target.value);
    // Der ELM327 schliesst jede Antwort mit '>' ab. Vorher ist sie
    // unvollständig - BLE liefert in Häppchen von rund zwanzig Byte.
    if (!puffer.includes(">")) return;
    const antwort = puffer.replace(/>/g, "").replace(/\r/g, "\n").trim();
    puffer = "";
    log(antwort || "(leer)", "rein");
    if (warteAuf) {
      clearTimeout(warteAuf.uhr);
      const { erfuellen } = warteAuf;
      warteAuf = null;
      erfuellen(antwort);
    }
  }

  function befehl(text, grenze_ms = 6000) {
    return new Promise((erfuellen, ablehnen) => {
      if (!schreiben) { ablehnen(new Error("nicht verbunden")); return; }
      if (warteAuf) { ablehnen(new Error("es läuft noch ein Befehl")); return; }
      log(text, "raus");
      puffer = "";
      warteAuf = {
        erfuellen,
        uhr: setTimeout(() => {
          warteAuf = null;
          // Ein Zeitablauf ist hier kein Absturz, sondern ein Befund: Der
          // Dongle hat nicht geantwortet, und das steht im Protokoll.
          log(`(keine Antwort auf ${text} innerhalb ${grenze_ms / 1000} s)`);
          ablehnen(new Error("Zeitüberschreitung bei " + text));
        }, grenze_ms),
      };
      const daten = new TextEncoder().encode(text + "\r");
      // writeValueWithoutResponse ist neuer als writeValue und fehlt in
      // manchen Umsetzungen - deshalb auf die Methode prüfen und nicht nur
      // auf die Eigenschaft der Charakteristik.
      const ohne_antwort = schreiben.properties.writeWithoutResponse
        && typeof schreiben.writeValueWithoutResponse === "function";
      const senden = ohne_antwort
        ? schreiben.writeValueWithoutResponse(daten)
        : schreiben.writeValue(daten);
      senden.catch((f) => {
        if (warteAuf) { clearTimeout(warteAuf.uhr); warteAuf = null; }
        ablehnen(f);
      });
    });
  }

  async function reihe(befehle) {
    for (const b of befehle) {
      const sauber = b.trim();
      if (!sauber) continue;
      try {
        await befehl(sauber, sauber === "ATZ" ? 10000 : 6000);
      } catch (fehler) {
        log("FEHLER " + fehler.message);
        return false;
      }
    }
    return true;
  }

  /* ---------- Ladestand ---------- */

  /* Die Antwort auf 22028C sieht bei einer positiven Rückmeldung so aus:
   * `62028Cxx` - 0x62 ist 0x22 + 0x40 (die Quittung des Dienstes), dann die
   * Datenkennung, dann die Nutzdaten. Was jolt daraus macht, ist geraten,
   * bis es einmal am Auto stimmt; deshalb steht die Rohantwort daneben. */
  function socAusAntwort(roh) {
    const hex = roh.replace(/[^0-9A-Fa-f]/g, "").toUpperCase();
    const marke = hex.indexOf("62028C");
    if (marke < 0) return null;
    const nutz = hex.slice(marke + 6);
    if (nutz.length < 2) return null;
    return parseInt(nutz.slice(0, 2), 16) / 2.5;
  }

  async function socLesen() {
    el("soc-wert").textContent = "…";
    try {
      await befehl("ATSH7E5");
      const antwort = await befehl("22028C");
      const wert = socAusAntwort(antwort);
      if (wert === null) {
        el("soc-wert").textContent = "?";
        log("Antwort enthält kein 62028C - siehe oben. Entweder ist die "
            + "Datenkennung eine andere, oder das Steuergerät antwortet "
            + "nicht auf 7E5.");
        return;
      }
      letzterSoc = Math.round(wert * 10) / 10;
      el("soc-wert").textContent = letzterSoc + " %";
      log(`Ladestand ${letzterSoc} % (Rohwert / 2,5)`);
    } catch (fehler) {
      el("soc-wert").textContent = "–";
      log("FEHLER " + fehler.message);
    }
  }

  /* ---------- An jolt melden ---------- */

  function ort() {
    return new Promise((erfuellen, ablehnen) => {
      if (!navigator.geolocation) { ablehnen(new Error("kein GPS")); return; }
      navigator.geolocation.getCurrentPosition(
        (p) => erfuellen({ lat: p.coords.latitude, lon: p.coords.longitude }),
        (f) => ablehnen(new Error("Standort: " + f.message)),
        { enableHighAccuracy: true, timeout: 10000 });
    });
  }

  async function melden() {
    const token = el("token").value.trim();
    if (!token) { log("Kein Logger-Token eingetragen."); return; }
    if (letzterSoc === null) { log("Erst den Ladestand abfragen."); return; }
    try {
      const wo = await ort();
      const antwort = await fetch("/api/live/melden", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, soc: letzterSoc,
                               lat: wo.lat, lon: wo.lon }),
      });
      const daten = await antwort.json();
      log(`jolt: HTTP ${antwort.status} ${JSON.stringify(daten).slice(0, 200)}`);
    } catch (fehler) {
      log("FEHLER " + fehler.message);
    }
  }

  /* ---------- Aufbau ---------- */

  if (!navigator.bluetooth) {
    el("untauglich").hidden = false;
    el("verbinden").disabled = true;
  }
  el("verbinden").addEventListener("click", verbinden);
  el("init").addEventListener("click", async () => {
    if (await reihe(HANDSHAKE)) log("Handshake durch.");
  });
  el("soc").addEventListener("click", socLesen);
  el("senden").addEventListener("click", () => reihe(el("frei").value.split("\n")));
  el("melden").addEventListener("click", melden);
  el("log-leeren").addEventListener("click", () => { el("log").textContent = ""; });
  log("Bereit. Dongle einstecken, Zündung an, dann „Dongle suchen“.");
})();
