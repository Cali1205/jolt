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
  let notifyAktuell = null;  // aktuell abonnierte Charakteristik

  /* Ohne diese Sperre konnten zwei Verbindungsversuche gleichzeitig laufen -
   * etwa das automatische Wiederverbinden im Hintergrund und ein manuelles
   * Antippen von "Dongle verbinden" zur selben Zeit. Beide bauen dieselbe
   * GATT-Verbindung neu auf und schicken danach dieselbe Handshake-Reihe;
   * `befehl()` lässt aber nur einen wartenden Befehl gleichzeitig zu und
   * lehnt den zweiten mit "es läuft noch ein Befehl" ab. Die Reihe, die das
   * trifft, gilt dann als unvollständig - obwohl beide Versuche für sich
   * genommen funktioniert hätten. Alles, was verbindet oder den Handshake
   * schickt, läuft deshalb nacheinander über `gesperrt()`. */
  let verbindungSperre = Promise.resolve();

  function gesperrt(aufgabe) {
    const eigene = verbindungSperre.catch(() => {}).then(aufgabe);
    verbindungSperre = eigene.catch(() => {});
    return eigene;
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
      await gesperrt(() => verbindungAufbauen(geraet));
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

      // Ohne das Abmelden hier bekäme ein zweiter Aufbau auf dasselbe Gerät
      // (z.B. weil ein automatischer und ein manueller Versuch ineinander
      // liefen) einen zweiten Listener dazu - jede Antwort käme doppelt bei
      // `beiDaten` an und würde den Puffer durcheinanderbringen.
      if (notifyAktuell) {
        try {
          notifyAktuell.removeEventListener("characteristicvaluechanged", beiDaten);
        } catch (fehler) { /* Charakteristik schon weg - nichts zu tun */ }
      }
      await notify.startNotifications();
      notify.addEventListener("characteristicvaluechanged", beiDaten);
      notifyAktuell = notify;
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
  /* **Solange die Fahrt läuft, wird weiter versucht.**
   *
   * Hier stand `grenze = 6`. Mit den wachsenden Abständen (3, 6, 12, 24, 48,
   * 60 Sekunden) war die Serie nach rund zweieinhalb Minuten aufgebraucht,
   * und danach versuchte es jolt **nie wieder**.
   *
   * Gemessen an einer echten Fahrt: Fünf Minuten mit der Seite im
   * Hintergrund haben alle sechs Versuche verbraucht. Die restlichen
   * vierzehn Minuten kamen nur noch GPS-Punkte an - zwanzig Kilometer
   * aufgezeichnet, ohne einen einzigen Fahrzeugwert, und ohne dass jolt es
   * noch einmal probiert hätte.
   *
   * Eine Obergrenze war für den Fall gedacht, dass der Dongle gezogen wurde.
   * Genau dafür ist aber `weiter` da - es endet, wenn die Fahrt endet. Statt
   * aufzugeben wird der Abstand nur gedeckelt: alle sechzig Sekunden
   * anklopfen kostet fast nichts und holt eine Verbindung zurück, sobald sie
   * wieder möglich ist. */
  const WIEDER_HOECHSTABSTAND_MS = 60000;

  async function wiederverbinden(versuch = 1, weiter = () => true) {
    if (!weiter()) return;
    try {
      let geraet = geraetGemerkt;
      if (!geraet && navigator.bluetooth.getDevices) {
        const bekannt = await navigator.bluetooth.getDevices();
        geraet = bekannt.find((g) => NAMEN.some((n) => (g.name || "").startsWith(n)))
                 || bekannt[0];
      }
      if (!geraet) throw new Error("kein gemerktes Gerät");
      melde(`Wiederverbinden, Versuch ${versuch} …`);
      await gesperrt(async () => {
        await verbindungAufbauen(geraet);
        letzteAdresse = null;        // Adresse und Filter sind weg
        // Was vor dem Abriss unterwegs war, kommt nicht mehr.
        schuldigeAntworten = 0;
        puffer = "";
        wechselGescheitert.clear();
        await reihe(HANDSHAKE);
      });
      melde("Wieder verbunden, Handshake erneuert.");
    } catch (fehler) {
      // Nur jeden zehnten Fehlversuch protokollieren, sonst füllt sich das
      // Protokoll auf einer langen Fahrt mit derselben Zeile.
      if (versuch <= 6 || versuch % 10 === 0) {
        melde(`Wiederverbinden fehlgeschlagen (Versuch ${versuch}): `
              + fehler.message);
      }
      // Wachsende Abstände bis zur Obergrenze: Ein Tunnel dauert Sekunden,
      // ein eingeschlafener Dongle Minuten. Alle zwei Sekunden zu klopfen
      // hilft in keinem der beiden Fälle und kostet Akku.
      const warten = Math.min(WIEDER_HOECHSTABSTAND_MS,
                              3000 * Math.pow(2, versuch - 1));
      // `weiter` muss mitgereicht werden. Ohne das galt beim zweiten
      // Versuch wieder die Vorgabe `() => true`, und die Kette lief nach dem
      // Ende der Fahrt einfach weiter - sie verband einen Dongle neu, den
      // niemand mehr braucht, und hielt die Verbindung offen.
      setTimeout(() => wiederverbinden(versuch + 1, weiter), warten);
    }
  }

  /* Die Verbindung absichtlich beenden.
   *
   * Gebraucht an der Ladesaeule: Ein verriegeltes Fahrzeug, das weiter ueber
   * CAN gefragt wird, loest die Alarmanlage aus. "Nicht mehr lesen" genuegt
   * dafuer nicht - der Dongle bleibt verbunden, und schon der Handshake nach
   * einem Abriss spricht wieder mit dem Bus.
   *
   * `geraetGemerkt` bleibt stehen: Das Geraet ist weiter erlaubt, und der
   * naechste Aufbau kommt ohne Auswahldialog aus. */
  function trennen() {
    try {
      if (geraetGemerkt && geraetGemerkt.gatt && geraetGemerkt.gatt.connected) {
        geraetGemerkt.gatt.disconnect();
      }
    } catch (fehler) {
      melde("Trennen: " + fehler.message);
    }
    schreiben = null;
    letzteAdresse = null;
    warteAuf = null;
    puffer = "";
    schuldigeAntworten = 0;
    melde("Verbindung absichtlich getrennt.");
  }

  /* ---------- Befehle ---------- */

  /* Wie viele Antworten noch von aufgegebenen Befehlen unterwegs sind.
   *
   * Ein Zeitablauf gibt den Befehl auf, aber nicht der Dongle: Der antwortet
   * gleich darauf trotzdem. Ohne Buchführung landete diese verspätete
   * Antwort beim **nächsten** Befehl. Falsche Zahlen kamen dabei nicht
   * heraus - `nutzbytes` prüft, dass die Quittung zur Datenkennung passt -,
   * aber jede Messung danach war um eins verschoben und lieferte "keine
   * Nutzdaten". Und weil der Ladestand pflicht ist, riss das gleich die
   * ganze Runde ab: ein einzelner langsamer Befehl kostete mehrere
   * Messpunkte statt einen Wert. */
  let schuldigeAntworten = 0;

  function beiDaten(e) {
    puffer += new TextDecoder().decode(e.target.value);
    // Der ELM327 schliesst jede Antwort mit '>' ab. Vorher ist sie
    // unvollständig - BLE liefert in Häppchen von rund zwanzig Byte.
    if (!puffer.includes(">")) return;
    const antwort = puffer.replace(/>/g, "").replace(/\r/g, "\n").trim();
    puffer = "";
    if (schuldigeAntworten > 0) {
      schuldigeAntworten -= 1;
      melde(`(verspätete Antwort verworfen: ${antwort || "leer"})`);
      return;
    }
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
          // Der Dongle antwortet vielleicht doch noch. Diese eine Antwort
          // gehört zu keinem wartenden Befehl mehr und wird verworfen.
          schuldigeAntworten += 1;
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
  const BMS = { cp: "17", sh: "FC007B", cra: "17FE007B", fcsh: "17FC007B" };
  /* Das Klimasteuergeraet haengt an einer **11-Bit-Kennung**, nicht an einer
   * 29-Bit wie alles andere.
   *
   * 0x746 (Anfrage) und 0x7B0 (Antwort) passen in elf Bit - das ist VWs
   * klassisches Diagnoseschema. 0x17FC007B, wo Batterie und Fahrzeug
   * antworten, passt nur in 29. Der Handshake stellt `ATSP7` ein, also
   * ausschliesslich 29 Bit; eine Anfrage an 0x00000746 geht damit als
   * 29-Bit-Rahmen hinaus, und darauf hoert das Klimageraet nicht.
   *
   * Das erklaert den Befund aus der dritten Testfahrt: Aussen- und
   * Innentemperatur fehlten in **allen** 77 Runden, waehrend alles auf
   * 0x17FC.... zu 100 % ankam. Adresse und Umrechnung stimmen mit der
   * MEB-Referenz von spot2000 ueberein - es ist die Rahmenbreite.
   *
   * `protokoll` schaltet deshalb fuer diese Abfragen kurz auf ATSP6 um.
   * Ungeprueft am Fahrzeug: Es folgt aus den Adressen, nicht aus einer
   * Messung. Deshalb liegt es hinter `selten` und faellt nach einem
   * Fehlschlag fuer die Sitzung aus (siehe `messwertLesen`). */
  const KLIMA = { cp: "00", sh: "746", cra: "7B0", fcsh: "746", protokoll: "6" };

  /* Das Batteriemanagement auf der 11-Bit-Seite. Dieselbe Umschaltung wie
   * beim Klimageraet - es antwortet auf 0x77A, nicht auf 0x17FE007B. */
  const AKKU11 = { cp: "00", sh: "710", cra: "77A", fcsh: "710", protokoll: "6" };
  // Fahrzeug-Steuergerät: Kilometerstand und - der eigentliche Fund - die
  // Leistung der Nebenverbraucher als fertige Zahl.
  const FAHRZEUG = { cp: "17", sh: "FC0076", cra: "17FE0076", fcsh: "17FC0076" };
  // Der DC/DC-Wandler speist das 12-V-Netz aus der Hochvoltbatterie.
  const DCDC = { cp: "17", sh: "FC00B9", cra: "17FE00B9", fcsh: "17FC00B9" };
  const ZUSATZ_TITEL = {
    geladen_kwh: "Geladen gesamt",
    kompressor_upm: "Kompressor-Drehzahl",
    kompressor_an: "Kompressor an",
  };

  const ZUSATZ_EINHEIT = { kompressor_upm: "/min", kompressor_an: null };
  const ZUSATZ_STELLEN = { kompressor_upm: 0, kompressor_an: 0 };

  const MESSWERTE = [
    { name: "soc_roh", titel: "Rohwert SoC", einheit: null, stellen: 0,
      did: "22028C", adresse: BMS, pflicht: true,
      lesen: (b) => b[0] },
    { name: "spannung_v", titel: "Spannung", einheit: "V", stellen: 1,
      did: "221E3B", adresse: BMS,
      lesen: (b) => b.length >= 2 ? (b[0] * 256 + b[1]) / 4 : null },
    /* Batteriestrom.
     *
     * Ich hatte das auf die WiCAN-Formel umgestellt - fuenf Bytes ab B5 und
     * umgekehrtes Vorzeichen. Das war falsch. Zwei unabhaengige Quellen
     * nennen uebereinstimmend vier Bytes ab dem ersten Datenbyte und
     * `(Rohwert - 150000)/100`: die MEB-Liste von spot2000 und der
     * ESP32-Logger von codingABI, dessen Pufferindex nachweislich beim
     * ersten Byte nach der Quittung beginnt - also genau bei unserem b[0].
     * Damit steht hier wieder, was urspruenglich dastand.
     *
     * Warum der Wert trotzdem nicht ankam, ist damit **nicht** geklaert -
     * die Formel war es jedenfalls nicht. */
    { name: "strom_a", titel: "Strom", einheit: "A", stellen: 1,
      did: "221E3D", adresse: BMS,
      lesen: (b) => b.length >= 4
        ? ((b[0] * 16777216) + (b[1] * 65536) + (b[2] * 256) + b[3] - 150000) / 100
        : null },
    /* Die Energiezaehler des Fahrzeugs - der genaueste Verbrauchsmesser,
     * den es hier gibt.
     *
     * Sie zaehlen ueber die Lebensdauer, was in den Akku hinein- und was
     * herausgegangen ist. Fuer den Verbrauch zaehlt nicht ihr Stand,
     * sondern ihre **Differenz** ueber ein Stueck Fahrt - und die ist um
     * Groessenordnungen genauer als alles andere:
     *
     *     Ladestand      Schritt 0,44 pp  =  339 Wh
     *     Entladezaehler Schritt 1/8583   =  0,117 Wh
     *
     * Fast dreitausendmal feiner. Damit wird ein Balken je Minute vom
     * Rauschen zur Messung: Was eine Minute bei sechzig km/h kostet, sind
     * rund 0,25 kWh - beim Ladestand 136 % Fehler, hier 0,05 %.
     *
     * Byte-Lage und Teiler stammen aus dem ESP32-Logger von codingABI
     * (`readAndSendHVTotalChargeDischarge`); spot2000 nennt denselben
     * Teiler. Die Antwort geht ueber mehrere Rahmen - ohne Flusskontrolle
     * und Zusammensetzen kam sie ueberhaupt nicht an. */
    { name: "entladen_kwh", titel: "Entladen gesamt", einheit: "kWh",
      stellen: 2, did: "221E32", adresse: BMS,
      /* **Vorzeichenbehaftet.** Der Entladezaehler kommt als negative Zahl -
       * am Fahrzeug gemessen 0xF7141E0D. Als vorzeichenlose 32-Bit-Zahl
       * gelesen sind das 4,15 Milliarden und damit 482 961 kWh; als
       * vorzeichenbehaftete -17 438,6, und das ist der richtige Wert.
       * codingABI castet dafuer nach `(long)`, was ich beim Uebertragen
       * uebersehen hatte. JavaScript braucht die Umrechnung von Hand.
       *
       * Aufgefallen ist es dem Kreuzvergleich der Pruefseite: 810 kWh/100 km
       * Lebensdauerverbrauch statt der erwarteten 12 bis 40. */
      lesen: (b) => {
        if (b.length < 16) return null;
        const roh = (b[12] * 16777216) + (b[13] * 65536) + (b[14] * 256) + b[15];
        return Math.abs((roh >= 2147483648 ? roh - 4294967296 : roh) / 8583.07);
      },
      /* Dieselbe Antwort traegt auch den Ladezaehler. `weitere` holt ihn
       * aus denselben Bytes, statt die Abfrage ein zweites Mal zu stellen -
       * eine Mehrrahmen-Antwort kostet Zeit. */
      weitere: {
        geladen_kwh: (b) => b.length >= 12
          ? ((b[8] * 16777216) + (b[9] * 65536) + (b[10] * 256) + b[11]) / 8583.07
          : null,
      } },
    { name: "ladegrenze_a", titel: "Ladegrenze", einheit: "A", stellen: 0,
      did: "221E1B", adresse: BMS,
      lesen: (b) => b.length >= 2 ? (b[0] * 256 + b[1]) / 5 : null },
    { name: "betriebsart", titel: "Betriebsart", einheit: null, stellen: 0,
      did: "227448", adresse: BMS, lesen: (b) => b[0] },
    /* Der Strom der PTC-Heizung. Mal Packspannung ergibt das, was die
     * Heizung allein zieht - im Winter die Frage hinter der Frage, weil sie
     * der einzige grosse Verbraucher ist, den man selbst beeinflusst. */
    { name: "ptc_strom_a", titel: "Heizstrom", einheit: "A", stellen: 1,
      did: "221620", adresse: BMS,
      lesen: (b) => b.length ? b[0] / 4 : null },
    { name: "tempo_kmh", titel: "Tempo", einheit: "km/h", stellen: 0,
      did: "22F40D", adresse: BMS, lesen: (b) => b[0] },
    /* Die Aussentemperatur ist der grösste Einzelposten der Kälte und ging
     * bisher aus einer Vorhersage ins Verbrauchsmodell. Aus dem Auto ist sie
     * gemessen, von der Strecke, zur richtigen Zeit. Sie sitzt in einem
     * anderen Steuergerät als die Batterie - siehe KLIMA. */
    /* **Nebenverbraucher als fertige Zahl.** Alles ausser dem Antrieb -
     * Heizung, Klima, Steuergeräte, 12-V-Netz - in kW, direkt aus dem
     * Steuergerät.
     *
     * Vorher wurde das im Stand gemessen und dazwischen fortgeschrieben:
     * Steht das Auto, ist die Packleistung die der Nebenverbraucher. Das
     * war eine brauchbare Näherung, aber eben eine - sie galt nur so lange,
     * wie sich an der Heizung nichts änderte, und im Fahren gar nicht. Ein
     * gemessener Wert schlägt jede Näherung. */
    { name: "nebenverbrauch_kw", titel: "Nebenverbraucher", einheit: "kW", stellen: 2,
      did: "220364", adresse: FAHRZEUG,
      lesen: (b) => b.length >= 2 ? (b[0] * 256 + b[1]) / 10 : null },

    /* Der Kilometerstand - jede Runde, und zwar direkt hinter dem
     * Nebenverbrauch.
     *
     * Er stand vorher am Ende der Liste mit `selten: 20`, wurde also bei
     * 30-Sekunden-Takt nur alle zehn Minuten gelesen. Bei den ersten
     * Testfahrten kam deshalb genau **ein** Wert an - und aus einem Wert
     * lässt sich keine Strecke bilden.
     *
     * Häufiger zu lesen kostet hier nichts ausser der Abfrage selbst: Er
     * sitzt auf derselben Zieladresse wie der Nebenverbrauch, der ohnehin
     * jede Runde drankommt. Der Adresswechsel, wegen dessen er selten
     * gemacht wurde, fällt so gar nicht erst an.
     *
     * Auflösung ist ein Kilometer. Für den Streckenanteil einer einzelnen
     * Runde ist das zu grob, für die Gesamtstrecke einer Fahrt genau
     * richtig - und die ist es, worauf es ankommt. */
    { name: "km_stand", titel: "Kilometerstand", einheit: "km", stellen: 0,
      did: "22295A", adresse: FAHRZEUG,
      lesen: (b) => b.length >= 3 ? (b[0] * 65536) + (b[1] * 256) + b[2] : null },
    { name: "dcdc_strom_a", titel: "DC/DC-Strom", einheit: "A", stellen: 1,
      did: "22465B", adresse: DCDC, selten: 10,
      lesen: (b) => b.length >= 2 ? (b[0] * 256 + b[1]) / 16 : null },
    /* Die nutzbare Kapazitaet des Akkus, wie das Fahrzeug sie kennt.
     *
     * Interessant, weil sie mit den Jahren sinkt - und weil jeder aus dem
     * Ladestand gerechnete Verbrauch mit ihr steht und faellt. Der Wert im
     * Fahrzeugprofil ist eine Angabe aus dem Prospekt; dieser hier ist
     * gemessen.
     *
     * **Die Umrechnung ist nicht belegt.** Die MEB-Referenz fuehrt den
     * Parameter mit "equation missing"; bekannt sind nur die Einheit (Wh),
     * die Adresse und dass die Antwort vier Nutzbytes hat. Angenommen wird
     * deshalb das Naheliegende - der 32-Bit-Wert in Wattstunden - und das
     * Ergebnis gegen eine Plausibilitaetsgrenze gehalten: Ein Autoakku hat
     * zwischen 10 und 200 kWh. Faellt der Wert heraus, stimmt die Annahme
     * nicht, und die Zeile bleibt leer statt eine Zahl zu erfinden.
     *
     * Selten gelesen, weil sie sich nicht waehrend einer Fahrt aendert. */
    { name: "akku_kwh", titel: "Akkukapazität", einheit: "kWh", stellen: 1,
      did: "222AB2", adresse: AKKU11, selten: 40,
      lesen: (b) => {
        /* codingABI: `buffer2unsignedLong() / 1310.77 / 1000` ueber alle
         * vier Datenbytes. WiCANs `[B4:B5] * 50` ist dieselbe Formel, nur
         * auf die oberen zwei Bytes verkuerzt - 50/65536 entspricht
         * 1/1310,7. Vier Bytes sind feiner. */
        if (b.length < 4) return null;
        const roh = (b[0] * 16777216) + (b[1] * 65536) + (b[2] * 256) + b[3];
        const kwh = roh / 1310.77 / 1000;
        return (kwh >= 10 && kwh <= 200) ? kwh : null;
      } },
    /* Die Reichweite, die das Auto selbst ausrechnet. Interessant als
     * Gegenprobe zu jolts Prognose - dieselbe Frage, zwei Antworten. */
    { name: "reichweite_km", titel: "Reichweite (Auto)", einheit: "km",
      stellen: 0, did: "222AB6", adresse: AKKU11, selten: 10,
      /* Die ersten beiden Datenbytes - so liest es codingABI. WiCAN nimmt
       * eines weiter; welches stimmt, sagt die erste Fahrt. Die Schranke
       * faengt den falschen Fall ab. */
      lesen: (b) => {
        if (b.length < 2) return null;
        const km = (b[0] * 256) + b[1];
        return (km >= 0 && km <= 999) ? km : null;
      } },
    /* Die Batterietemperatur. Sie bestimmt die Ladeleistung, und bisher
     * nimmt `laden/kurven.temperatur_faktor` die **Aussen**temperatur als
     * Ersatz - der Kommentar dort sagt selbst, dass sie die Kaelte der
     * Batterie nach einer Nacht im Freien unterschaetzt. Hier ist der
     * richtige Wert. */
    { name: "batterie_c", titel: "Batterietemperatur", einheit: "°C",
      stellen: 1, did: "222A0B", adresse: BMS, selten: 10,
      lesen: (b) => b.length ? (b[0] / 2) - 40 : null },
    /* Die Leistung des Klimakompressors - aus zwei Messungen abgeleitet,
     * nicht aus einer Quelle abgeschrieben.
     *
     * Keine der drei Referenzen (spot2000, WiCAN, codingABI) nennt fuer
     * `220800` eine Umrechnung; spot2000 fuehrt sie als "equation missing".
     * Die Antwort traegt elf Bytes, und vier davon liegen im plausiblen
     * Wattbereich - raten waere hier besonders verlockend und besonders
     * falsch gewesen.
     *
     * Eine Differenzmessung am Fahrzeug hat es entschieden, einmal mit und
     * einmal ohne laufenden Kompressor:
     *
     *              b0    b1b2   b3b4   b5b6   b7
     *     aus    0x10       0      0      0    0
     *     an     0x51    9408   9408   2618   14
     *     (drittens, Teillast)  3648   3712   935    5
     *
     * Daraus:
     *   - `b0` Bit 0 ist an/aus.
     *   - `b1b2` und `b3b4` laufen gleich und viel hoeher - Soll- und
     *     Ist-Drehzahl. Als Watt gelesen waeren 9,4 kW fuer einen
     *     Klimakompressor zu viel.
     *   - `b5b6` ist die **Leistung in Watt**: null wenn aus, 935 bei
     *     Teillast, 2618 bei voller Kuehlung. Genau das Profil.
     *   - `b7` ist dieselbe Groesse groeber - das Verhaeltnis b5b6/b7 ist
     *     in beiden Messungen exakt 187.
     *
     * Die Drehzahl passt dazu: 3650 zu 9408 Umdrehungen ist Faktor 2,58,
     * 935 zu 2618 Watt Faktor 2,80 - naeherungsweise proportional, also
     * etwa gleiches Drehmoment. Die Schranke faengt ab, falls das an einem
     * anderen Fahrzeug doch anders liegt. */
    { name: "kompressor_w", titel: "Klimakompressor", einheit: "W",
      stellen: 0, did: "220800", adresse: KLIMA, selten: 20,
      lesen: (b) => {
        if (b.length < 7) return null;
        const w = (b[5] * 256) + b[6];
        return (w >= 0 && w <= 8000) ? w : null;
      },
      weitere: {
        kompressor_upm: (b) => b.length >= 5 ? (b[3] * 256) + b[4] : null,
        kompressor_an: (b) => b.length ? (b[0] & 1) : null,
      } },
    /* Ganz zum Schluss und nur selten: Diese beiden brauchen einen
     * Protokollwechsel (siehe KLIMA). Geht der schief, sind die
     * Pflichtwerte dieser Runde laengst gelesen. */
    { name: "aussentemp_c", titel: "Aussentemperatur", einheit: "°C", stellen: 1,
      did: "222609", adresse: KLIMA, selten: 20,
      lesen: (b) => b.length ? b[0] / 2 - 50 : null },
    { name: "innentemp_c", titel: "Innentemperatur", einheit: "°C", stellen: 1,
      did: "222613", adresse: KLIMA, selten: 20,
      lesen: (b) => b.length >= 2 ? ((b[0] * 256 + b[1]) / 5) - 40 : null },
  ];


  /* Antwort in Nutzbytes zerlegen. Die Quittung ist `62` + die zwei Bytes
   * der Datenkennung; alles davor ist Absenderkennung und ISO-TP-Kopf,
   * alles danach ist Nutzlast. */
  /* Eine Antwort, die nicht in einen CAN-Rahmen passt, wieder zusammensetzen.
   *
   * Ein Rahmen fasst acht Byte. Laengere Antworten schickt das Steuergeraet
   * als ISO-TP-Folge, und der ELM327 gibt sie zeilenweise aus - mit Kopf und
   * einem Steuerbyte je Zeile:
   *
   *   000007B0 10 14 62 08 00 ..      erster Rahmen: 1L LL = Gesamtlaenge
   *   000007B0 21 .. .. .. .. .. ..   Folgerahmen:   2N    = laufende Nummer
   *
   * Ohne dieses Zusammensetzen las `nutzbytes` die erste Zeile und haengte
   * Koepfe und Steuerbytes der folgenden als Nutzdaten daran - Zahlensalat.
   * Betroffen sind unter anderem die Leistung des Klimakompressors und der
   * Energieinhalt des Akkus.
   *
   * Gibt null zurueck, wenn es keine Mehrrahmen-Antwort ist; dann gilt der
   * einfache Weg darunter. */
  function mehrrahmen(roh) {
    const zeilen = roh.split("\n").map((z) => z.trim()).filter(Boolean);
    if (zeilen.length < 2) return null;
    const teile = [];
    let erwartet = null;
    for (const zeile of zeilen) {
      const hex = zeile.replace(/[^0-9A-Fa-f]/g, "").toUpperCase();
      /* Kopf abschneiden: acht Zeichen bei 29 Bit, drei bei 11 Bit.
       * Zu unterscheiden sind sie an der Laenge der Zeile - der Rest ist
       * immer eine gerade Anzahl Zeichen, also entscheidet die Parität:
       * 8 + 2n ist gerade, 3 + 2n ungerade. Das gilt auch fuer den letzten,
       * kuerzeren Rahmen einer Folge. */
      const ohneKopf = (hex.length % 2) ? hex.slice(3) : hex.slice(8);
      if (ohneKopf.length < 2) continue;
      const pci = parseInt(ohneKopf.slice(0, 2), 16);
      if ((pci & 0xF0) === 0x10) {
        erwartet = ((pci & 0x0F) << 8) | parseInt(ohneKopf.slice(2, 4), 16);
        teile.push(ohneKopf.slice(4));
      } else if ((pci & 0xF0) === 0x20) {
        teile.push(ohneKopf.slice(2));
      }
    }
    if (erwartet === null || !teile.length) return null;
    return teile.join("").slice(0, erwartet * 2);
  }

  function nutzbytes(roh, did) {
    const zusammen = mehrrahmen(roh);
    const hex = (zusammen || roh.replace(/[^0-9A-Fa-f]/g, "")).toUpperCase();
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

  /* Adressen, deren Protokollwechsel schiefging. Einmal reicht: Wer bei
   * jeder zwanzigsten Runde erneut umschaltet und scheitert, zahlt den
   * Umlauf dauerhaft, ohne je einen Wert zu bekommen. */
  const wechselGescheitert = new Set();

  /* Flusskontrolle - der fehlende Handgriff bei langen Antworten.
   *
   * Passt eine Antwort nicht in einen Rahmen, muss der Fragende ein
   * Flow-Control-Paket zuruecksenden, bevor das Steuergeraet weiterschickt.
   * Der ELM327 macht das selbst, aber nur, wenn er weiss, **mit welchem
   * Kopf** - bei einer Standardadresse raet er richtig, bei den
   * MEB-Adressen nicht.
   *
   * jolt setzte diese drei Befehle gar nicht. Damit scheiterte jede
   * mehrteilige Antwort stumm: Der Batteriestrom (221E3D) kam in allen 77
   * Runden der dritten Testfahrt nicht an, und der Energieinhalt des Akkus
   * ebenso wenig. Das WiCAN-Fahrzeugprofil setzt sie vor **jeder** Abfrage;
   * dieselbe Reihenfolge steht hier.
   *
   *   ATFCSH  Kopf des Flow-Control-Pakets
   *   ATFCSD  dessen Inhalt: 30 = weiter, 00 = ohne Pause, 00 = ohne Abstand
   *   ATFCSM1 diese Vorgaben benutzen statt selbst zu raten
   */
  async function flusskontrolle(ziel) {
    if (!ziel.fcsh) return;
    await befehl(`ATFCSH${ziel.fcsh}`);
    await befehl("ATFCSD300000");
    await befehl("ATFCSM1");
  }

  async function messwertLesen(eintrag) {
    const ziel = eintrag.adresse;

    /* Steuergeraete auf einer 11-Bit-Kennung brauchen ein anderes Protokoll
     * als der Rest (siehe KLIMA). Umgeschaltet wird nur fuer die Dauer
     * dieser einen Abfrage und im `finally` wieder zurueck - der Ladestand
     * ist Pflicht, und eine Sitzung, die im falschen Protokoll haengen
     * bleibt, kostet jede weitere Runde. */
    if (ziel.protokoll) {
      if (wechselGescheitert.has(ziel.sh)) return null;
      try {
        await befehl(`ATSP${ziel.protokoll}`);
        await befehl(`ATSH${ziel.sh}`);
        await flusskontrolle(ziel);
        await befehl(`ATCRA${ziel.cra}`);
        return auswerten(await befehl(eintrag.did, 8000), eintrag);
      } catch (fehler) {
        melde(`Protokollwechsel auf ATSP${ziel.protokoll} fehlgeschlagen - `
              + `${ziel.sh} wird in dieser Sitzung nicht mehr versucht.`);
        wechselGescheitert.add(ziel.sh);
        return null;
      } finally {
        // Zurueck ins 29-Bit-Protokoll, und die gemerkte Adresse verwerfen:
        // Der naechste Wert setzt ATCP, ATSH und ATCRA vollstaendig neu.
        try { await befehl("ATSP7"); } catch (e) { /* siehe naechste Runde */ }
        letzteAdresse = null;
      }
    }

    if (!letzteAdresse || letzteAdresse.sh !== ziel.sh) {
      // Die Prioritätsbits gehören dazu: Zwischen Batterie (0x17…) und
      // Fahrzeug (0x17…FC0076) unterscheiden sich die unteren Bits, und ohne
      // Umschalten geht die Anfrage an eine Kennung, auf der niemand hört.
      if (!letzteAdresse || letzteAdresse.cp !== ziel.cp) {
        await befehl(`ATCP${ziel.cp}`);
      }
      await befehl(`ATSH${ziel.sh}`);
      await flusskontrolle(ziel);
      await befehl(`ATCRA${ziel.cra}`);
      letzteAdresse = ziel;
    }
    return auswerten(await befehl(eintrag.did, 8000), eintrag);
  }

  function auswerten(antwort, eintrag) {
    const bytes = nutzbytes(antwort, eintrag.did);
    if (!bytes || !bytes.length) return null;
    const sauber = (wert) => (wert === null || wert === undefined
                              || Number.isNaN(wert)) ? null : wert;
    const wert = sauber(eintrag.lesen(bytes));
    if (!eintrag.weitere) return wert;
    // Mehrere Groessen aus derselben Antwort - siehe `entladen_kwh`.
    const weitere = {};
    for (const [name, lies] of Object.entries(eintrag.weitere)) {
      const w = sauber(lies(bytes));
      if (w !== null) weitere[name] = Math.round(w * 1000) / 1000;
    }
    return { wert, weitere };
  }

  /* Einen vollständigen Satz lesen. Fehler einzelner Grössen werden
   * vermerkt und übergangen - eine Aufzeichnung, die wegen des
   * Kilometerstands abbricht, hätte den Ladestand mit verloren. */
  async function satzLesen(runde) {
    const roh = {};
    for (const eintrag of MESSWERTE) {
      if (eintrag.selten && runde % eintrag.selten !== 0) continue;
      try {
        let wert = await messwertLesen(eintrag);
        if (wert && typeof wert === "object") {
          Object.assign(roh, wert.weitere);
          wert = wert.wert;
        }
        if (wert !== null) {
          roh[eintrag.name] = Math.round(wert * 1000) / 1000;
        } else if (eintrag.pflicht) {
          throw new Error("keine Nutzdaten");
        } else {
          /* Geantwortet, aber ohne brauchbaren Wert.
           *
           * Das ist etwas anderes als ein Zeitablauf, und der Unterschied
           * ist der wichtigste beim Einrichten: Ein Zeitablauf heisst
           * "gerade nicht erreicht", ein leerer Wert heisst "diese
           * Datenkennung stimmt für dieses Fahrzeug nicht".
           *
           * Bisher fiel dieser Fall stumm durch - weder ein Wert noch ein
           * Eintrag in `_fehlend`. In der ersten Aufzeichnung fehlten
           * dadurch vier von dreizehn Messwerten bei allen 77 Runden, ohne
           * dass irgendwo stand, dass sie fehlen. */
          (roh._leer = roh._leer || []).push(eintrag.name);
        }
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
      await gesperrt(() => verbindungAufbauen(geraet));
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
    // Schon verbunden - z.B. weil das automatische Wiederverbinden gerade
    // erst durchkam: nichts tun, statt eine zweite Verbindung aufzubauen,
    // die der ersten nur in die Quere käme.
    if (verbunden_()) return true;
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
    handshake: () => gesperrt(() => reihe(HANDSHAKE)),
    trennen,
    befehl,
    reihe,
    satzLesen,
    socAusRoh,
    socAusAntwort,
    // Fuer die Diagnoseseite: rohe Nutzbytes einer Antwort, inklusive
    // Mehrrahmen-Zusammensetzung.
    nutzbytes,
    NAMEN,
    /* Was ausgelesen wird, mit Beschriftung und Einheit.
     *
     * Damit kann die Oberflaeche jeden Messwert anzeigen, ohne die Liste ein
     * zweites Mal zu fuehren - eine neue Datenkennung taucht dort dann von
     * selbst auf. Die Lesefunktion und die Zieladresse bleiben drinnen; sie
     * gehen niemanden ausserhalb etwas an.
     *
     * `pflicht` wandert mit: Der Ladestand ist der einzige Wert, ohne den
     * eine Runde verworfen wird, und das soll man ihm ansehen koennen. */
    FELDER: MESSWERTE.flatMap((m) => [
      { name: m.name, titel: m.titel || m.name, einheit: m.einheit || null,
        stellen: typeof m.stellen === "number" ? m.stellen : 1,
        pflicht: !!m.pflicht, selten: m.selten || 0 },
      // Werte, die aus derselben Antwort mitkommen, gehoeren genauso in die
      // Tabelle - sonst zeigt sie weniger, als gemessen wird.
      ...Object.keys(m.weitere || {}).map((n) => ({
        name: n, titel: ZUSATZ_TITEL[n] || n,
        // Eigene Einheit, sonst erbt die Drehzahl das Watt des Hauptwerts.
        einheit: ZUSATZ_EINHEIT[n] !== undefined ? ZUSATZ_EINHEIT[n]
          : (m.einheit || null),
        stellen: ZUSATZ_STELLEN[n] !== undefined ? ZUSATZ_STELLEN[n]
          : (typeof m.stellen === "number" ? m.stellen : 1),
        pflicht: false, selten: m.selten || 0 })),
    ]),
  };
})();
