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
   * Deshalb die Liste der gebräuchlichen; der Vgate iCar Pro nutzt nach
   * verbreiteter Auskunft 0xFFF0, die anderen kosten nichts.
   *
   * Ausgeschrieben als 128-bit-UUID und nicht als Kurzform `0xfff0`: Die
   * Spezifikation erlaubt beides, aber Bluefy reicht die Optionen an eine
   * native Schicht weiter, und die stolperte über die Zahl - `RequestDevice:
   * Request payload could not be parsed`, noch bevor ein Geräte-Dialog
   * erschien. An der ausgeschriebenen Form gibt es nichts zu deuten, und
   * Chrome nimmt sie ebenso. */
  const kurz = (id) => `0000${id}-0000-1000-8000-00805f9b34fb`;
  const DIENSTE = [
    kurz("fff0"),   // Vgate, Veepeak, viele Klone
    kurz("ffe0"),   // HM-10-basiert
    kurz("ffe5"),
    kurz("fee7"),
    kurz("18f0"),
    "6e400001-b5a3-f393-e0a9-e50e24dcca9e",   // Nordic UART
  ];

  /* Namen, unter denen sich ELM327-Dongles melden. Der Vgate iCar Pro 2S
   * heisst `IOS-Vlink` - abgelesen am Gerät, nicht geraten.
   *
   * `namePrefix` vergleicht **unterscheidend nach Gross- und
   * Kleinschreibung**: `IOS-vlink` mit kleinem v trifft `IOS-Vlink` nicht,
   * und der Dialog bliebe leer, als wäre kein Dongle da. Deshalb steht das
   * kurze, eindeutige `IOS-` mit in der Liste - es trifft unabhängig davon,
   * wie der Rest geschrieben ist. */
  const NAMEN = ["IOS-Vlink", "IOS-", "Vlink", "vlink", "VLink",
                 "OBD", "Vgate", "VEEPEAK"];

  /* Mehrere Anläufe, weil sich die Browser hier verschieden verhalten und
   * ein einzelner Fehlschlag nicht sagt, woran es lag. Der letzte Anlauf
   * kann zwar keinen Dienst lesen, beantwortet aber die Frage, ob überhaupt
   * ein Auswahldialog erscheint - und trennt damit "der Aufruf ist kaputt"
   * von "der Dongle wird nicht gefunden". */

  const VARIANTEN = [
    ["alle Geräte, Dienste angemeldet",
     () => ({ acceptAllDevices: true, optionalServices: DIENSTE })],
    ["nach Namen gefiltert",
     () => ({ filters: NAMEN.map((n) => ({ namePrefix: n })),
              optionalServices: DIENSTE })],
    ["nach bekannten Diensten gefiltert",
     () => ({ filters: DIENSTE.map((d) => ({ services: [d] })),
              optionalServices: DIENSTE })],
    ["alle Geräte, ohne Dienstliste",
     () => ({ acceptAllDevices: true })],
  ];

  /* Der Handshake - übernommen aus dem eigenen Android-Logger
   * (Cali1205/OBD2_Logger_Kotlin, core/Obd2.kt, `vwPre`), nicht geraten.
   *
   * Der erste Versuch stand auf `ATSP6` (CAN 11 bit) mit Adresse 7E5 und
   * bekam von jeder Adresse `NO DATA` - der Bus war still, weil in der
   * falschen Adressform gefragt wurde. Der MEB spricht Diagnose über
   * **29-bit-Kennungen**:
   *
   *   ATSP7        CAN mit erweiterten 29-bit-Kennungen
   *   ATCP17       die oberen Prioritätsbits der Kennung sind 0x17
   *   ATCAF0       keine automatische Formatierung
   *   ATSH17FC007B senden an das Batteriemanagement
   *   ATCRA17FE007B  und nur dessen Antwort durchlassen
   *
   * ATSP0 davor lässt den ELM einmal selbst suchen; ATSP7 überschreibt das
   * gleich darauf. So steht es im Logger, und da es dort funktioniert, wird
   * hier nichts daran verbessert. */
  const BMS_SENDEN = "17FC007B";
  const BMS_EMPFANGEN = "17FE007B";
  const HANDSHAKE = [
    "ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATSP0",
    "ATSP7", "ATCP17", "ATCAF0",
    `ATSH${BMS_SENDEN}`, `ATCRA${BMS_EMPFANGEN}`,
  ];

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
      // Der Reihe nach durchprobieren, statt auf eine Form zu setzen: Welche
      // Gestalt der Anfrage ein Browser akzeptiert, unterscheidet sich - und
      // ein einzelner Fehlschlag sagt nicht, woran es lag. Jeder Versuch
      // steht im Protokoll, damit der nächste nicht wieder raten muss.
      let geraet = null;
      let letzterFehler = null;
      for (const [name, bauen] of VARIANTEN) {
        try {
          log(`Versuch: ${name}`);
          geraet = await navigator.bluetooth.requestDevice(bauen());
          break;
        } catch (fehler) {
          letzterFehler = fehler;
          log(`  ${fehler.name || "Fehler"}: ${fehler.message}`);
          // Abbruch durch den Nutzer ist kein Grund weiterzuprobieren - er
          // hat den Dialog gesehen und zugemacht. Jede weitere Variante
          // öffnete ihn nur erneut.
          if (fehler.name === "NotFoundError"
              && /cancel|abbruch|user/i.test(fehler.message)) throw fehler;
        }
      }
      if (!geraet) throw letzterFehler || new Error("Keine Variante ging.");
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

  /* Sechs Sekunden waren zu knapp: Nach `ATSP0` sucht der ELM das Protokoll
 * selbst (`SEARCHING...`), und das dauert an einem Fahrzeug, das nicht
 * antwortet, bis zu zehn Sekunden. Die Antwort kam eine Sekunde nach dem
 * Abbruch - im Protokoll stand dann ein Zeitablauf, wo in Wirklichkeit ein
 * Befund war. */
