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

  /* Der Handshake - **am Fahrzeug bestätigt** am 26.08.2026 an einem
   * ID.Buzz mit einem Vgate iCar Pro 2S (ELM327 v2.3).
   *
   * Der MEB spricht Diagnose über 29-bit-Kennungen, nicht über die kurzen
   * 11-bit-Adressen der Abgasdiagnose. Auf 7E0/7E2/7E5/7E6 antwortete
   * nichts, und zwar nicht weil die Steuergeräte schwiegen, sondern weil in
   * der falschen Adressform gefragt wurde.
   *
   * Die Grundlage stammt aus dem eigenen Android-Logger
   * (Cali1205/OBD2_Logger_Kotlin, core/Obd2.kt, `vwPre`). Eine Sache musste
   * dabei berichtigt werden, und sie war der Unterschied zwischen NO DATA
   * und einer Antwort:
   *
   *   Der Logger setzt `ATCP17` **und** gibt `ATSH17FC007B` die vollständige
   *   Adresse. Das schliesst einander aus. `ATCP` setzt die oberen fünf Bit
   *   der 29-bit-Kennung, `ATSH` liefert die unteren 24 - genau deshalb gibt
   *   es `ATCP` überhaupt. 0x17FC007B zerlegt sich in 0x17 oben und
   *   0xFC007B unten, und richtig heisst es deshalb `ATSHFC007B`. Der ELM
   *   quittiert die lange Form zwar mit OK, sendet dann aber auf einer
   *   anderen Kennung.
   *
   * Ausserdem `ATCAF1` statt `ATCAF0`: Mit abgeschalteter Formatierung
   * müsste das ISO-TP-Längenbyte von Hand im Befehl stehen (`0322028C`).
   * Automatisch ist weniger fehleranfällig und beherrscht mehrteilige
   * Antworten gleich mit.
   *
   * `ATH1` lässt die Absenderkennung in der Antwort stehen. Ein Byte mehr
   * zu lesen kostet nichts und beantwortet im Zweifel die Frage, *wer*
   * geantwortet hat - beim Suchen war das die nützlichste Zeile überhaupt.
   *
   * Bestätigte Antwort auf 22028C:  17FE007B 04 62028C B4 */
  const BMS_SENDEN = "FC007B";        // untere 24 Bit; obere 5 via ATCP17
  const BMS_EMPFANGEN = "17FE007B";   // Empfangsfilter: volle Kennung
  const HANDSHAKE = [
    "ATZ", "ATE0", "ATL0", "ATS0", "ATH1",
    "ATSP7", "ATCP17", "ATCAF1", "ATST FF",
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
    for (const id of ["init", "soc", "senden", "melden", "fahrt-start"]) el(id).disabled = !an;
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

  /* Vom Rohbyte zu den beiden Ladeständen.
   *
   * Die Antwort auf 22028C sieht so aus: `17FE007B 04 62028C B4` - die
   * Absenderkennung (wegen ATH1), das ISO-TP-Längenbyte, die Quittung
   * `62` = `22` + `40`, die Datenkennung, dann ein einziges Nutzbyte.
   *
   * Aus diesem Byte folgen **zwei** Zahlen, und die zu verwechseln ist der
   * gefährlichste Fehler an dieser Stelle:
   *
   *   SoC(BMS) = Rohwert / 2,5
   *   SoC(HMI) = SoC(BMS) * 51/46 - 6,4
   *
   * Der BMS-Wert ist der Brutto-Ladestand der Batterie. Die Anzeige im Auto
   * zeigt ihn nicht - sie rechnet ihn auf das nutzbare Fenster um, das oben
   * und unten einen Puffer freilässt (rechnerisch: 0 % Anzeige bei 5,8 %
   * brutto, 100 % Anzeige bei 96 % brutto).
   *
   * Am Fahrzeug bestätigt: Rohwert 0xB4 = 180 ergibt 72,0 % brutto und
   * 73,4 % Anzeige - das Auto zeigte 74 %. Mit dem Teiler 2,55, wie ihn der
   * eigene Android-Logger verwendet, käme 71,9 % heraus und die Rechnung
   * ginge nicht auf. Der Teiler ist 2,5.
   *
   * **jolt braucht den HMI-Wert.** `reserve_soc` und `ziel_soc` sind am
   * Anzeigewert gedacht, und der liegt hier gut anderthalb Punkte über dem
   * Brutto-Wert. Wer den falschen meldet, setzt die Reserve zu optimistisch
   * - und zwar genau am unteren Ende, wo es zählt. */
  /* Aus dem Rohbyte die beiden Ladestände. Getrennt von `socAusAntwort`,
   * weil die Aufzeichnung das Byte schon zerlegt vorliegen hat. */
  function socAusRoh(byte) {
    const bms = byte / 2.5;
    return { roh: byte, bms,
             hmi: Math.min(100, Math.max(0, bms * 51 / 46 - 6.4)) };
  }

  function socAusAntwort(roh) {
    const hex = roh.replace(/[^0-9A-Fa-f]/g, "").toUpperCase();
    const marke = hex.indexOf("62028C");
    if (marke < 0) return null;
    const nutz = hex.slice(marke + 6);
    if (nutz.length < 2) return null;
    return socAusRoh(parseInt(nutz.slice(0, 2), 16));
  }

  async function socLesen() {
    el("soc-wert").textContent = "…";
    try {
      // Adresse und Filter stehen seit dem Handshake; sie hier erneut zu
      // setzen würde ATCP17 und ATCAF1 nicht wiederholen und damit gerade
      // das zerstören, worauf es ankommt.
      const antwort = await befehl("22028C");
      const wert = socAusAntwort(antwort);
      if (wert === null) {
        el("soc-wert").textContent = "?";
        log("Antwort enthält kein 62028C - siehe oben. Entweder ist die "
            + "Datenkennung eine andere, oder das Steuergerät antwortet "
            + "nicht auf dieser Kennung.");
        return;
      }
      letzterSoc = Math.round(wert.hmi * 10) / 10;
      // Beide Zahlen anzeigen: Die grosse ist die, die im Auto steht und die
      // jolt bekommt; die kleine daneben macht nachvollziehbar, woraus sie
      // entstanden ist.
      el("soc-wert").textContent = letzterSoc + " %";
      el("soc-herkunft").textContent =
        `Rohwert 0x${wert.roh.toString(16).toUpperCase()} = ${wert.roh}`
        + ` → brutto ${wert.bms.toFixed(1)} % → Anzeige ${wert.hmi.toFixed(1)} %`;
      log(`Ladestand: brutto ${wert.bms.toFixed(1)} %, `
          + `Anzeige ${wert.hmi.toFixed(1)} % (Rohwert ${wert.roh})`);
    } catch (fehler) {
      el("soc-wert").textContent = "–";
      log("FEHLER " + fehler.message);
    }
  }


  /* ---------- Was ausgelesen wird ---------- */

  /* Die Datenkennungen stammen aus der MEB-Liste von spot2000 und aus dem
   * eigenen Android-Logger; bestätigt am Fahrzeug ist bisher nur `028C`.
   * Die übrigen stehen mit `pflicht: false` drin - schlägt eine fehl, läuft
   * die Aufzeichnung weiter, statt an einer Nebensache zu scheitern.
   *
   * `adresse` schaltet die Zieladresse um: Der Kilometerstand sitzt in einem
   * anderen Steuergerät als die Batterie. Umgeschaltet wird nur, wenn nötig -
   * jedes ATSH kostet einen Umlauf über die serielle Strecke.
   *
   * Warum überhaupt mehr als der Ladestand: Die Aussentemperatur geht direkt
   * ins Verbrauchsmodell (bisher kommt sie von Open-Meteo, also aus einer
   * Vorhersage statt aus dem Auto). Spannung mal Strom ergibt die
   * tatsächliche Leistung, an der sich die Prognose nachprüfen lässt. Und
   * der Rohwert des Ladestands ist der einzige Weg, die Umrechnung auf den
   * Anzeigewert später zu berichtigen, ohne ein zweites Mal loszufahren. */
  const BMS = { sh: "FC007B", cra: "17FE007B" };
  const MESSWERTE = [
    { name: "soc_roh", did: "22028C", adresse: BMS, pflicht: true,
      lesen: (b) => b[0] },
    { name: "spannung_v", did: "221E3B", adresse: BMS,
      lesen: (b) => b.length >= 2 ? (b[0] * 256 + b[1]) / 4 : null },
    { name: "strom_a", did: "221E3D", adresse: BMS,
      lesen: (b) => b.length >= 4
        ? ((b[0] * 16777216) + (b[1] * 65536) + (b[2] * 256) + b[3] - 150000) / 100
        : null },
    { name: "energie_kwh", did: "221E32", adresse: BMS,
      lesen: (b) => b.length >= 4
        ? ((b[0] * 16777216) + (b[1] * 65536) + (b[2] * 256) + b[3]) / 8583.07
        : null },
    { name: "ladegrenze_a", did: "221E1B", adresse: BMS,
      lesen: (b) => b.length >= 2 ? (b[0] * 256 + b[1]) / 5 : null },
    { name: "betriebsart", did: "227448", adresse: BMS, lesen: (b) => b[0] },
    { name: "tempo_kmh", did: "22F40D", adresse: BMS, lesen: (b) => b[0] },
    { name: "km_stand", did: "22295A", adresse: { sh: "FC0076", cra: "17FE0076" },
      selten: 20,
      lesen: (b) => b.length >= 3 ? (b[0] * 65536) + (b[1] * 256) + b[2] : null },
  ];

  let letzteAdresse = null;

  /* Antwort in Nutzbytes zerlegen. Die Quittung ist `62` + die zwei Bytes
   * der Datenkennung; alles davor ist Absenderkennung und ISO-TP-Kopf,
   * alles danach ist Nutzlast. */
  function nutzbytes(roh, did) {
    const hex = roh.replace(/[^0-9A-Fa-f]/g, "").toUpperCase();
    const quittung = "62" + did.slice(2).toUpperCase();
    const marke = hex.indexOf(quittung);
    if (marke < 0) return null;
    const rest = hex.slice(marke + quittung.length);
    const bytes = [];
    for (let i = 0; i + 1 < rest.length; i += 2) {
      bytes.push(parseInt(rest.slice(i, i + 2), 16));
    }
    return bytes;
  }

  async function messwertLesen(eintrag) {
    const ziel = eintrag.adresse;
    if (!letzteAdresse || letzteAdresse.sh !== ziel.sh) {
      await befehl(`ATSH${ziel.sh}`);
      await befehl(`ATCRA${ziel.cra}`);
      letzteAdresse = ziel;
    }
    const antwort = await befehl(eintrag.did, 8000);
    const bytes = nutzbytes(antwort, eintrag.did);
    if (!bytes || !bytes.length) return null;
    const wert = eintrag.lesen(bytes);
    return (wert === null || wert === undefined || Number.isNaN(wert))
      ? null : wert;
  }

  /* Einen vollständigen Satz lesen. Fehler einzelner Grössen werden
   * vermerkt und übergangen - eine Aufzeichnung, die wegen des
   * Kilometerstands abbricht, hätte den Ladestand mit verloren. */
  async function satzLesen(runde) {
    const roh = {};
    for (const eintrag of MESSWERTE) {
      if (eintrag.selten && runde % eintrag.selten !== 0) continue;
      try {
        const wert = await messwertLesen(eintrag);
        if (wert !== null) roh[eintrag.name] = Math.round(wert * 1000) / 1000;
        else if (eintrag.pflicht) throw new Error("keine Nutzdaten");
      } catch (fehler) {
        if (eintrag.pflicht) throw fehler;
        if (!roh._fehlend) roh._fehlend = [];
        roh._fehlend.push(eintrag.name);
      }
    }
    return roh;
  }


  /* ---------- Aufzeichnung ---------- */

  let laeuft = false;
  let runde = 0;
  let wachhalter = null;   // WakeLockSentinel

  /* Den Bildschirm wach halten. Ohne das schaltet iOS ihn nach einer Minute
   * aus, und mit dem Bildschirm schläft der Seiteninhalt - die Verbindung
   * übersteht zwar den Sperrbildschirm, die Schleife aber nicht.
   *
   * Die Sperre geht verloren, wenn die Seite in den Hintergrund gerät, und
   * kommt nicht von selbst zurück; deshalb wird sie beim Zurückkommen neu
   * geholt. Kennt der Browser die Schnittstelle nicht, läuft die
   * Aufzeichnung trotzdem - dann muss man den Bildschirm eben in den
   * Einstellungen an lassen. */
  async function bildschirmWachHalten() {
    if (!("wakeLock" in navigator)) {
      log("Dieser Browser kennt keine Bildschirmsperre-Verhinderung. "
          + "Automatische Sperre bitte in den iOS-Einstellungen auf 'Nie'.");
      return;
    }
    try {
      wachhalter = await navigator.wakeLock.request("screen");
      log("Bildschirm wird wachgehalten.");
    } catch (fehler) {
      log("Bildschirm wachhalten ging nicht: " + fehler.message);
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (laeuft && document.visibilityState === "visible" && !wachhalter) {
      bildschirmWachHalten();
    }
  });

  function kacheln(werte) {
    el("fahrt-werte").innerHTML = werte.map(([name, zahl]) =>
      `<div class="wert"><div class="zahl">${zahl}</div>`
      + `<div class="name">${name}</div></div>`).join("");
  }

  async function eineRunde() {
    const roh = await satzLesen(runde);
    runde += 1;

    const soc = socAusRoh(roh.soc_roh);
    const wo = await ort().catch((f) => {
      log("Standort: " + f.message);
      return null;
    });
    if (!wo) return null;

    const nutzlast = {
      token: el("token").value.trim(),
      lat: wo.lat, lon: wo.lon,
      soc: Math.round(soc.hmi * 10) / 10,
      rohwerte: roh,
    };
    // Was das Auto selbst misst, schlägt jede Vorhersage: Die
    // Aussentemperatur ging bisher aus Open-Meteo ins Verbrauchsmodell.
    if (typeof roh.tempo_kmh === "number") nutzlast.tempo_kmh = roh.tempo_kmh;
    if (typeof roh.aussentemp_c === "number") {
      nutzlast.aussentemp_c = roh.aussentemp_c;
    }

    const antwort = await fetch("/api/live/melden", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(nutzlast),
    });
    const daten = await antwort.json().catch(() => ({}));
    return { soc, roh, daten, status: antwort.status };
  }

  async function fahrtSchleife() {
    while (laeuft) {
      const beginn = Date.now();
      try {
        const ergebnis = await eineRunde();
        if (ergebnis) {
          const { soc, roh, daten } = ergebnis;
          letzterSoc = Math.round(soc.hmi * 10) / 10;
          el("soc-wert").textContent = letzterSoc + " %";
          const leistung = (typeof roh.spannung_v === "number"
                            && typeof roh.strom_a === "number")
            ? (roh.spannung_v * roh.strom_a / 1000).toFixed(1) + " kW" : "–";
          kacheln([
            ["Ladestand", letzterSoc + " %"],
            ["brutto", soc.bms.toFixed(1) + " %"],
            ["Leistung", leistung],
            ["Spannung", (roh.spannung_v ?? "–") + " V"],
            ["aufgenommen", daten.aufgenommen ? "ja" : "nein"],
            ["Runde", String(runde)],
          ]);
          stand2(daten.aufgenommen
            ? `läuft – zuletzt ${new Date().toLocaleTimeString("de-DE")}`
            : `läuft – jolt: ${daten.grund || "nicht aufgenommen"}`,
            daten.aufgenommen ? "gut" : "");
          log(`Runde ${runde}: ${letzterSoc} % (roh ${roh.soc_roh})`
              + `${roh._fehlend ? ", ohne " + roh._fehlend.join("/") : ""}`
              + ` → jolt ${daten.aufgenommen ? "ok" : (daten.grund || "?")}`);
        }
      } catch (fehler) {
        // Ein Aussetzer beendet die Fahrt nicht. Tunnel, Funkloch, ein
        // Steuergerät das gerade nicht mag - das nächste Mal klappt es
        // wieder, und eine abgebrochene Aufzeichnung merkt man erst hinterher.
        stand2("Aussetzer: " + fehler.message, "schlecht");
        log("Runde übersprungen: " + fehler.message);
        letzteAdresse = null;      // Adresse neu setzen, sicher ist sicher
      }
      const rest = Number(el("takt").value) * 1000 - (Date.now() - beginn);
      await new Promise((w) => setTimeout(w, Math.max(1000, rest)));
    }
  }

  function stand2(text, art) {
    const k = el("fahrt-stand");
    k.textContent = text;
    k.className = "stand " + (art || "");
  }

  async function fahrtStarten() {
    if (!el("token").value.trim()) {
      stand2("Erst das Logger-Token eintragen.", "schlecht");
      return;
    }
    laeuft = true;
    runde = 0;
    el("fahrt-start").hidden = true;
    el("fahrt-stop").hidden = false;
    await bildschirmWachHalten();
    log("Aufzeichnung gestartet.");
    fahrtSchleife();
  }

  async function fahrtBeenden() {
    laeuft = false;
    el("fahrt-start").hidden = false;
    el("fahrt-stop").hidden = true;
    stand2("beendet");
    if (wachhalter) {
      try { await wachhalter.release(); } catch (e) {}
      wachhalter = null;
    }
    log("Aufzeichnung beendet.");
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
  el("fahrt-start").addEventListener("click", fahrtStarten);
  el("fahrt-stop").addEventListener("click", fahrtBeenden);
  el("takt").addEventListener("input", (e) => {
    el("takt-wert").textContent = e.target.value;
  });
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
