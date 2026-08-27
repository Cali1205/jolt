/* Der OBD2-Dongle als Baustein - Verbindung, ELM327, Messwerte.
 *
 * Herausgelöst aus der Diagnoseseite, weil es zwei Nutzer gibt: jene Seite
 * zum Fehlersuchen, und die jolt-Oberfläche selbst. Zwei Kopien derselben
 * ELM-Befehlsfolge wären zwei Kopien, die auseinanderlaufen - und das an
 * einer Stelle, an der jeder Unterschied wieder ein NO DATA am Fahrzeug
 * bedeutet.
 *
 * Das Modul kennt weder Bedienelemente noch jolts API. Es verbindet, liest
 * und meldet über einen Rückruf, was es tut; was daraus wird, entscheidet
 * der Aufrufer. Deshalb dient es der Diagnoseseite und der Hauptoberfläche
 * gleichermassen.
 *
 * `verfuegbar()` ist die Frage, an der alles hängt: Web Bluetooth gibt es
 * auf iOS nicht von Apple, sondern nur in Bluefy. In Safari meldet sich das
 * Modul schlicht als nicht verfügbar, und der Aufrufer zeichnet dann ohne
 * Dongle auf - statt eine Fehlermeldung zu zeigen, die niemand beheben kann.
 */