function befehl(text, grenze_ms = 15000) {
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

  /* Weitermachen statt abbrechen. Ein Befehl ohne Antwort ist hier ein
   * Befund und kein Grund aufzuhören - beim ersten Versuch riss ein
   * Zeitablauf bei `0100` die Reihe ab, und ausgerechnet das darauf folgende
   * `ATDP` lief nie. Genau der Befehl hätte gesagt, ob überhaupt ein
   * Protokoll gefunden wurde. */
  async function reihe(befehle) {
    let alles_gut = true;
    for (const b of befehle) {
      const sauber = b.trim();
      if (!sauber) continue;
      try {
        await befehl(sauber);
      } catch (fehler) {
        log("FEHLER " + fehler.message + " - weiter mit dem nächsten Befehl");
        alles_gut = false;
        // Eine verspätete Antwort auf den abgelaufenen Befehl darf nicht dem
        // nächsten zugeschlagen werden.
        puffer = "";
        await new Promise((w) => setTimeout(w, 300));
      }
    }
    return alles_gut;
  }

  /* ---------- Ladestand ---------- */

  /* Die Antwort auf 22028C sieht bei einer positiven Rückmeldung so aus:
   * `62028Cxx` - 0x62 ist 0x22 + 0x40 (die Quittung des Dienstes), dann die
   * Datenkennung, dann die Nutzdaten.
   *
   * Geteilt wird durch **2,55** und nicht durch 2,5: Ein Byte deckt 0 bis
   * 255 ab, und 255/2,55 sind glatte 100 %. Der Wert stammt aus
   * `hybridBatteryRemainingLife()` im eigenen Android-Logger, wo dieselbe
   * Datenkennung so ausgewertet wird. Der Unterschied sind bei vollem Akku
   * zwei Prozentpunkte - genug, um eine Reserve falsch zu setzen.
   *
   * Wegen ATCAF0 stehen vor der Quittung noch Rahmenbytes; deshalb wird
   * `62028C` gesucht statt am Anfang erwartet. */
  function socAusAntwort(roh) {
    const hex = roh.replace(/[^0-9A-Fa-f]/g, "").toUpperCase();
    const marke = hex.indexOf("62028C");
    if (marke < 0) return null;
    const nutz = hex.slice(marke + 6);
    if (nutz.length < 2) return null;
    return parseInt(nutz.slice(0, 2), 16) / 2.55;
  }

  async function socLesen() {
    el("soc-wert").textContent = "…";
    try {
      // Adresse und Filter stehen seit dem Handshake; sie hier erneut zu
      // setzen würde ATCP17 und ATCAF0 nicht wiederholen und damit gerade
      // das zerstören, worauf es ankommt.
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
  /* Auf dem Telefon ist das Markieren in einem Kasten mit Bildlauf fummelig,
   * und ein Bildschirmfoto verliert genau das, worauf es ankommt: die
   * Hex-Antworten Zeichen für Zeichen. */
  el("log-kopieren").addEventListener("click", async () => {
    const text = el("log").textContent;
    try {
      await navigator.clipboard.writeText(text);
      el("log-kopieren").textContent = "kopiert";
      setTimeout(() => { el("log-kopieren").textContent = "Protokoll kopieren"; }, 2000);
    } catch (fehler) {
      // Ohne Zwischenablage (älterer Browser, fehlende Erlaubnis) bleibt das
      // Markieren von Hand - dann wenigstens alles auf einmal auswählen.
      const bereich = document.createRange();
      bereich.selectNodeContents(el("log"));
      const auswahl = window.getSelection();
      auswahl.removeAllRanges();
      auswahl.addRange(bereich);
      log("Zwischenablage nicht verfügbar - Protokoll ist markiert, bitte "
          + "von Hand kopieren.");
    }
  });
  log("Bereit. Dongle einstecken, Zündung an, dann „Dongle suchen“.");
})();
