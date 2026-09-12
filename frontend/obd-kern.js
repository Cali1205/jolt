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

  /* Woher das Bluetooth kommt, entscheidet sich zur Laufzeit.
   *
   * Im Browser ist es `navigator.bluetooth` - auf iOS heisst das: in
   * Bluefy, denn Safari kennt die API nicht. In der iOS-App gibt es sie
   * ebenso wenig, dort liefert `obd-ble-nativ.js` dieselbe Gestalt über
   * CoreBluetooth nach. Alles unterhalb dieser Zeile merkt davon nichts,
   * und das ist der Zweck: Die Messwerte, Adressblöcke und Byte-Formeln in
   * dieser Datei sind am Fahrzeug erarbeitet und sollen nicht ein zweites
   * Mal entstehen, nur weil der Weg zum Dongle ein anderer ist. */
  function bt() {
    const nativ = window.joltBleNativ;
    if (nativ && nativ.verfuegbar()) return nativ.bluetooth;
    return navigator.bluetooth || null;
  }

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
          geraet = await bt().requestDevice(bauen());
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
        // Ohne das hier hielte `verbunden_()` einen Abriss für eine
        // bestehende Verbindung - `schreiben` wurde bisher nur beim
        // absichtlichen `trennen()` geloescht. `anschliessen()` verlässt
        // sich inzwischen auf `verbunden_()`, um einen unnötigen zweiten
        // Aufbau zu vermeiden - genau das hätte nach einem echten Abriss
        // jeden weiteren Verbindungsversuch übersprungen.
        schreiben = null;
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
      if (!geraet && bt() && bt().getDevices) {
        const bekannt = await bt().getDevices();
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

  /* Die Messwerte stehen in `messwerte.js` - Datenkennung, Zieladresse,
   * Byte-Lage und Umrechnung als Tabelle mit benannten Feldern.
   *
   * Vorher stand hier jede Umrechnung als eigene Funktion. Das las sich
   * gut, hiess aber: Wer eine Datenkennung ergaenzen wollte, schrieb Code
   * mitten in den Baustein, der die Verbindung zum Auto haelt. Die
   * Tabelle trennt beides - dort das Wissen ueber das Fahrzeug, hier der
   * Weg zum Dongle.
   *
   * Aufgeloest wird die Tabelle genau einmal, beim Laden. Was dabei
   * auffaellt - ein Tippfehler im Adressnamen, eine fehlende Byte-Lage -
   * landet in `TABELLE_FEHLER` und wird auf der Diagnoseseite sichtbar,
   * statt spaeter als "keine Nutzdaten" am Auto aufzutauchen. */
  const TABELLE = window.joltMesswerte || { adressen: {}, werte: [] };
  const TABELLE_FEHLER = [];

  /* Aus einer Tabellenzeile die Lesefunktion bauen.
   *
   * Die Reihenfolge der Rechenschritte ist die Stelle, an der eine
   * Portierung still danebengeht, deshalb steht sie hier genau einmal und
   * nicht zwanzigmal:
   *
   *     roh   = Bytes `ab` bis `ab + laenge - 1`, hoechstwertiges zuerst
   *     roh  &= maske
   *     wert  = (roh + vorversatz) / teiler * faktor + versatz
   *
   * `vorversatz` und `versatz` sind zwei Felder, weil beide Reihenfolgen
   * vorkommen: Der Batteriestrom ist `(roh - 150000) / 100`, die
   * Batterietemperatur `roh / 2 - 40`. */
  function formel(zeile) {
    const ab = zeile.ab | 0;
    const laenge = zeile.laenge || 1;
    const teiler = typeof zeile.teiler === "number" ? zeile.teiler : 1;
    const faktor = typeof zeile.faktor === "number" ? zeile.faktor : 1;
    const versatz = zeile.versatz || 0;
    const vorversatz = zeile.vorversatz || 0;

    return (bytes) => {
      // Reichen die Bytes nicht, bleibt die Zeile leer - eine zu kurze
      // Antwort ist ein Befund, keine Zahl.
      if (!bytes || bytes.length < ab + laenge) return null;
      let roh = 0;
      for (let i = 0; i < laenge; i += 1) roh = roh * 256 + bytes[ab + i];
      if (zeile.vorzeichen) {
        // Zweierkomplement. Ohne das las sich der Entladezaehler als
        // 4,15 Milliarden statt als -17 438 - und beides sieht als Zahl
        // erst einmal gleich unverdaechtig aus.
        const grenze = Math.pow(2, laenge * 8 - 1);
        if (roh >= grenze) roh -= grenze * 2;
      }
      if (typeof zeile.maske === "number") roh &= zeile.maske;
      let wert = (roh + vorversatz) / teiler * faktor + versatz;
      if (zeile.betrag) wert = Math.abs(wert);
      // Plausibilitaetsgrenzen: Faellt der Wert heraus, stimmt die
      // angenommene Umrechnung nicht. Dann lieber nichts als etwas
      // Falsches, das plausibel aussieht.
      if (typeof zeile.min === "number" && wert < zeile.min) return null;
      if (typeof zeile.max === "number" && wert > zeile.max) return null;
      return wert;
    };
  }

  /* Was eine Zeile mindestens braucht, damit daraus eine Abfrage wird. */
  function zeilePruefen(zeile, adressen, istZusatz) {
    const wo = zeile.name || "(ohne Namen)";
    if (!zeile.name) TABELLE_FEHLER.push("Eintrag ohne `name`.");
    if (typeof zeile.laenge !== "number" || zeile.laenge < 1) {
      TABELLE_FEHLER.push(`${wo}: 'laenge' fehlt oder ist kleiner als 1.`);
    }
    if (typeof zeile.ab !== "number" || zeile.ab < 0) {
      TABELLE_FEHLER.push(`${wo}: 'ab' fehlt oder ist negativ.`);
    }
    if (istZusatz) return;
    if (!zeile.did) TABELLE_FEHLER.push(`${wo}: 'did' fehlt.`);
    if (!adressen[zeile.adresse]) {
      TABELLE_FEHLER.push(`${wo}: Adresse "${zeile.adresse}" steht nicht `
                          + `in 'adressen'.`);
    }
  }

  const MESSWERTE = (TABELLE.werte || []).map((zeile) => {
    zeilePruefen(zeile, TABELLE.adressen || {}, false);
    const zusatz = zeile.auch || [];
    for (const w of zusatz) zeilePruefen(w, TABELLE.adressen || {}, true);

    // `weitere` bleibt null statt leer: `auswerten` unterscheidet daran,
    // ob ein einzelner Wert oder ein Paar zurueckkommt.
    let weitere = null;
    if (zusatz.length) {
      weitere = {};
      for (const w of zusatz) weitere[w.name] = formel(w);
    }

    return {
      name: zeile.name,
      titel: zeile.titel || zeile.name,
      einheit: zeile.einheit === undefined ? null : zeile.einheit,
      stellen: typeof zeile.stellen === "number" ? zeile.stellen : 1,
      did: zeile.did,
      adresse: (TABELLE.adressen || {})[zeile.adresse],
      pflicht: !!zeile.pflicht,
      selten: zeile.selten || 0,
      lesen: formel(zeile),
      weitere,
      auch: zusatz,
    };
  });

  if (TABELLE_FEHLER.length) {
    // Beim Laden, nicht erst beim Fahren: Ein Tippfehler in der Tabelle
    // soll auffallen, solange noch jemand am Rechner sitzt.
    console.warn("[obd] Fehler in messwerte.js:\n  "
                 + TABELLE_FEHLER.join("\n  "));
  }


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
    if (!bt() || !bt().getDevices) {
      melde("Dieser Browser kann bekannte Geräte nicht wiederfinden "
            + "(getDevices fehlt) - der Auswahldialog kommt.");
      return null;
    }
    let bekannt = [];
    try {
      bekannt = await bt().getDevices();
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
        schreiben = null;   // siehe Begründung beim anderen Listener oben
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
    verfuegbar: () => !!bt(),
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
      { name: m.name, titel: m.titel, einheit: m.einheit,
        stellen: m.stellen, pflicht: m.pflicht, selten: m.selten },
      // Werte, die aus derselben Antwort mitkommen, gehoeren genauso in die
      // Tabelle - sonst zeigt sie weniger, als gemessen wird. Titel,
      // Einheit und Stellen stehen bei ihnen in der Tabelle selbst; fehlen
      // sie dort, erben sie vom Hauptwert. Das ist der Grund, warum die
      // Drehzahl des Kompressors nicht in Watt erscheint.
      ...(m.auch || []).map((w) => ({
        name: w.name,
        titel: w.titel || w.name,
        einheit: w.einheit === undefined ? m.einheit : w.einheit,
        stellen: typeof w.stellen === "number" ? w.stellen : m.stellen,
        pflicht: false, selten: m.selten })),
    ]),

    /* Was beim Aufloesen der Tabelle auffiel - leer, wenn alles stimmt.
     * Die Diagnoseseite zeigt es an: Ein Tippfehler im Adressnamen sieht
     * am Auto sonst aus wie ein schweigendes Steuergeraet. */
    TABELLE_FEHLER,

    /* Die aufgeloeste Lesefunktion eines Messwerts - **fuer
     * tools/check_messwerte.js**.
     *
     * Sonst bleiben die Lesefunktionen drinnen; sie gehen niemanden
     * ausserhalb etwas an. Hier ist die Ausnahme begruendet: Die
     * Umrechnungen sind aus handgeschriebenen Funktionen zu Tabellenzeilen
     * geworden, und dass dabei kein Vorzeichen und kein Teiler verrutscht
     * ist, laesst sich nur pruefen, wenn die Pruefung an sie herankommt.
     * Ohne diesen Zugang muesste sie den Interpreter nachbauen - und
     * verglichen wuerde dann Nachbau gegen Nachbau. */
    lesenFuer(name) {
      for (const m of MESSWERTE) {
        if (m.name === name) return m.lesen;
        if (m.weitere && m.weitere[name]) return m.weitere[name];
      }
      return null;
    },
  };
})();