window.joltObd = (function () {
  "use strict";

  let melde = () => {};      // Protokoll-Rückruf des Aufrufers
  let beiAbriss = null;      // gerufen, wenn die Verbindung stirbt


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

  let schreiben = null;      // Charakteristik zum Senden
  let geraetGemerkt = null;  // für das Wiederverbinden nach Abriss
  let puffer = "";
  let warteAuf = null;       // {erfuellen, ablehnen, uhr}
  let letzteAdresse = null;

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
          melde(`Versuch: ${name}`);
          geraet = await navigator.bluetooth.requestDevice(bauen());
          break;
        } catch (fehler) {
          letzterFehler = fehler;
          melde(`  ${fehler.name || "Fehler"}: ${fehler.message}`);
          // Abbruch durch den Nutzer ist kein Grund weiterzuprobieren - er
          // hat den Dialog gesehen und zugemacht. Jede weitere Variante
          // öffnete ihn nur erneut.
          if (fehler.name === "NotFoundError"
              && /cancel|abbruch|user/i.test(fehler.message)) throw fehler;
        }
      }
      if (!geraet) throw letzterFehler || new Error("Keine Variante ging.");
      geraetGemerkt = geraet;
      melde(`Gerät gewählt: ${geraet.name || "(ohne Namen)"}`);
      geraet.addEventListener("gattserverdisconnected", () => {
        melde("Verbindung getrennt.");
        // Im Tunnel oder wenn der Dongle einschläft reisst die Verbindung
        // ab. Während einer laufenden Aufzeichnung ist das kein Grund
        // aufzuhören - wer dann erst eine Berührung braucht, verliert die
        // halbe Fahrt, weil niemand am Steuer auf den Bildschirm sieht.
        if (beiAbriss) beiAbriss();
      });
      await verbindungAufbauen(geraet);
    } catch (fehler) {
      melde("FEHLER " + fehler.message);
    }
  }

  /* Den GATT-Aufbau getrennt von der Geräteauswahl.
   *
   * `requestDevice` verlangt zwingend eine Nutzergeste - eine Seite darf
   * sich beim Laden nicht von selbst verbinden. `gatt.connect()` auf ein
   * bereits erlaubtes Gerät dagegen nicht. Genau deshalb steht es hier für
   * sich: Nach einem Abriss im Tunnel lässt sich damit ohne Zutun wieder
   * aufbauen, solange das Gerät gemerkt ist. */
  async function verbindungAufbauen(geraet) {
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
        melde(`Dienst ${dienst.uuid}: ${chars.length} Charakteristiken`);
        if (w && n) { schreiben = w; notify = n; break; }
      }
      if (!schreiben || !notify) {
        throw new Error("Kein Dienst mit Schreiben und Benachrichtigen "
                        + "gefunden. Die UUID des Dongles steht oben im "
                        + "Protokoll - sie gehört in die Liste DIENSTE.");
      }

      await notify.startNotifications();
      notify.addEventListener("characteristicvaluechanged", beiDaten);
      melde(`Bereit. Schreiben auf ${schreiben.uuid}, Lesen auf ${notify.uuid}`);
  }


  /* ---------- Wiederverbinden ---------- */

  /* Nach einem Abriss ohne Zutun wieder aufbauen.
   *
   * Zwei Wege, und welcher geht, hängt am Browser: Ist das Gerät noch
   * gemerkt, genügt `gatt.connect()` - das braucht keine Geste. Ist es das
   * nicht (Seite neu geladen), fragt `getDevices()` nach den bereits
   * erlaubten Geräten; auch das ohne Geste, aber nicht jeder Browser kennt
   * es. Erst wenn beides scheitert, muss jemand tippen - und dann steht das
   * auch gross da statt nur im Protokoll.
   */
  async function wiederverbinden(versuch = 1, weiter = () => true) {
    if (!weiter()) return;
    const grenze = 6;
    try {
      let geraet = geraetGemerkt;
      if (!geraet && navigator.bluetooth.getDevices) {
        const bekannt = await navigator.bluetooth.getDevices();
        geraet = bekannt.find((g) => NAMEN.some((n) => (g.name || "").startsWith(n)))
                 || bekannt[0];
      }
      if (!geraet) throw new Error("kein gemerktes Gerät");
      melde(`Wiederverbinden, Versuch ${versuch} …`);
      await verbindungAufbauen(geraet);
      letzteAdresse = null;          // Adresse und Filter sind weg
      await reihe(HANDSHAKE);
      melde("Wieder verbunden, Handshake erneuert.");
    } catch (fehler) {
      melde(`Wiederverbinden fehlgeschlagen: ${fehler.message}`);
      if (versuch >= grenze) {
        return;
      }
      // Wachsende Abstände: Ein Tunnel dauert Sekunden, ein eingeschlafener
      // Dongle Minuten. Alle zwei Sekunden zu klopfen hilft in keinem der
      // beiden Fälle und kostet Akku.
      const warten = Math.min(60000, 3000 * Math.pow(2, versuch - 1));
      // `weiter` muss mitgereicht werden. Ohne das galt beim zweiten
      // Versuch wieder die Vorgabe `() => true`, und die Kette lief nach dem
      // Ende der Fahrt einfach weiter - sie verband einen Dongle neu, den
      // niemand mehr braucht, und hielt die Verbindung offen.
      setTimeout(() => wiederverbinden(versuch + 1, weiter), warten);
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
    melde(antwort || "(leer)", "rein");
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
      melde(text, "raus");
      puffer = "";
      warteAuf = {
        erfuellen,
        uhr: setTimeout(() => {
          warteAuf = null;
          // Ein Zeitablauf ist hier kein Absturz, sondern ein Befund: Der
          // Dongle hat nicht geantwortet, und das steht im Protokoll.
          melde(`(keine Antwort auf ${text} innerhalb ${grenze_ms / 1000} s)`);
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
        melde("FEHLER " + fehler.message + " - weiter mit dem nächsten Befehl");
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
  /* Eine Adresse besteht aus drei Teilen, und der dritte wird gern
   * vergessen: `cp` sind die oberen fünf Bit der 29-bit-Kennung, `sh` die
   * unteren 24. Beim Batteriemanagement (0x17FC007B) ist cp = 17; beim
   * Klimasteuergerät (0x00000746) ist es 00. Wer cp stehen lässt, sendet an
   * eine ganz andere Kennung und bekommt NO DATA - genau der Fehler, der
   * die Suche nach dem Ladestand aufgehalten hat. */
  const BMS = { cp: "17", sh: "FC007B", cra: "17FE007B" };
  const KLIMA = { cp: "00", sh: "000746", cra: "000007B0" };
  // Fahrzeug-Steuergerät: Kilometerstand und - der eigentliche Fund - die
  // Leistung der Nebenverbraucher als fertige Zahl.
  const FAHRZEUG = { cp: "17", sh: "FC0076", cra: "17FE0076" };
  // Der DC/DC-Wandler speist das 12-V-Netz aus der Hochvoltbatterie.
  const DCDC = { cp: "17", sh: "FC00B9", cra: "17FE00B9" };
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
    /* Der Strom der PTC-Heizung. Mal Packspannung ergibt das, was die
     * Heizung allein zieht - im Winter die Frage hinter der Frage, weil sie
     * der einzige grosse Verbraucher ist, den man selbst beeinflusst. */
    { name: "ptc_strom_a", did: "221620", adresse: BMS,
      lesen: (b) => b.length ? b[0] / 4 : null },
    { name: "tempo_kmh", did: "22F40D", adresse: BMS, lesen: (b) => b[0] },
    /* Die Aussentemperatur ist der grösste Einzelposten der Kälte und ging
     * bisher aus einer Vorhersage ins Verbrauchsmodell. Aus dem Auto ist sie
     * gemessen, von der Strecke, zur richtigen Zeit. Sie sitzt in einem
     * anderen Steuergerät als die Batterie - siehe KLIMA. */
    { name: "aussentemp_c", did: "222609", adresse: KLIMA,
      lesen: (b) => b.length ? b[0] / 2 - 50 : null },
    { name: "innentemp_c", did: "222613", adresse: KLIMA,
      lesen: (b) => b.length >= 2 ? ((b[0] * 256 + b[1]) / 5) - 40 : null },
    /* **Nebenverbraucher als fertige Zahl.** Alles ausser dem Antrieb -
     * Heizung, Klima, Steuergeräte, 12-V-Netz - in kW, direkt aus dem
     * Steuergerät.
     *
     * Vorher wurde das im Stand gemessen und dazwischen fortgeschrieben:
     * Steht das Auto, ist die Packleistung die der Nebenverbraucher. Das
     * war eine brauchbare Näherung, aber eben eine - sie galt nur so lange,
     * wie sich an der Heizung nichts änderte, und im Fahren gar nicht. Ein
     * gemessener Wert schlägt jede Näherung. */
    { name: "nebenverbrauch_kw", did: "220364", adresse: FAHRZEUG,
      lesen: (b) => b.length >= 2 ? (b[0] * 256 + b[1]) / 10 : null },
    { name: "dcdc_strom_a", did: "22465B", adresse: DCDC, selten: 10,
      lesen: (b) => b.length >= 2 ? (b[0] * 256 + b[1]) / 16 : null },
    { name: "km_stand", did: "22295A", adresse: FAHRZEUG,
      selten: 20,
      lesen: (b) => b.length >= 3 ? (b[0] * 65536) + (b[1] * 256) + b[2] : null },
  ];


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
      // Die Prioritätsbits gehören dazu: Zwischen Batterie (0x17…) und
      // Klima (0x00…) unterscheiden sie sich, und ohne Umschalten geht die
      // Anfrage an eine Kennung, auf der niemand hört.
      if (!letzteAdresse || letzteAdresse.cp !== ziel.cp) {
        await befehl(`ATCP${ziel.cp}`);
      }
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





  /* ---------- Ohne Auswahldialog verbinden ---------- */

  /* Der Dongle soll nicht jedes Mal ausgewählt werden müssen.
   *
   * Die Erlaubnis für ein Gerät bleibt im Browser bestehen, sobald sie
   * einmal erteilt wurde - `getDevices()` gibt die bekannten zurück, **ohne
   * Nutzergeste**, und `gatt.connect()` darauf braucht auch keine. Nur
   * `requestDevice` verlangt zwingend eine, und genau deshalb ist es der
   * zweite Weg und nicht der erste.
   *
   * Ob ein Browser `getDevices()` kennt, ist offen: Es ist neuer als der
   * Rest von Web Bluetooth. Fehlt es, kommt der Dialog wie bisher - dann
   * ist nichts verloren, es ist nur eine Berührung mehr.
   *
   * Zurückgegeben wird, welcher Weg gegangen wurde, damit der Aufrufer es
   * sagen kann statt es zu verschweigen.
   */
  async function verbindenOhneDialog() {
    if (!navigator.bluetooth || !navigator.bluetooth.getDevices) {
      melde("Dieser Browser kann bekannte Geräte nicht wiederfinden "
            + "(getDevices fehlt) - der Auswahldialog kommt.");
      return null;
    }
    let bekannt = [];
    try {
      bekannt = await navigator.bluetooth.getDevices();
    } catch (fehler) {
      melde("Bekannte Geräte nicht abrufbar: " + fehler.message);
      return null;
    }
    if (!bekannt.length) {
      melde("Noch kein Gerät erlaubt - beim ersten Mal muss ausgewählt werden.");
      return null;
    }
    // Den passenden nehmen, nicht irgendeinen: In der Liste stehen alle
    // Geräte, denen diese Seite je erlaubt wurde.
    const geraet = bekannt.find((g) => NAMEN.some(
      (n) => (g.name || "").startsWith(n))) || bekannt[0];
    melde(`Bekanntes Gerät: ${geraet.name || "(ohne Namen)"} - verbinde ohne Dialog.`);
    try {
      geraetGemerkt = geraet;
      geraet.addEventListener("gattserverdisconnected", () => {
        melde("Verbindung getrennt.");
        if (beiAbriss) beiAbriss();
      });
      await verbindungAufbauen(geraet);
      return geraet;
    } catch (fehler) {
      // Das Gerät ist bekannt, aber nicht da - ausgeschaltet, ausser
      // Reichweite, oder es steckt gerade nicht im Auto.
      melde("Bekanntes Gerät antwortet nicht: " + fehler.message);
      return null;
    }
  }

  /* Erst ohne Dialog, dann mit. Das ist der Weg, den Aufrufer nehmen
   * sollten - er kostet beim zweiten Mal keine Berührung mehr. */
  async function anschliessen() {
    if (await verbindenOhneDialog()) return true;
    await verbinden();
    return verbunden_();
  }

  function verbunden_() { return !!schreiben; }

  /* ---------- Nach aussen ---------- */

  return {
    verfuegbar: () => !!navigator.bluetooth,
    verbunden: () => !!schreiben,
    /* `melder` bekommt jede Zeile, die sonst im Protokoll stünde; `abriss`
     * wird gerufen, wenn die Verbindung stirbt - ob das ein Grund zum
     * Wiederverbinden ist, entscheidet der Aufrufer, nicht dieses Modul. */
    einrichten(melder, abriss) { melde = melder || melde; beiAbriss = abriss; },
    verbinden,
    anschliessen,
    wiederverbinden,
    handshake: () => reihe(HANDSHAKE),
    befehl,
    reihe,
    satzLesen,
    socAusRoh,
    socAusAntwort,
    NAMEN,
  };
})();
