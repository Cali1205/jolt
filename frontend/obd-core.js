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

  let reporterOutside = () => {};   // Protokoll-Rückruf des Aufrufers
  let atDropout = null;      // gerufen, wenn die Verbindung stirbt

  /* ---------- Beobachten: Protokoll und Zähler ----------
   *
   * Das Modul führt selbst Buch, statt es den Aufrufern zu überlassen. Die
   * Einstellungen-Ansicht will auch dann ein Protokoll zeigen, wenn die
   * Verbindung längst steht - sie wurde dann von einer anderen Ansicht
   * aufgebaut, und deren Rückruf hat niemand für die spätere gemerkt.
   *
   * Rein beobachtend: Nichts davon verändert, was gesendet oder wie gelesen
   * wird. Die Messwert-Formeln und die Befehlsfolge bleiben unangetastet. */
  const LOG_MAX = 600;
  const logRing = [];

  function report(text, variety) {
    logRing.push({ timestamp: Date.now(), variety: variety || "", text: String(text) });
    if (logRing.length > LOG_MAX) {
      logRing.splice(0, logRing.length - LOG_MAX);
    }
    try { reporterOutside(text, variety); } catch (failure) { /* der Aufrufer irrt, nicht wir */ }
  }

  let lastRecord = null;   // der zuletzt vollständig gelesene Satz

  const counter = {
    commands: { sent: 0, answered: 0, timeout: 0, delayed: 0,
               sumMs: 0, latestMs: null, lastReception: null },
    readings: {},    // name -> { ok, leer, fehler, summeMs, letzteMs, wert, zeit }
    rounds: { n: 0, failure: 0, sumMs: 0, latestMs: null, timestamp: null },
    connection: { device: "", since: null, dropouts: 0, retries: 0 },
  };

  function readingCounter(name) {
    return counter.readings[name] || (counter.readings[name] = {
      ok: 0, empty: 0, failure: 0, sumMs: 0, latestMs: null,
      val: null, timestamp: null });
  }


  const short = (id) => `0000${id}-0000-1000-8000-00805f9b34fb`;
  const SERVICES = [
    short("fff0"),   // Vgate, Veepeak, viele Klone
    short("ffe0"),   // HM-10-basiert
    short("ffe5"),
    short("fee7"),
    short("18f0"),
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
  const NAMES = ["IOS-Vlink", "IOS-", "Vlink", "vlink", "VLink",
                 "OBD", "Vgate", "VEEPEAK"];

  /* Mehrere Anläufe, weil sich die Browser hier verschieden verhalten und
   * ein einzelner Fehlschlag nicht sagt, woran es lag. Der letzte Anlauf
   * kann zwar keinen Dienst lesen, beantwortet aber die Frage, ob überhaupt
   * ein Auswahldialog erscheint - und trennt damit "der Aufruf ist kaputt"
   * von "der Dongle wird nicht gefunden". */

  const VARIANTS = [
    ["alle Geräte, Dienste angemeldet",
     () => ({ acceptAllDevices: true, optionalServices: SERVICES })],
    ["nach Namen gefiltert",
     () => ({ filters: NAMES.map((n) => ({ namePrefix: n })),
              optionalServices: SERVICES })],
    ["nach bekannten Diensten gefiltert",
     () => ({ filters: SERVICES.map((d) => ({ services: [d] })),
              optionalServices: SERVICES })],
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
  const SEND_BMS = "FC007B";        // untere 24 Bit; obere 5 via ATCP17
  const BMS_EMPFANGEN = "17FE007B";   // Empfangsfilter: volle Kennung
  const HANDSHAKE = [
    "ATZ", "ATE0", "ATL0", "ATS0", "ATH1",
    "ATSP7", "ATCP17", "ATCAF1", "ATST FF",
    `ATSH${SEND_BMS}`, `ATCRA${BMS_EMPFANGEN}`,
  ];

  let write_out = null;      // Charakteristik zum Senden
  let deviceRemembered = null;  // für das Wiederverbinden nach Abriss
  let buffer = "";
  let waitOn = null;       // {erfuellen, ablehnen, uhr}
  let latestAddress = null;
  let notifyCurrent = null;  // aktuell abonnierte Charakteristik
  let roundRunning = false;   // gerade wird ein Satz gelesen
  let listenActive = false; // der Dongle hoert nur zu (lauschen())
  let listener = null;       // nimmt die Rohdaten waehrend des Mithoerens entgegen

  /* Ohne diese Sperre konnten zwei Verbindungsversuche gleichzeitig laufen -
   * etwa das automatische Wiederverbinden im Hintergrund und ein manuelles
   * Antippen von "Dongle verbinden" zur selben Zeit. Beide bauen dieselbe
   * GATT-Verbindung neu auf und schicken danach dieselbe Handshake-Reihe;
   * `befehl()` lässt aber nur einen wartenden Befehl gleichzeitig zu und
   * lehnt den zweiten mit "es läuft noch ein Befehl" ab. Die Reihe, die das
   * trifft, gilt dann als unvollständig - obwohl beide Versuche für sich
   * genommen funktioniert hätten. Alles, was verbindet oder den Handshake
   * schickt, läuft deshalb nacheinander über `gesperrt()`. */
  let connectionLock = Promise.resolve();

  function locked(task) {
    const own = connectionLock.catch(() => {}).then(task);
    connectionLock = own.catch(() => {});
    return own;
  }

  /* ---------- Verbinden ---------- */

  /* Woher das Bluetooth kommt, entscheidet sich zur Laufzeit.
   *
   * Im Browser ist es `navigator.bluetooth` - auf iOS heisst das: in
   * Bluefy, denn Safari kennt die API nicht. In der iOS-App gibt es sie
   * ebenso wenig, dort liefert `obd-ble-native.js` dieselbe Gestalt über
   * CoreBluetooth nach. Alles unterhalb dieser Zeile merkt davon nichts,
   * und das ist der Zweck: Die Messwerte, Adressblöcke und Byte-Formeln in
   * dieser Datei sind am Fahrzeug erarbeitet und sollen nicht ein zweites
   * Mal entstehen, nur weil der Weg zum Dongle ein anderer ist. */
  function bt() {
    const native = window.joltBleNative;
    if (native && native.obtainable()) return native.bluetooth;
    return navigator.bluetooth || null;
  }

  async function link() {
    try {
      // Der Reihe nach durchprobieren, statt auf eine Form zu setzen: Welche
      // Gestalt der Anfrage ein Browser akzeptiert, unterscheidet sich - und
      // ein einzelner Fehlschlag sagt nicht, woran es lag. Jeder Versuch
      // steht im Protokoll, damit der nächste nicht wieder raten muss.
      let device = null;
      let lastError = null;
      for (const [name, build] of VARIANTS) {
        try {
          report(`Versuch: ${name}`);
          device = await bt().requestDevice(build());
          break;
        } catch (failure) {
          lastError = failure;
          report(`  ${failure.name || "Fehler"}: ${failure.message}`);
          // Abbruch durch den Nutzer ist kein Grund weiterzuprobieren - er
          // hat den Dialog gesehen und zugemacht. Jede weitere Variante
          // öffnete ihn nur erneut.
          if (failure.name === "NotFoundError"
              && /cancel|abbruch|user/i.test(failure.message)) throw failure;
        }
      }
      if (!device) throw lastError || new Error("Keine Variante ging.");
      deviceRemembered = device;
      report(`Gerät gewählt: ${device.name || "(ohne Namen)"}`);
      device.addEventListener("gattserverdisconnected", () => {
        report("Verbindung getrennt.");
        counter.connection.dropouts += 1;
        counter.connection.since = null;
        // Ohne das hier hielte `verbunden_()` einen Abriss für eine
        // bestehende Verbindung - `schreiben` wurde bisher nur beim
        // absichtlichen `trennen()` geloescht. `anschliessen()` verlässt
        // sich inzwischen auf `verbunden_()`, um einen unnötigen zweiten
        // Aufbau zu vermeiden - genau das hätte nach einem echten Abriss
        // jeden weiteren Verbindungsversuch übersprungen.
        write_out = null;
        // Im Tunnel oder wenn der Dongle einschläft reisst die Verbindung
        // ab. Während einer laufenden Aufzeichnung ist das kein Grund
        // aufzuhören - wer dann erst eine Berührung braucht, verliert die
        // halbe Fahrt, weil niemand am Steuer auf den Bildschirm sieht.
        if (atDropout) atDropout();
      });
      await locked(() => connectionBuildUp(device));
    } catch (failure) {
      report("FEHLER " + failure.message);
    }
  }

  /* Den GATT-Aufbau getrennt von der Geräteauswahl.
   *
   * `requestDevice` verlangt zwingend eine Nutzergeste - eine Seite darf
   * sich beim Laden nicht von selbst verbinden. `gatt.connect()` auf ein
   * bereits erlaubtes Gerät dagegen nicht. Genau deshalb steht es hier für
   * sich: Nach einem Abriss im Tunnel lässt sich damit ohne Zutun wieder
   * aufbauen, solange das Gerät gemerkt ist. */
  async function connectionBuildUp(device) {
    const server = await device.gatt.connect();
    counter.connection.device = device.name || "";

      // Den brauchbaren Dienst suchen: einer, der eine beschreibbare und eine
      // benachrichtigende Charakteristik hat. Bei manchen Dongles ist das
      // dieselbe.
      let notify = null;
      for (const service of await server.getPrimaryServices()) {
        const chars = await service.getCharacteristics();
        const w = chars.find((c) => c.properties.write
                                 || c.properties.writeWithoutResponse);
        const n = chars.find((c) => c.properties.notify);
        report(`Dienst ${service.uuid}: ${chars.length} Charakteristiken`);
        if (w && n) { write_out = w; notify = n; break; }
      }
      if (!write_out || !notify) {
        throw new Error("Kein Dienst mit Schreiben und Benachrichtigen "
                        + "gefunden. Die UUID des Dongles steht oben im "
                        + "Protokoll - sie gehört in die Liste DIENSTE.");
      }

      // Ohne das Abmelden hier bekäme ein zweiter Aufbau auf dasselbe Gerät
      // (z.B. weil ein automatischer und ein manueller Versuch ineinander
      // liefen) einen zweiten Listener dazu - jede Antwort käme doppelt bei
      // `beiDaten` an und würde den Puffer durcheinanderbringen.
      if (notifyCurrent) {
        try {
          notifyCurrent.removeEventListener("characteristicvaluechanged", atData);
        } catch (failure) { /* Charakteristik schon weg - nichts zu tun */ }
      }
      await notify.startNotifications();
      notify.addEventListener("characteristicvaluechanged", atData);
      notifyCurrent = notify;
      report(`Bereit. Schreiben auf ${write_out.uuid}, Lesen auf ${notify.uuid}`);
      counter.connection.since = Date.now();
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
   * aufzugeben wird der Abstand nur gedeckelt: alle zwanzig Sekunden
   * anklopfen kostet fast nichts und holt eine Verbindung zurück, sobald sie
   * wieder möglich ist. */
  const AGAIN_MAX_DISTANCE_MS = 20000;

  async function reconnect(attempt = 1, onward = () => true) {
    if (!onward()) return;
    try {
      let device = deviceRemembered;
      if (!device && bt() && bt().getDevices) {
        const known = await bt().getDevices();
        device = known.find((g) => NAMES.some((n) => (g.name || "").startsWith(n)))
                 || known[0];
      }
      if (!device) throw new Error("kein gemerktes Gerät");
      report(`Wiederverbinden, Versuch ${attempt} …`);
      counter.connection.retries += 1;
      await locked(async () => {
        await connectionBuildUp(device);
        latestAddress = null;        // Adresse und Filter sind weg
        // Was vor dem Abriss unterwegs war, kommt nicht mehr.
        culpritResponses = 0;
        buffer = "";
        changeFailed.clear();
        await series(HANDSHAKE);
      });
      report("Wieder verbunden, Handshake erneuert.");
    } catch (failure) {
      // Nur jeden zehnten Fehlversuch protokollieren, sonst füllt sich das
      // Protokoll auf einer langen Fahrt mit derselben Zeile.
      if (attempt <= 6 || attempt % 10 === 0) {
        report(`Wiederverbinden fehlgeschlagen (Versuch ${attempt}): `
              + failure.message);
      }
      // Wachsende Abstände bis zur Obergrenze: Ein Tunnel dauert Sekunden,
      // ein eingeschlafener Dongle Minuten. Alle zwei Sekunden zu klopfen
      // hilft in keinem der beiden Fälle und kostet Akku.
      const wait = Math.min(AGAIN_MAX_DISTANCE_MS,
                              3000 * Math.pow(2, attempt - 1));
      // `weiter` muss mitgereicht werden. Ohne das galt beim zweiten
      // Versuch wieder die Vorgabe `() => true`, und die Kette lief nach dem
      // Ende der Fahrt einfach weiter - sie verband einen Dongle neu, den
      // niemand mehr braucht, und hielt die Verbindung offen.
      setTimeout(() => reconnect(attempt + 1, onward), wait);
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
  function detach() {
    try {
      if (deviceRemembered && deviceRemembered.gatt && deviceRemembered.gatt.connected) {
        deviceRemembered.gatt.disconnect();
      }
    } catch (failure) {
      report("Trennen: " + failure.message);
    }
    write_out = null;
    latestAddress = null;
    waitOn = null;
    buffer = "";
    culpritResponses = 0;
    counter.connection.since = null;
    report("Verbindung absichtlich getrennt.");
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
  let culpritResponses = 0;

  function atData(e) {
    const text = new TextDecoder().decode(e.target.value);
    // Beim Mithoeren kommt ein Strom von Frames ohne Eingabeaufforderung; er
    // gehoert dem Mithoerer, nicht der Befehl-und-Antwort-Logik.
    if (listener) { listener(text); return; }
    buffer += text;
    // Der ELM327 schliesst jede Antwort mit '>' ab. Vorher ist sie
    // unvollständig - BLE liefert in Häppchen von rund zwanzig Byte.
    if (!buffer.includes(">")) return;
    const response = buffer.replace(/>/g, "").replace(/\r/g, "\n").trim();
    buffer = "";
    counter.commands.lastReception = Date.now();
    if (culpritResponses > 0) {
      culpritResponses -= 1;
      counter.commands.delayed += 1;
      report(`(verspätete Antwort verworfen: ${response || "leer"})`);
      return;
    }
    report(response || "(leer)", "rein");
    if (waitOn) {
      const duration = Date.now() - waitOn.sent;
      counter.commands.answered += 1;
      counter.commands.sumMs += duration;
      counter.commands.latestMs = duration;
      clearTimeout(waitOn.clock);
      const { fulfil } = waitOn;
      waitOn = null;
      fulfil(response);
    }
  }

  /* Sechs Sekunden waren zu knapp: Nach `ATSP0` sucht der ELM das Protokoll
 * selbst (`SEARCHING...`), und das dauert an einem Fahrzeug, das nicht
 * antwortet, bis zu zehn Sekunden. Die Antwort kam eine Sekunde nach dem
 * Abbruch - im Protokoll stand dann ein Zeitablauf, wo in Wirklichkeit ein
 * Befund war. */
function command(text, limit_ms = 15000, intern = false) {
    return new Promise((fulfil, reject) => {
      if (!write_out) { reject(new Error("nicht verbunden")); return; }
      // Jedes Zeichen beendet das Mithoeren des ELM327. Ein Befehl von aussen
      // (Spannungspruefung, Konsole, Leserunde) wuerde es mitten im Strom
      // abbrechen und dessen Antwort mit Frames vermischen.
      if (listenActive && !intern) {
        reject(new Error("der Dongle lauscht gerade")); return;
      }
      if (waitOn) { reject(new Error("es läuft noch ein Befehl")); return; }
      report(text, "raus");
      counter.commands.sent += 1;
      buffer = "";
      waitOn = {
        fulfil,
        sent: Date.now(),
        clock: setTimeout(() => {
          waitOn = null;
          counter.commands.timeout += 1;
          // Der Dongle antwortet vielleicht doch noch. Diese eine Antwort
          // gehört zu keinem wartenden Befehl mehr und wird verworfen.
          culpritResponses += 1;
          // Ein Zeitablauf ist hier kein Absturz, sondern ein Befund: Der
          // Dongle hat nicht geantwortet, und das steht im Protokoll.
          report(`(keine Antwort auf ${text} innerhalb ${limit_ms / 1000} s)`);
          reject(new Error("Zeitüberschreitung bei " + text));
        }, limit_ms),
      };
      const records = new TextEncoder().encode(text + "\r");
      // writeValueWithoutResponse ist neuer als writeValue und fehlt in
      // manchen Umsetzungen - deshalb auf die Methode prüfen und nicht nur
      // auf die Eigenschaft der Charakteristik.
      const without_response = write_out.properties.writeWithoutResponse
        && typeof write_out.writeValueWithoutResponse === "function";
      const send = without_response
        ? write_out.writeValueWithoutResponse(records)
        : write_out.writeValue(records);
      send.catch((f) => {
        if (waitOn) { clearTimeout(waitOn.clock); waitOn = null; }
        reject(f);
      });
    });
  }

  /* Weitermachen statt abbrechen. Ein Befehl ohne Antwort ist hier ein
   * Befund und kein Grund aufzuhören - beim ersten Versuch riss ein
   * Zeitablauf bei `0100` die Reihe ab, und ausgerechnet das darauf folgende
   * `ATDP` lief nie. Genau der Befehl hätte gesagt, ob überhaupt ein
   * Protokoll gefunden wurde. */
  /* Welche Befehle der letzten Reihe gescheitert sind - damit eine Meldung
   * "Handshake unvollständig" sagen kann, woran es lag. */
  let seriesError = [];

  async function series(commands) {
    let everything_good = true;
    seriesError = [];
    for (const b of commands) {
      const clean = b.trim();
      if (!clean) continue;
      try {
        await command(clean);
      } catch (failure) {
        report("FEHLER " + failure.message + " - weiter mit dem nächsten Befehl");
        everything_good = false;
        seriesError.push(`${clean}: ${failure.message}`);
        // Eine verspätete Antwort auf den abgelaufenen Befehl darf nicht dem
        // nächsten zugeschlagen werden.
        buffer = "";
        await new Promise((w) => setTimeout(w, 300));
      }
    }
    return everything_good;
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
  function socFromRaw(byte) {
    const bms = byte / 2.5;
    return { raw: byte, bms,
             hmi: Math.min(100, Math.max(0, bms * 51 / 46 - 6.4)) };
  }

  function socFromResponse(raw) {
    const hex = raw.replace(/[^0-9A-Fa-f]/g, "").toUpperCase();
    const brand = hex.indexOf("62028C");
    if (brand < 0) return null;
    const payload = hex.slice(brand + 6);
    if (payload.length < 2) return null;
    return socFromRaw(parseInt(payload.slice(0, 2), 16));
  }

  /* ---------- Was ausgelesen wird ---------- */

  /* Die Messwerte stehen in `readings.js` - Datenkennung, Zieladresse,
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
  const TABLE = window.joltReadings || { addresses: {}, vals: [] };
  const TABLE_ERROR = [];

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
  function formula(row) {
    const downhill = row.downhill | 0;
    const len_total = row.len_total || 1;
    const divider = typeof row.divider === "number" ? row.divider : 1;
    const factor = typeof row.factor === "number" ? row.factor : 1;
    const offset = row.offset || 0;
    const pre_offset = row.pre_offset || 0;

    return (bytes) => {
      // Reichen die Bytes nicht, bleibt die Zeile leer - eine zu kurze
      // Antwort ist ein Befund, keine Zahl.
      if (!bytes || bytes.length < downhill + len_total) return null;
      let raw = 0;
      for (let i = 0; i < len_total; i += 1) raw = raw * 256 + bytes[downhill + i];
      if (row.sign) {
        // Zweierkomplement. Ohne das las sich der Entladezaehler als
        // 4,15 Milliarden statt als -17 438 - und beides sieht als Zahl
        // erst einmal gleich unverdaechtig aus.
        const bound = Math.pow(2, len_total * 8 - 1);
        if (raw >= bound) raw -= bound * 2;
      }
      if (typeof row.mask === "number") raw &= row.mask;
      let val = (raw + pre_offset) / divider * factor + offset;
      if (row.amount) val = Math.abs(val);
      // Plausibilitaetsgrenzen: Faellt der Wert heraus, stimmt die
      // angenommene Umrechnung nicht. Dann lieber nichts als etwas
      // Falsches, das plausibel aussieht.
      if (typeof row.min === "number" && val < row.min) return null;
      if (typeof row.max === "number" && val > row.max) return null;
      return val;
    };
  }

  /* Was eine Zeile mindestens braucht, damit daraus eine Abfrage wird. */
  function examineRow(row, addresses, actualExtra) {
    const wo = row.name || "(ohne Namen)";
    if (!row.name) TABLE_ERROR.push("Eintrag ohne `name`.");
    if (typeof row.len_total !== "number" || row.len_total < 1) {
      TABLE_ERROR.push(`${wo}: 'laenge' fehlt oder ist kleiner als 1.`);
    }
    if (typeof row.downhill !== "number" || row.downhill < 0) {
      TABLE_ERROR.push(`${wo}: 'ab' fehlt oder ist negativ.`);
    }
    if (actualExtra) return;
    if (!row.did) TABLE_ERROR.push(`${wo}: 'did' fehlt.`);
    if (!addresses[row.address]) {
      TABLE_ERROR.push(`${wo}: Adresse "${row.address}" steht nicht `
                          + `in 'adressen'.`);
    }
  }

  const READINGS = (TABLE.vals || []).map((row) => {
    examineRow(row, TABLE.addresses || {}, false);
    const extra = row.also || [];
    for (const w of extra) examineRow(w, TABLE.addresses || {}, true);

    // `weitere` bleibt null statt leer: `auswerten` unterscheidet daran,
    // ob ein einzelner Wert oder ein Paar zurueckkommt.
    let further = null;
    if (extra.length) {
      further = {};
      for (const w of extra) further[w.name] = formula(w);
    }

    return {
      name: row.name,
      title: row.title || row.name,
      unit: row.unit === undefined ? null : row.unit,
      put: typeof row.put === "number" ? row.put : 1,
      did: row.did,
      address: (TABLE.addresses || {})[row.address],
      required: !!row.required,
      rarely: row.rarely || 0,
      load: formula(row),
      further,
      also: extra,
    };
  });

  if (TABLE_ERROR.length) {
    // Beim Laden, nicht erst beim Fahren: Ein Tippfehler in der Tabelle
    // soll auffallen, solange noch jemand am Rechner sitzt.
    console.warn("[obd] Fehler in readings.js:\n  "
                 + TABLE_ERROR.join("\n  "));
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
  function multiframe(raw) {
    const rows = raw.split("\n").map((z) => z.trim()).filter(Boolean);
    if (rows.length < 2) return null;
    const parts = [];
    let expected = null;
    for (const row of rows) {
      const hex = row.replace(/[^0-9A-Fa-f]/g, "").toUpperCase();
      /* Kopf abschneiden: acht Zeichen bei 29 Bit, drei bei 11 Bit.
       * Zu unterscheiden sind sie an der Laenge der Zeile - der Rest ist
       * immer eine gerade Anzahl Zeichen, also entscheidet die Parität:
       * 8 + 2n ist gerade, 3 + 2n ungerade. Das gilt auch fuer den letzten,
       * kuerzeren Rahmen einer Folge. */
      const withoutHeader = (hex.length % 2) ? hex.slice(3) : hex.slice(8);
      if (withoutHeader.length < 2) continue;
      const pci = parseInt(withoutHeader.slice(0, 2), 16);
      if ((pci & 0xF0) === 0x10) {
        expected = ((pci & 0x0F) << 8) | parseInt(withoutHeader.slice(2, 4), 16);
        parts.push(withoutHeader.slice(4));
      } else if ((pci & 0xF0) === 0x20) {
        parts.push(withoutHeader.slice(2));
      }
    }
    if (expected === null || !parts.length) return null;
    return parts.join("").slice(0, expected * 2);
  }

  function payload_bytes(raw, did) {
    const together = multiframe(raw);
    const hex = (together || raw.replace(/[^0-9A-Fa-f]/g, "")).toUpperCase();
    const receipt = "62" + did.slice(2).toUpperCase();
    const brand = hex.indexOf(receipt);
    if (brand < 0) return null;
    const rest = hex.slice(brand + receipt.length);
    const bytes = [];
    for (let i = 0; i + 1 < rest.length; i += 2) {
      bytes.push(parseInt(rest.slice(i, i + 2), 16));
    }
    return bytes;
  }

  /* Adressen, deren Protokollwechsel schiefging. Einmal reicht: Wer bei
   * jeder zwanzigsten Runde erneut umschaltet und scheitert, zahlt den
   * Umlauf dauerhaft, ohne je einen Wert zu bekommen. */
  const changeFailed = new Set();

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
  async function flusskontrolle(destination) {
    if (!destination.fcsh) return;
    await command(`ATFCSH${destination.fcsh}`);
    await command("ATFCSD300000");
    await command("ATFCSM1");
  }

  async function readReading(entry) {
    const destination = entry.address;

    /* Steuergeraete auf einer 11-Bit-Kennung brauchen ein anderes Protokoll
     * als der Rest (siehe KLIMA). Umgeschaltet wird nur fuer die Dauer
     * dieser einen Abfrage und im `finally` wieder zurueck - der Ladestand
     * ist Pflicht, und eine Sitzung, die im falschen Protokoll haengen
     * bleibt, kostet jede weitere Runde. */
    if (destination.trace_log) {
      if (changeFailed.has(destination.sh)) return null;
      try {
        await command(`ATSP${destination.trace_log}`);
        await command(`ATSH${destination.sh}`);
        await flusskontrolle(destination);
        await command(`ATCRA${destination.cra}`);
        return evaluate(await command(entry.did, 8000), entry);
      } catch (failure) {
        report(`Protokollwechsel auf ATSP${destination.trace_log} fehlgeschlagen - `
              + `${destination.sh} wird in dieser Sitzung nicht mehr versucht.`);
        changeFailed.add(destination.sh);
        return null;
      } finally {
        // Zurueck ins 29-Bit-Protokoll, und die gemerkte Adresse verwerfen:
        // Der naechste Wert setzt ATCP, ATSH und ATCRA vollstaendig neu.
        try { await command("ATSP7"); } catch (e) { /* siehe naechste Runde */ }
        latestAddress = null;
      }
    }

    if (!latestAddress || latestAddress.sh !== destination.sh) {
      // Die Prioritätsbits gehören dazu: Zwischen Batterie (0x17…) und
      // Fahrzeug (0x17…FC0076) unterscheiden sich die unteren Bits, und ohne
      // Umschalten geht die Anfrage an eine Kennung, auf der niemand hört.
      if (!latestAddress || latestAddress.cp !== destination.cp) {
        await command(`ATCP${destination.cp}`);
      }
      await command(`ATSH${destination.sh}`);
      await flusskontrolle(destination);
      await command(`ATCRA${destination.cra}`);
      latestAddress = destination;
    }
    return evaluate(await command(entry.did, 8000), entry);
  }

  function evaluate(response, entry) {
    const bytes = payload_bytes(response, entry.did);
    if (!bytes || !bytes.length) return null;
    const clean = (val) => (val === null || val === undefined
                              || Number.isNaN(val)) ? null : val;
    const val = clean(entry.load(bytes));
    if (!entry.further) return val;
    // Mehrere Groessen aus derselben Antwort - siehe `entladen_kwh`.
    const further = {};
    for (const [name, lies] of Object.entries(entry.further)) {
      const w = clean(lies(bytes));
      if (w !== null) further[name] = Math.round(w * 1000) / 1000;
    }
    return { val, further };
  }

  /* Einen vollständigen Satz lesen. Fehler einzelner Grössen werden
   * vermerkt und übergangen - eine Aufzeichnung, die wegen des
   * Kilometerstands abbricht, hätte den Ladestand mit verloren. */
  async function readRecord(lap) {
    if (listenActive) throw new Error("der Dongle lauscht gerade");
    roundRunning = true;
    const onset = Date.now();
    let succeeded = false;
    try {
      const record = await recordReadRaw(lap);
      succeeded = true;
      lastRecord = { timestamp: Date.now(), lap, vals: record };
      return record;
    } finally {
      roundRunning = false;
      const r = counter.rounds;
      r.n += 1;
      if (!succeeded) r.failure += 1;
      r.latestMs = Date.now() - onset;
      r.sumMs += r.latestMs;
      r.timestamp = Date.now();
    }
  }

  /* Die Spannung am Diagnosestecker, in Volt - **ohne den CAN-Bus
   * anzufassen**.
   *
   * `ATRV` ist ein Befehl an den ELM327-Chip selbst: Er misst die Spannung an
   * Pin 16 mit seinem eigenen Wandler und antwortet, ohne einen einzigen
   * Rahmen zu senden. Das ist der Unterschied zu allem anderen in dieser
   * Datei - jede Datenkennung weckt das Fahrzeug, `ATRV` nicht. Deshalb darf
   * es auch am abgeschlossenen Auto laufen.
   *
   * Es sagt etwas über den Zustand des Autos: Läuft der DC/DC-Wandler (das
   * Auto ist an oder lädt), liegt die 12-V-Spannung deutlich über der der
   * ruhenden Batterie. Fällt sie ab, ist das Auto ausgegangen - und zwar
   * Sekunden bevor jemand ausgestiegen ist und abschliesst.
   *
   * Während einer Leserunde und bei jedem laufenden Befehl gibt es nichts
   * zurück statt zu warten: Die Spannung ist ein Zusatz, und `befehl()`
   * lässt ohnehin nur einen wartenden Befehl zu. */
  async function voltage() {
    if (!write_out || waitOn || roundRunning) return null;
    try {
      const response = await command("ATRV", 3000);
      const hit = /(\d{1,2}\.\d+)\s*V?/i.exec(response || "");
      return hit ? parseFloat(hit[1]) : null;
    } catch (failure) {
      return null;
    }
  }

  async function recordReadRaw(lap) {
    const raw = {};
    for (const entry of READINGS) {
      if (entry.rarely && lap % entry.rarely !== 0) continue;
      const z = readingCounter(entry.name);
      const onset = Date.now();
      let counted = false;
      const complete = (variety) => {
        counted = true;
        z[variety] += 1;
        z.latestMs = Date.now() - onset;
        z.sumMs += z.latestMs;
        z.timestamp = Date.now();
      };
      try {
        let val = await readReading(entry);
        if (val && typeof val === "object") {
          Object.assign(raw, val.further);
          val = val.val;
        }
        if (val !== null) {
          raw[entry.name] = Math.round(val * 1000) / 1000;
          z.val = raw[entry.name];
          complete("ok");
        } else if (entry.required) {
          complete("fehler");
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
          (raw._empty = raw._empty || []).push(entry.name);
          complete("empty");
        }
      } catch (failure) {
        if (!counted) complete("fehler");
        if (entry.required) throw failure;
        if (!raw._missing) raw._missing = [];
        raw._missing.push(entry.name);
      }
    }
    return raw;
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
  async function connectWithoutDialog() {
    if (!bt() || !bt().getDevices) {
      report("Dieser Browser kann bekannte Geräte nicht wiederfinden "
            + "(getDevices fehlt) - der Auswahldialog kommt.");
      return null;
    }
    let known = [];
    try {
      known = await bt().getDevices();
    } catch (failure) {
      report("Bekannte Geräte nicht abrufbar: " + failure.message);
      return null;
    }
    if (!known.length) {
      report("Noch kein Gerät erlaubt - beim ersten Mal muss ausgewählt werden.");
      return null;
    }
    // Den passenden nehmen, nicht irgendeinen: In der Liste stehen alle
    // Geräte, denen diese Seite je erlaubt wurde.
    const device = known.find((g) => NAMES.some(
      (n) => (g.name || "").startsWith(n))) || known[0];
    report(`Bekanntes Gerät: ${device.name || "(ohne Namen)"} - verbinde ohne Dialog.`);
    try {
      deviceRemembered = device;
      device.addEventListener("gattserverdisconnected", () => {
        report("Verbindung getrennt.");
        write_out = null;   // siehe Begründung beim anderen Listener oben
        if (atDropout) atDropout();
      });
      await locked(() => connectionBuildUp(device));
      return device;
    } catch (failure) {
      // Das Gerät ist bekannt, aber nicht da - ausgeschaltet, ausser
      // Reichweite, oder es steckt gerade nicht im Auto.
      report("Bekanntes Gerät antwortet nicht: " + failure.message);
      return null;
    }
  }

  /* Erst ohne Dialog, dann mit. Das ist der Weg, den Aufrufer nehmen
   * sollten - er kostet beim zweiten Mal keine Berührung mehr. */
  async function attach() {
    // Schon verbunden - z.B. weil das automatische Wiederverbinden gerade
    // erst durchkam: nichts tun, statt eine zweite Verbindung aufzubauen,
    // die der ersten nur in die Quere käme.
    if (connected_()) return true;
    if (await connectWithoutDialog()) return true;
    await link();
    return connected_();
  }

  function connected_() { return !!write_out; }

  /* ---------- Mithoeren (passiv) ----------
   *
   * Zuhoeren, was auf dem Bus ohnehin laeuft - ohne selbst zu fragen. Das ist
   * der Weg, auf dem man ein verriegeltes, ladendes Auto beobachten koennte,
   * ohne dass eine Diagnoseanfrage die Alarmanlage ausloest.
   *
   * Gesendet wird dabei **nichts auf den CAN**: Alle Befehle bis ATMA sind
   * AT-Befehle, die im Dongle bleiben. ATCSM1 stellt den ELM327 auf stilles
   * Mitlesen, ohne Bestaetigung der Frames; ob ein Nachbau das wirklich tut,
   * laesst sich von hier aus nicht pruefen.
   *
   * Ob ueberhaupt etwas ankommt, ist offen: Der OBD-Anschluss haengt beim MEB
   * vermutlich hinter dem Diagnose-Gateway, auf dem Broadcast-Daten nicht
   * laufen. Diese Funktion beantwortet genau das - mit einer Liste der
   * gesehenen Kennungen, wie oft und wie veraenderlich sie sind.
   *
   * Danach wird der Handshake wiederholt (nur AT-Befehle): Protokoll, Filter
   * und Format sind sonst die des Mithoerens, und die naechste Leserunde
   * bekaeme Unsinn. */
  function writeRaw(text) {
    const records = new TextEncoder().encode(text);
    const without_response = write_out.properties.writeWithoutResponse
      && typeof write_out.writeValueWithoutResponse === "function";
    return without_response ? write_out.writeValueWithoutResponse(records)
                        : write_out.writeValue(records);
  }

  async function listen(options) {
    const o = options || {};
    const trace_log = o.trace_log === "7" ? "7" : "6";
    const duration = Math.max(500, Math.min(120000, Number(o.duration_ms) || 10000));
    if (!write_out) throw new Error("nicht verbunden");
    if (listenActive) throw new Error("es wird schon gelauscht");
    if (waitOn || roundRunning) throw new Error("es läuft noch ein Befehl");

    listenActive = true;
    const result = { trace_log, duration_ms: duration, total: 0, ids: [],
                       probes: [], hints: [] };
    const ids = new Map();
    let rest = "";
    let stopped = false;
    const onset = Date.now();

    const hint = (t) => {
      if (result.hints.length < 8 && !result.hints.includes(t)) {
        result.hints.push(t);
      }
    };
    const row = (z) => {
      const t = z.toUpperCase().split(/\s+/);
      const actualFrame = /^(?:[0-9A-F]{3}|[0-9A-F]{8})$/.test(t[0])
        && t.length > 1 && t.slice(1).every((b) => /^[0-9A-F]{2}$/.test(b));
      if (!actualFrame) { hint(z.slice(0, 60)); return; }
      result.total += 1;
      const records = t.slice(1).join(" ");
      let e = ids.get(t[0]);
      if (!e) {
        if (ids.size >= 2000) return;
        e = { id: t[0], n: 0, variants: new Set(), tail: "" };
        ids.set(t[0], e);
      }
      e.n += 1;
      e.tail = records;
      if (e.variants.size < 200) e.variants.add(records);
      if (result.probes.length < 30) result.probes.push(z);
    };
    const collect = (text) => {
      if (text.includes(">")) stopped = true;
      rest += text.replace(/>/g, "");
      const parts = rest.split(/[\r\n]+/);
      rest = parts.pop();
      for (const z of parts) { if (z.trim()) row(z.trim()); }
    };

    try {
      for (const b of ["ATE0", "ATL0", "ATS1", "ATH1", "ATCAF0",
                       `ATSP${trace_log}`, "ATCSM1", "ATCRA"]) {
        try {
          const response = await command(b, 4000, true);
          if (/\?/.test(response || "")) hint(`${b}: vom Dongle nicht verstanden`);
        } catch (failure) {
          hint(`${b}: ${failure.message}`);
        }
      }
      report(`Mithören beginnt (Protokoll ${trace_log}, ${duration / 1000} s) - `
            + "gesendet werden nur AT-Befehle, nichts auf den CAN.");
      buffer = "";
      listener = collect;
      await writeRaw("ATMA\r");
      await new Promise((w) => setTimeout(w, duration));
      // Jedes Zeichen beendet das Mithören; die Eingabeaufforderung danach
      // meldet, dass der Dongle wieder bereit ist.
      await writeRaw("\r");
      const upto = Date.now() + 2500;
      while (!stopped && Date.now() < upto) {
        await new Promise((w) => setTimeout(w, 25));
      }
      if (rest.trim()) row(rest.trim());
      if (!stopped) hint("keine Eingabeaufforderung nach dem Stopp");
    } finally {
      listener = null;
      buffer = "";
      // Den Dongle zurückstellen - nur AT-Befehle.
      for (const b of HANDSHAKE) {
        try { await command(b, 5000, true); } catch (failure) { /* weiter */ }
      }
      latestAddress = null;
      changeFailed.clear();
      culpritResponses = 0;
      listenActive = false;
    }

    const seconds = Math.max(1, (Date.now() - onset) / 1000);
    result.ids = Array.from(ids.values())
      .sort((a, b) => b.n - a.n)
      .map((e) => ({ id: e.id, n: e.n,
                     perSec: Math.round(e.n / (duration / 1000) * 10) / 10,
                     variants: e.variants.size, tail: e.tail }));
    report(`Mithören beendet: ${result.total} Frames, ${result.ids.length} `
          + `Kennungen in ${Math.round(seconds)} s.`);
    return result;
  }

  /* ---------- Nach aussen ---------- */

  return {
    obtainable: () => !!bt(),
    linked: () => !!write_out,

    /* ---------- Beobachten (für die Einstellungen) ---------- */

    /* Woher das Bluetooth kommt: "nativ" (CoreBluetooth in der iOS-App),
     * "web" (Web Bluetooth, Bluefy/Chrome) oder "keiner". */
    transport() {
      const native = window.joltBleNative;
      if (native && native.obtainable()) return "nativ";
      return navigator.bluetooth ? "web" : "keiner";
    },

    /* Eine Kopie des Ist-Zustands: nichts davon lässt sich von aussen
     * verändern, und es ist als JSON ausgebbar (Diagnosebericht). */
    diagnose() {
      return JSON.parse(JSON.stringify({
        linked: !!write_out,
        transport: this.transport(),
        commandRunning: !!waitOn,
        roundRunning,
        culpritResponses,
        rememberedDevice: deviceRemembered ? (deviceRemembered.name || "(ohne Namen)") : null,
        changeFailed: Array.from(changeFailed),
        tablesError: TABLE_ERROR,
        latestAddress: latestAddress ? latestAddress.sh : null,
        ...counter,
        lastRecord,
      }));
    },

    /* Die letzten Zeilen des Dongle-Protokolls, älteste zuerst. */
    trace_log(onlyConspicuous) {
      const conspicuous = /FEHLER|Zeitüberschreitung|keine Antwort|verspätet|getrennt|fehlgeschlagen|NO DATA|ERROR|UNABLE|CAN ERROR|BUS/i;
      return logRing
        .filter((z) => !onlyConspicuous || conspicuous.test(z.text))
        .map((z) => ({ ...z }));
    },
    logClear() { logRing.length = 0; },

    /* Zähler zurücksetzen - für eine saubere Messung "ab jetzt". */
    resetCounter() {
      counter.commands = { sent: 0, answered: 0, timeout: 0,
                          delayed: 0, sumMs: 0, latestMs: null,
                          lastReception: null };
      counter.readings = {};
      counter.rounds = { n: 0, failure: 0, sumMs: 0, latestMs: null,
                         timestamp: null };
      counter.connection.dropouts = 0;
      counter.connection.retries = 0;
    },

    /* Einen einzelnen Befehl von Hand senden - die Konsole der Einstellungen.
     *
     * Danach wird die gemerkte Adresse verworfen: Wer ATSH oder ATCRA von
     * Hand ändert, würde sonst die nächste Leserunde auf der falschen
     * Adresse beginnen lassen, weil das Modul glaubt, sie stehe noch. */
    async cli(text) {
      try { return await command(String(text).trim(), 8000); }
      finally { latestAddress = null; }
    },

    /* Das gemerkte Gerät vergessen: Der nächste Aufbau fragt wieder nach. */
    forget() {
      deviceRemembered = null;
      const native = window.joltBleNative;
      if (native && native.forget) native.forget();
      counter.connection.device = "";
      report("Gemerktes Gerät vergessen.");
    },
    /* `melder` bekommt jede Zeile, die sonst im Protokoll stünde; `abriss`
     * wird gerufen, wenn die Verbindung stirbt - ob das ein Grund zum
     * Wiederverbinden ist, entscheidet der Aufrufer, nicht dieses Modul. */
    set_up(reporter, dropout) { reporterOutside = reporter || reporterOutside; atDropout = dropout; },
    link,
    attach,
    reconnect,
    handshake: () => locked(() => series(HANDSHAKE)),
    listen: (options) => locked(() => listen(options)),
    listens: () => listenActive,
    seriesError: () => seriesError.slice(),
    detach,
    command,
    series,
    readRecord,
    voltage,
    socFromRaw,
    socFromResponse,
    // Fuer die Diagnoseseite: rohe Nutzbytes einer Antwort, inklusive
    // Mehrrahmen-Zusammensetzung.
    payload_bytes,
    NAMES,
    /* Was ausgelesen wird, mit Beschriftung und Einheit.
     *
     * Damit kann die Oberflaeche jeden Messwert anzeigen, ohne die Liste ein
     * zweites Mal zu fuehren - eine neue Datenkennung taucht dort dann von
     * selbst auf. Die Lesefunktion und die Zieladresse bleiben drinnen; sie
     * gehen niemanden ausserhalb etwas an.
     *
     * `pflicht` wandert mit: Der Ladestand ist der einzige Wert, ohne den
     * eine Runde verworfen wird, und das soll man ihm ansehen koennen. */
    FIELDS: READINGS.flatMap((m) => [
      { name: m.name, title: m.title, unit: m.unit,
        put: m.put, required: m.required, rarely: m.rarely },
      // Werte, die aus derselben Antwort mitkommen, gehoeren genauso in die
      // Tabelle - sonst zeigt sie weniger, als gemessen wird. Titel,
      // Einheit und Stellen stehen bei ihnen in der Tabelle selbst; fehlen
      // sie dort, erben sie vom Hauptwert. Das ist der Grund, warum die
      // Drehzahl des Kompressors nicht in Watt erscheint.
      ...(m.also || []).map((w) => ({
        name: w.name,
        title: w.title || w.name,
        unit: w.unit === undefined ? m.unit : w.unit,
        put: typeof w.put === "number" ? w.put : m.put,
        required: false, rarely: m.rarely })),
    ]),

    /* Was beim Aufloesen der Tabelle auffiel - leer, wenn alles stimmt.
     * Die Diagnoseseite zeigt es an: Ein Tippfehler im Adressnamen sieht
     * am Auto sonst aus wie ein schweigendes Steuergeraet. */
    TABLE_ERROR,

    /* Die aufgeloeste Lesefunktion eines Messwerts - **fuer
     * tools/check_readings.js**.
     *
     * Sonst bleiben die Lesefunktionen drinnen; sie gehen niemanden
     * ausserhalb etwas an. Hier ist die Ausnahme begruendet: Die
     * Umrechnungen sind aus handgeschriebenen Funktionen zu Tabellenzeilen
     * geworden, und dass dabei kein Vorzeichen und kein Teiler verrutscht
     * ist, laesst sich nur pruefen, wenn die Pruefung an sie herankommt.
     * Ohne diesen Zugang muesste sie den Interpreter nachbauen - und
     * verglichen wuerde dann Nachbau gegen Nachbau. */
    readFor(name) {
      for (const m of READINGS) {
        if (m.name === name) return m.load;
        if (m.further && m.further[name]) return m.further[name];
      }
      return null;
    },
  };
})();
