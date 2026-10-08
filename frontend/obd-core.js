/* The OBD2 dongle as a building block - connection, ELM327, readings.
 *
 * Extracted from the diagnostics page because there are two users: that
 * page for troubleshooting, and the jolt UI itself. Two copies of the same
 * ELM command sequence would be two copies that drift apart - and in a
 * place where every difference means another NO DATA at the vehicle.
 *
 * The module knows neither UI controls nor jolt's API. It connects, reads
 * and reports what it does through a callback; what becomes of that is up
 * to the caller. That is why it serves the diagnostics page and the main
 * UI alike.
 *
 * `obtainable()` is the question everything hangs on: Web Bluetooth does
 * not exist on iOS from Apple, only in Bluefy. In Safari the module
 * simply reports itself as unavailable, and the caller then records
 * without a dongle - instead of showing an error message nobody can fix.
 */
window.joltObd = (function () {
  "use strict";

  let reporterOutside = () => {};   // log callback of the caller
  let atDropout = null;      // called when the connection dies

  /* ---------- Observing: log and counters ----------
   *
   * The module keeps its own books instead of leaving that to the callers.
   * The settings view wants to show a log even when the connection has long
   * been up - it was then set up by another view, and nobody kept that
   * view's callback for the later one.
   *
   * Purely observing: none of this changes what is sent or how it is read.
   * The reading formulas and the command sequence remain untouched. */
  const LOG_MAX = 600;
  const logRing = [];

  function report(text, level) {
    logRing.push({ timestamp: Date.now(), variety: level || "", text: String(text) });
    if (logRing.length > LOG_MAX) {
      logRing.splice(0, logRing.length - LOG_MAX);
    }
    try { reporterOutside(text, level); } catch (failure) { /* the caller is at fault, not us */ }
  }

  let lastRecord = null;   // the most recent record read completely

  const counter = {
    commands: { sent: 0, answered: 0, timeout: 0, delayed: 0,
               sumMs: 0, latestMs: null, lastReception: null },
    readings: {},    // name -> { ok, empty, failure, sumMs, latestMs, val, timestamp }
    rounds: { n: 0, failure: 0, sumMs: 0, latestMs: null, timestamp: null },
    connection: { device: "", since: null, dropouts: 0, retries: 0 },
  };

  function readingCounter(name) {
    return counter.readings[name] || (counter.readings[name] = {
      ok: 0, empty: 0, failure: 0, sumMs: 0, latestMs: null,
      val: null, timestamp: null });
  }


  const fullUuid = (id) => `0000${id}-0000-1000-8000-00805f9b34fb`;
  const SERVICES = [
    fullUuid("fff0"),   // Vgate, Veepeak, many clones
    fullUuid("ffe0"),   // HM-10 based
    fullUuid("ffe5"),
    fullUuid("fee7"),
    fullUuid("18f0"),
    "6e400001-b5a3-f393-e0a9-e50e24dcca9e",   // Nordic UART
  ];

  /* Names under which ELM327 dongles announce themselves. The Vgate iCar
   * Pro 2S is called `IOS-Vlink` - read off the device, not guessed.
   *
   * `namePrefix` compares **case-sensitively**: `IOS-vlink` with a small v
   * does not match `IOS-Vlink`, and the dialog would stay empty as if no
   * dongle were there. That is why the short, unambiguous `IOS-` is in the
   * list too - it matches regardless of how the rest is written. */
  const NAMES = ["IOS-Vlink", "IOS-", "Vlink", "vlink", "VLink",
                 "OBD", "Vgate", "VEEPEAK"];

  /* Several attempts, because browsers behave differently here and a
   * single failure does not say what caused it. The last attempt cannot
   * read a service, but it answers the question whether a selection
   * dialog appears at all - and thereby separates "the call is broken"
   * from "the dongle is not found". */

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

  /* The handshake - **confirmed at the vehicle** on 26.08.2026 on an
   * ID.Buzz with a Vgate iCar Pro 2S (ELM327 v2.3).
   *
   * The MEB speaks diagnostics over 29-bit identifiers, not over the short
   * 11-bit addresses of emissions diagnostics. Nothing answered on
   * 7E0/7E2/7E5/7E6, and not because the control units were silent, but
   * because the question was asked in the wrong address form.
   *
   * The basis comes from our own Android logger
   * (Cali1205/OBD2_Logger_Kotlin, core/Obd2.kt, `vwPre`). One thing had to
   * be corrected, and it was the difference between NO DATA and an answer:
   *
   *   The logger sets `ATCP17` **and** gives `ATSH17FC007B` the full
   *   address. These are mutually exclusive. `ATCP` sets the upper five
   *   bits of the 29-bit identifier, `ATSH` supplies the lower 24 - which
   *   is exactly why `ATCP` exists at all. 0x17FC007B breaks down into
   *   0x17 on top and 0xFC007B below, so the correct command is
   *   `ATSHFC007B`. The ELM acknowledges the long form with OK, but then
   *   sends on a different identifier.
   *
   * Also `ATCAF1` instead of `ATCAF0`: with formatting switched off, the
   * ISO-TP length byte would have to be put into the command by hand
   * (`0322028C`). Automatic is less error-prone and handles multi-part
   * responses right away.
   *
   * `ATH1` leaves the sender identifier in the response. One more byte to
   * read costs nothing and, when in doubt, answers the question *who*
   * replied - while searching, that was the most useful line of all.
   *
   * Confirmed response to 22028C:  17FE007B 04 62028C B4 */
  const SEND_BMS = "FC007B";        // lower 24 bits; upper 5 via ATCP17
  const BMS_RECEIVE = "17FE007B";   // receive filter: full identifier
  const HANDSHAKE = [
    "ATZ", "ATE0", "ATL0", "ATS0", "ATH1",
    "ATSP7", "ATCP17", "ATCAF1", "ATST FF",
    `ATSH${SEND_BMS}`, `ATCRA${BMS_RECEIVE}`,
  ];

  let writeChar = null;      // characteristic for sending
  let deviceRemembered = null;  // for reconnecting after a dropout
  let buffer = "";
  let waitOn = null;       // {fulfil, reject, clock}
  let latestAddress = null;
  let notifyCurrent = null;  // currently subscribed characteristic
  let roundRunning = false;   // a record is currently being read
  let listenActive = false; // the dongle only listens (listen())
  let listener = null;       // receives the raw data while listening in

  /* Without this lock, two connection attempts could run at the same
   * time - for example the automatic reconnect in the background and a
   * manual tap on "Dongle verbinden" at the same moment. Both rebuild the
   * same GATT connection and afterwards send the same handshake series;
   * `command()` allows only one waiting command at a time, however, and
   * rejects the second with "es läuft noch ein Befehl". The series hit by
   * that is then considered incomplete - although both attempts would have
   * worked on their own. Everything that connects or sends the handshake
   * therefore runs one after another through `locked()`. */
  let connectionLock = Promise.resolve();

  function locked(task) {
    const own = connectionLock.catch(() => {}).then(task);
    connectionLock = own.catch(() => {});
    return own;
  }

  /* ---------- Connecting ---------- */

  /* Where Bluetooth comes from is decided at runtime.
   *
   * In the browser it is `navigator.bluetooth` - on iOS that means: in
   * Bluefy, because Safari does not know the API. In the iOS app it does
   * not exist either; there `obd-ble-native.js` supplies the same shape on
   * top of CoreBluetooth. Everything below this line is unaware of that,
   * and that is the point: the readings, address blocks and byte formulas
   * in this file were worked out at the vehicle and should not come into
   * being a second time just because the route to the dongle is different. */
  function bt() {
    const native = window.joltBleNative;
    if (native && native.obtainable()) return native.bluetooth;
    return navigator.bluetooth || null;
  }

  async function link() {
    try {
      // Try them in turn instead of betting on one shape: which shape of the
      // request a browser accepts differs - and a single failure does not say
      // what caused it. Every attempt is in the log so the next one does not
      // have to guess again.
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
          // Cancelling by the user is no reason to keep trying - they saw the
          // dialog and closed it. Every further variant would only open it again.
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
        // Without this, `isConnected()` would take a dropout for an existing
        // connection - `writeChar` used to be cleared only on the intentional
        // `detach()`. `attach()` now relies on `isConnected()` to avoid an
        // unnecessary second setup - exactly that would have skipped every
        // further connection attempt after a real dropout.
        writeChar = null;
        // In a tunnel, or when the dongle falls asleep, the connection drops.
        // During a running recording that is no reason to stop - someone who
        // needs a touch first loses half the trip, because nobody at the wheel
        // looks at the screen.
        if (atDropout) atDropout();
      });
      await locked(() => connectionBuildUp(device));
    } catch (failure) {
      report("FEHLER " + failure.message);
    }
  }

  /* Building the GATT connection separately from device selection.
   *
   * `requestDevice` strictly requires a user gesture - a page may not
   * connect by itself on load. `gatt.connect()` on an already permitted
   * device does not. That is exactly why it stands here on its own: after
   * a dropout in a tunnel it allows rebuilding without any action, as long
   * as the device is remembered. */
  async function connectionBuildUp(device) {
    const server = await device.gatt.connect();
    counter.connection.device = device.name || "";

      // Find the usable service: one that has a writable and a notifying
      // characteristic. On some dongles that is the same one.
      let notify = null;
      for (const service of await server.getPrimaryServices()) {
        const chars = await service.getCharacteristics();
        const w = chars.find((c) => c.properties.write
                                 || c.properties.writeWithoutResponse);
        const n = chars.find((c) => c.properties.notify);
        report(`Dienst ${service.uuid}: ${chars.length} Charakteristiken`);
        if (w && n) { writeChar = w; notify = n; break; }
      }
      if (!writeChar || !notify) {
        throw new Error("Kein Dienst mit Schreiben und Benachrichtigen "
                        + "gefunden. Die UUID des Dongles steht oben im "
                        + "Protokoll - sie gehört in die Liste DIENSTE.");
      }

      // Without unsubscribing here, a second setup on the same device (e.g.
      // because an automatic and a manual attempt ran into each other) would
      // add a second listener - every response would arrive twice at `atData`
      // and mess up the buffer.
      if (notifyCurrent) {
        try {
          notifyCurrent.removeEventListener("characteristicvaluechanged", atData);
        } catch (failure) { /* characteristic already gone - nothing to do */ }
      }
      await notify.startNotifications();
      notify.addEventListener("characteristicvaluechanged", atData);
      notifyCurrent = notify;
      report(`Bereit. Schreiben auf ${writeChar.uuid}, Lesen auf ${notify.uuid}`);
      counter.connection.since = Date.now();
  }


  /* ---------- Reconnecting ---------- */

  /* Rebuild after a dropout without any action.
   *
   * Two ways, and which one works depends on the browser: if the device is
   * still remembered, `gatt.connect()` suffices - that needs no gesture.
   * If not (page reloaded), `getDevices()` asks for the already permitted
   * devices; also without a gesture, but not every browser knows it. Only
   * when both fail does someone have to tap - and then that is shown
   * prominently instead of only in the log.
   */
  /* **As long as the trip is running, keep trying.**
   *
   * There used to be `grenze = 6` here. With the growing intervals (3, 6,
   * 12, 24, 48, 60 seconds) the series was used up after about two and a
   * half minutes, and after that jolt **never tried again**.
   *
   * Measured on a real trip: five minutes with the page in the background
   * used up all six attempts. For the remaining fourteen minutes only GPS
   * points arrived - twenty kilometres recorded without a single vehicle
   * value, and without jolt ever trying once more.
   *
   * An upper limit was meant for the case that the dongle was unplugged.
   * But that is exactly what `onward` is for - it ends when the trip
   * ends. Instead of giving up, the interval is merely capped: knocking
   * every twenty seconds costs almost nothing and brings a connection
   * back as soon as it is possible again. */
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
        latestAddress = null;        // address and filters are gone
        // Whatever was in flight before the dropout will no longer arrive.
        culpritResponses = 0;
        buffer = "";
        changeFailed.clear();
        await series(HANDSHAKE);
      });
      report("Wieder verbunden, Handshake erneuert.");
    } catch (failure) {
      // Log only every tenth failed attempt, otherwise the log fills up with
      // the same line on a long trip.
      if (attempt <= 6 || attempt % 10 === 0) {
        report(`Wiederverbinden fehlgeschlagen (Versuch ${attempt}): `
              + failure.message);
      }
      // Growing intervals up to the cap: a tunnel lasts seconds, a sleeping
      // dongle minutes. Knocking every two seconds helps in neither case and
      // costs battery.
      const wait = Math.min(AGAIN_MAX_DISTANCE_MS,
                              3000 * Math.pow(2, attempt - 1));
      // `onward` has to be passed along. Without it, the second attempt got
      // the default `() => true` again, and the chain simply kept running
      // after the trip ended - it reconnected a dongle nobody needs anymore
      // and kept the connection open.
      setTimeout(() => reconnect(attempt + 1, onward), wait);
    }
  }

  /* End the connection deliberately.
   *
   * Needed at the charging post: a locked vehicle that keeps being asked
   * over CAN triggers the alarm system. "No longer reading" is not enough
   * for that - the dongle stays connected, and even the handshake after a
   * dropout talks to the bus again.
   *
   * `deviceRemembered` stays: the device is still permitted, and the next
   * setup manages without a selection dialog. */
  function detach() {
    try {
      if (deviceRemembered && deviceRemembered.gatt && deviceRemembered.gatt.connected) {
        deviceRemembered.gatt.disconnect();
      }
    } catch (failure) {
      report("Trennen: " + failure.message);
    }
    writeChar = null;
    latestAddress = null;
    waitOn = null;
    buffer = "";
    culpritResponses = 0;
    counter.connection.since = null;
    report("Verbindung absichtlich getrennt.");
  }

  /* ---------- Commands ---------- */

  /* How many responses from abandoned commands are still in flight.
   *
   * A timeout gives up the command, but the dongle does not: it answers
   * right afterwards anyway. Without bookkeeping, this late response
   * landed on the **next** command. No wrong numbers came out of it -
   * `payload_bytes` checks that the acknowledgement matches the data
   * identifier -, but every measurement after it was shifted by one and
   * delivered "no payload". And because the charge level is mandatory,
   * that tore down the whole round: a single slow command cost several
   * measurement points instead of one value. */
  let culpritResponses = 0;

  function atData(e) {
    const text = new TextDecoder().decode(e.target.value);
    // While listening in, a stream of frames arrives without a prompt; it
    // belongs to the listener, not to the command-and-response logic.
    if (listener) { listener(text); return; }
    buffer += text;
    // The ELM327 ends every response with '>'. Before that it is
    // incomplete - BLE delivers in chunks of around twenty bytes.
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

  /* Six seconds was too tight: after `ATSP0` the ELM searches for the
 * protocol itself (`SEARCHING...`), and on a vehicle that does not
 * answer that takes up to ten seconds. The answer came one second after
 * the abort - the log then showed a timeout where in reality there was
 * a finding. */
function command(text, limit_ms = 15000, internal = false) {
    return new Promise((fulfil, reject) => {
      if (!writeChar) { reject(new Error("nicht verbunden")); return; }
      // Any character ends the ELM327's listening mode. A command from outside
      // (voltage check, console, read round) would interrupt it in the middle
      // of the stream and mix its response with frames.
      if (listenActive && !internal) {
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
          // The dongle may still answer after all. This one response belongs to
          // no waiting command anymore and is discarded.
          culpritResponses += 1;
          // A timeout here is not a crash but a finding: the dongle did not
          // answer, and that goes in the log.
          report(`(keine Antwort auf ${text} innerhalb ${limit_ms / 1000} s)`);
          reject(new Error("Zeitüberschreitung bei " + text));
        }, limit_ms),
      };
      const bytes = new TextEncoder().encode(text + "\r");
      // writeValueWithoutResponse is newer than writeValue and missing in some
      // implementations - so check for the method and not only for the
      // property of the characteristic.
      const noResponse = writeChar.properties.writeWithoutResponse
        && typeof writeChar.writeValueWithoutResponse === "function";
      const send = noResponse
        ? writeChar.writeValueWithoutResponse(bytes)
        : writeChar.writeValue(bytes);
      send.catch((f) => {
        if (waitOn) { clearTimeout(waitOn.clock); waitOn = null; }
        reject(f);
      });
    });
  }

  /* Carry on instead of aborting. A command without a response is a
   * finding here and no reason to stop - on the first attempt a timeout at
   * `0100` broke off the series, and the `ATDP` following it, of all
   * commands, never ran. Exactly that command would have said whether a
   * protocol was found at all. */
  /* Which commands of the last series failed - so that a message
   * "Handshake unvollständig" can say what the cause was. */
  let seriesError = [];

  async function series(commands) {
    let allOk = true;
    seriesError = [];
    for (const b of commands) {
      const trimmed = b.trim();
      if (!trimmed) continue;
      try {
        await command(trimmed);
      } catch (failure) {
        report("FEHLER " + failure.message + " - weiter mit dem nächsten Befehl");
        allOk = false;
        seriesError.push(`${trimmed}: ${failure.message}`);
        // A late response to the expired command must not be attributed to the
        // next one.
        buffer = "";
        await new Promise((w) => setTimeout(w, 300));
      }
    }
    return allOk;
  }

  /* ---------- Charge level ---------- */

  /* From the raw byte to the two charge levels.
   *
   * The response to 22028C looks like this: `17FE007B 04 62028C B4` - the
   * sender identifier (because of ATH1), the ISO-TP length byte, the
   * acknowledgement `62` = `22` + `40`, the data identifier, then a single
   * payload byte.
   *
   * Two numbers follow from this byte, and mixing them up is the most
   * dangerous mistake at this spot:
   *
   *   SoC(BMS) = raw value / 2.5
   *   SoC(HMI) = SoC(BMS) * 51/46 - 6.4
   *
   * The BMS value is the gross charge level of the battery. The display in
   * the car does not show it - it converts it to the usable window, which
   * leaves a buffer at the top and bottom (computationally: 0 % display at
   * 5.8 % gross, 100 % display at 96 % gross).
   *
   * Confirmed at the vehicle: raw value 0xB4 = 180 gives 72.0 % gross and
   * 73.4 % display - the car showed 74 %. With the divisor 2.55, as our own
   * Android logger uses it, 71.9 % would result and the calculation would
   * not work out. The divisor is 2.5.
   *
   * **jolt needs the HMI value.** `reserve_soc` and `target_soc` are meant
   * for the display value, and that is a good one and a half points above
   * the gross value here. Whoever reports the wrong one sets the reserve
   * too optimistically - and exactly at the lower end, where it counts. */
  /* From the raw byte the two charge levels. Kept separate from
   * `socFromResponse`, because the recording already has the byte
   * decomposed. */
  function socFromRaw(byte) {
    const bms = byte / 2.5;
    return { raw: byte, bms,
             hmi: Math.min(100, Math.max(0, bms * 51 / 46 - 6.4)) };
  }

  function socFromResponse(raw) {
    const hex = raw.replace(/[^0-9A-Fa-f]/g, "").toUpperCase();
    const index = hex.indexOf("62028C");
    if (index < 0) return null;
    const payload = hex.slice(index + 6);
    if (payload.length < 2) return null;
    return socFromRaw(parseInt(payload.slice(0, 2), 16));
  }

  /* ---------- What is read out ---------- */

  /* The readings are in `readings.js` - data identifier, target address,
   * byte position and conversion as a table with named fields.
   *
   * Previously every conversion stood here as its own function. That read
   * well, but meant: whoever wanted to add a data identifier wrote code in
   * the middle of the module that holds the connection to the car. The
   * table separates the two - the knowledge about the vehicle there, the
   * way to the dongle here.
   *
   * The table is resolved exactly once, at load time. Whatever turns up in
   * the process - a typo in an address name, a missing byte position -
   * ends up in `TABLE_ERROR` and becomes visible on the diagnostics page,
   * instead of appearing later as "keine Nutzdaten" at the car. */
  const TABLE = window.joltReadings || { addresses: {}, vals: [] };
  const TABLE_ERROR = [];

  /* Build the read function from a table row.
   *
   * The order of the calculation steps is the spot where a port silently
   * goes wrong, so it is written down here exactly once and not twenty
   * times:
   *
   *     raw   = bytes `downhill` to `downhill + len_total - 1`
   *             (first payload byte and length), most significant first
   *     raw  &= mask
   *     val   = (raw + pre_offset) / divider * factor + offset
   *
   * `pre_offset` and `offset` are two fields because both orders occur:
   * the battery current is `(raw - 150000) / 100`, the battery temperature
   * `raw / 2 - 40`. */
  function formula(row) {
    const downhill = row.downhill | 0;
    const len_total = row.len_total || 1;
    const divider = typeof row.divider === "number" ? row.divider : 1;
    const factor = typeof row.factor === "number" ? row.factor : 1;
    const offset = row.offset || 0;
    const pre_offset = row.pre_offset || 0;

    return (bytes) => {
      // If the bytes do not suffice, the row stays empty - a response that is
      // too short is a finding, not a number.
      if (!bytes || bytes.length < downhill + len_total) return null;
      let raw = 0;
      for (let i = 0; i < len_total; i += 1) raw = raw * 256 + bytes[downhill + i];
      if (row.sign) {
        // Two's complement. Without it the discharge counter read as 4.15
        // billion instead of -17 438 - and both look equally unsuspicious as a
        // number at first.
        const signLimit = Math.pow(2, len_total * 8 - 1);
        if (raw >= signLimit) raw -= signLimit * 2;
      }
      if (typeof row.mask === "number") raw &= row.mask;
      let val = (raw + pre_offset) / divider * factor + offset;
      if (row.amount) val = Math.abs(val);
      // Plausibility bounds: if the value falls outside, the assumed
      // conversion is wrong. Then better nothing than something wrong that
      // looks plausible.
      if (typeof row.min === "number" && val < row.min) return null;
      if (typeof row.max === "number" && val > row.max) return null;
      return val;
    };
  }

  /* What a row needs at minimum for it to become a query. */
  function examineRow(row, addresses, actualExtra) {
    const rowName = row.name || "(ohne Namen)";
    if (!row.name) TABLE_ERROR.push("Eintrag ohne `name`.");
    if (typeof row.len_total !== "number" || row.len_total < 1) {
      TABLE_ERROR.push(`${rowName}: 'laenge' fehlt oder ist kleiner als 1.`);
    }
    if (typeof row.downhill !== "number" || row.downhill < 0) {
      TABLE_ERROR.push(`${rowName}: 'ab' fehlt oder ist negativ.`);
    }
    if (actualExtra) return;
    if (!row.did) TABLE_ERROR.push(`${rowName}: 'did' fehlt.`);
    if (!addresses[row.address]) {
      TABLE_ERROR.push(`${rowName}: Adresse "${row.address}" steht nicht `
                          + `in 'adressen'.`);
    }
  }

  const READINGS = (TABLE.vals || []).map((row) => {
    examineRow(row, TABLE.addresses || {}, false);
    const extra = row.also || [];
    for (const w of extra) examineRow(w, TABLE.addresses || {}, true);

    // `further` stays null instead of empty: `evaluate` uses it to tell
    // whether a single value or a pair comes back.
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
    // At load time, not only while driving: a typo in the table should stand
    // out while someone is still sitting at the computer.
    console.warn("[obd] Fehler in readings.js:\n  "
                 + TABLE_ERROR.join("\n  "));
  }


  /* Split a response into payload bytes. The acknowledgement is `62` + the
   * two bytes of the data identifier; everything before it is sender
   * identifier and ISO-TP header, everything after it is payload. */
  /* Reassemble a response that does not fit into one CAN frame.
   *
   * A frame holds eight bytes. Longer responses are sent by the control
   * unit as an ISO-TP sequence, and the ELM327 outputs them line by line -
   * with a header and a control byte per line:
   *
   *   000007B0 10 14 62 08 00 ..      first frame:       1L LL = total length
   *   000007B0 21 .. .. .. .. .. ..   consecutive frame: 2N    = sequence number
   *
   * Without this reassembly, `payload_bytes` read the first line and
   * appended headers and control bytes of the following ones as payload -
   * a jumble of numbers. Affected, among others, are the power of the
   * air-conditioning compressor and the energy content of the battery.
   *
   * Returns null if it is not a multi-frame response; then the simple
   * path below applies. */
  function multiframe(raw) {
    const rows = raw.split("\n").map((z) => z.trim()).filter(Boolean);
    if (rows.length < 2) return null;
    const parts = [];
    let expected = null;
    for (const row of rows) {
      const hex = row.replace(/[^0-9A-Fa-f]/g, "").toUpperCase();
      /* Cut off the header: eight characters at 29 bit, three at 11 bit.
       * They can be told apart by the length of the line - the rest is
       * always an even number of characters, so parity decides:
       * 8 + 2n is even, 3 + 2n odd. This also holds for the last, shorter
       * frame of a sequence. */
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
    const joined = multiframe(raw);
    const hex = (joined || raw.replace(/[^0-9A-Fa-f]/g, "")).toUpperCase();
    const ack = "62" + did.slice(2).toUpperCase();
    const index = hex.indexOf(ack);
    if (index < 0) return null;
    const rest = hex.slice(index + ack.length);
    const bytes = [];
    for (let i = 0; i + 1 < rest.length; i += 2) {
      bytes.push(parseInt(rest.slice(i, i + 2), 16));
    }
    return bytes;
  }

  /* Addresses whose protocol switch went wrong. Once is enough: whoever
   * switches again every twentieth round and fails pays for the round trip
   * permanently without ever getting a value. */
  const changeFailed = new Set();

  /* Flow control - the missing step for long responses.
   *
   * If a response does not fit into one frame, the asker has to send a
   * flow-control packet back before the control unit continues. The ELM327
   * does that itself, but only if it knows **with which header** - with a
   * standard address it guesses right, with the MEB addresses it does not.
   *
   * jolt did not set these three commands at all. As a result every
   * multi-part response failed silently: the battery current (221E3D) did
   * not arrive in any of the 77 rounds of the third test drive, nor did the
   * energy content of the battery. The WiCAN vehicle profile sets them
   * before **every** query; the same order is used here.
   *
   *   ATFCSH  header of the flow-control packet
   *   ATFCSD  its content: 30 = continue, 00 = no pause, 00 = no spacing
   *   ATFCSM1 use these defaults instead of guessing on its own
   */
  async function flowControl(destination) {
    if (!destination.fcsh) return;
    await command(`ATFCSH${destination.fcsh}`);
    await command("ATFCSD300000");
    await command("ATFCSM1");
  }

  async function readReading(entry) {
    const destination = entry.address;

    /* Control units on an 11-bit identifier need a different protocol than
     * the rest (see CLIMATE). The switch lasts only for the duration of this
     * one query and is reverted in the `finally` - the charge level is
     * mandatory, and a session that gets stuck in the wrong protocol costs
     * every further round. */
    if (destination.trace_log) {
      if (changeFailed.has(destination.sh)) return null;
      try {
        await command(`ATSP${destination.trace_log}`);
        await command(`ATSH${destination.sh}`);
        await flowControl(destination);
        await command(`ATCRA${destination.cra}`);
        return evaluate(await command(entry.did, 8000), entry);
      } catch (failure) {
        report(`Protokollwechsel auf ATSP${destination.trace_log} fehlgeschlagen - `
              + `${destination.sh} wird in dieser Sitzung nicht mehr versucht.`);
        changeFailed.add(destination.sh);
        return null;
      } finally {
        // Back to the 29-bit protocol, and discard the remembered address: the
        // next value sets ATCP, ATSH and ATCRA completely anew.
        try { await command("ATSP7"); } catch (e) { /* see next round */ }
        latestAddress = null;
      }
    }

    if (!latestAddress || latestAddress.sh !== destination.sh) {
      // The priority bits belong to it: between battery (0x17…) and vehicle
      // (0x17…FC0076) the lower bits differ, and without switching the request
      // goes to an identifier nobody listens on.
      if (!latestAddress || latestAddress.cp !== destination.cp) {
        await command(`ATCP${destination.cp}`);
      }
      await command(`ATSH${destination.sh}`);
      await flowControl(destination);
      await command(`ATCRA${destination.cra}`);
      latestAddress = destination;
    }
    return evaluate(await command(entry.did, 8000), entry);
  }

  function evaluate(response, entry) {
    const bytes = payload_bytes(response, entry.did);
    if (!bytes || !bytes.length) return null;
    const orNull = (val) => (val === null || val === undefined
                              || Number.isNaN(val)) ? null : val;
    const val = orNull(entry.load(bytes));
    if (!entry.further) return val;
    // Several quantities from the same response - see `discharge_kwh`.
    const further = {};
    for (const [name, readFn] of Object.entries(entry.further)) {
      const w = orNull(readFn(bytes));
      if (w !== null) further[name] = Math.round(w * 1000) / 1000;
    }
    return { val, further };
  }

  /* Read a complete record. Errors of single quantities are noted and
   * skipped - a recording that aborts because of the odometer would have
   * lost the charge level along with it. */
  async function readRecord(round) {
    if (listenActive) throw new Error("der Dongle lauscht gerade");
    roundRunning = true;
    const startedAt = Date.now();
    let succeeded = false;
    try {
      const record = await recordReadRaw(round);
      succeeded = true;
      lastRecord = { timestamp: Date.now(), lap: round, vals: record };
      return record;
    } finally {
      roundRunning = false;
      const r = counter.rounds;
      r.n += 1;
      if (!succeeded) r.failure += 1;
      r.latestMs = Date.now() - startedAt;
      r.sumMs += r.latestMs;
      r.timestamp = Date.now();
    }
  }

  /* The voltage at the diagnostic connector, in volts - **without touching
   * the CAN bus**.
   *
   * `ATRV` is a command to the ELM327 chip itself: it measures the voltage
   * at pin 16 with its own converter and answers without sending a single
   * frame. That is the difference to everything else in this file - every
   * data identifier wakes the vehicle, `ATRV` does not. That is why it may
   * also run on a locked car.
   *
   * It says something about the state of the car: if the DC/DC converter
   * is running (the car is on or charging), the 12 V voltage is clearly
   * above that of the resting battery. If it drops, the car has gone off -
   * seconds before anyone has got out and locks it.
   *
   * During a read round and with any running command it returns nothing
   * instead of waiting: the voltage is an extra, and `command()` allows
   * only one waiting command anyway. */
  async function voltage() {
    if (!writeChar || waitOn || roundRunning) return null;
    try {
      const response = await command("ATRV", 3000);
      const hit = /(\d{1,2}\.\d+)\s*V?/i.exec(response || "");
      return hit ? parseFloat(hit[1]) : null;
    } catch (failure) {
      return null;
    }
  }

  async function recordReadRaw(round) {
    const raw = {};
    for (const entry of READINGS) {
      if (entry.rarely && round % entry.rarely !== 0) continue;
      const z = readingCounter(entry.name);
      const startedAt = Date.now();
      let counted = false;
      const complete = (outcome) => {
        counted = true;
        z[outcome] += 1;
        z.latestMs = Date.now() - startedAt;
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
          /* Answered, but with no usable value.
           *
           * That is something different from a timeout, and the difference is the
           * most important one when setting up: a timeout means "currently not
           * reachable", an empty value means "this data identifier is not right
           * for this vehicle".
           *
           * Until now this case fell through silently - neither a value nor an
           * entry in `_missing`. In the first recording, four of thirteen readings
           * were missing in all 77 rounds as a result, without anything saying
           * that they were missing. */
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





  /* ---------- Connect without a selection dialog ---------- */

  /* The dongle should not have to be selected every time.
   *
   * The permission for a device persists in the browser once it has been
   * granted - `getDevices()` returns the known ones **without a user
   * gesture**, and `gatt.connect()` on them needs none either. Only
   * `requestDevice` strictly requires one, and exactly that is why it is
   * the second way and not the first.
   *
   * Whether a browser knows `getDevices()` is open: it is newer than the
   * rest of Web Bluetooth. If it is missing, the dialog comes as before -
   * then nothing is lost, it is just one more touch.
   *
   * What is returned is which way was taken, so that the caller can say so
   * instead of keeping quiet about it.
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
    // Take the matching one, not just any: the list contains all devices
    // this page has ever been permitted to use.
    const device = known.find((g) => NAMES.some(
      (n) => (g.name || "").startsWith(n))) || known[0];
    report(`Bekanntes Gerät: ${device.name || "(ohne Namen)"} - verbinde ohne Dialog.`);
    try {
      deviceRemembered = device;
      device.addEventListener("gattserverdisconnected", () => {
        report("Verbindung getrennt.");
        writeChar = null;   // see the reasoning at the other listener above
        if (atDropout) atDropout();
      });
      await locked(() => connectionBuildUp(device));
      return device;
    } catch (failure) {
      // The device is known but not there - switched off, out of range, or
      // currently not plugged into the car.
      report("Bekanntes Gerät antwortet nicht: " + failure.message);
      return null;
    }
  }

  /* First without dialog, then with. This is the way callers should take -
   * the second time it no longer costs a touch. */
  async function attach() {
    // Already connected - e.g. because the automatic reconnect has just come
    // through: do nothing instead of setting up a second connection that
    // would only get in the way of the first.
    if (isConnected()) return true;
    if (await connectWithoutDialog()) return true;
    await link();
    return isConnected();
  }

  function isConnected() { return !!writeChar; }

  /* ---------- Listening in (passive) ----------
   *
   * Listen to what is running on the bus anyway - without asking ourselves.
   * This is the way one could observe a locked, charging car without a
   * diagnostic request triggering the alarm system.
   *
   * **Nothing is sent onto the CAN**: all commands up to ATMA are AT
   * commands that stay in the dongle. ATCSM1 puts the ELM327 into silent
   * monitoring, without acknowledging the frames; whether a clone really
   * does that cannot be checked from here.
   *
   * Whether anything arrives at all is open: on the MEB the OBD port
   * presumably sits behind the diagnostic gateway, on which broadcast data
   * does not run. This function answers exactly that - with a list of the
   * identifiers seen, how often and how variable they are.
   *
   * Afterwards the handshake is repeated (AT commands only): protocol,
   * filter and format are otherwise those of listening in, and the next
   * read round would get nonsense. */
  function writeRaw(text) {
    const bytes = new TextEncoder().encode(text);
    const noResponse = writeChar.properties.writeWithoutResponse
      && typeof writeChar.writeValueWithoutResponse === "function";
    return noResponse ? writeChar.writeValueWithoutResponse(bytes)
                        : writeChar.writeValue(bytes);
  }

  async function listen(options) {
    const o = options || {};
    const trace_log = o.trace_log === "7" ? "7" : "6";
    const duration = Math.max(500, Math.min(120000, Number(o.duration_ms) || 10000));
    if (!writeChar) throw new Error("nicht verbunden");
    if (listenActive) throw new Error("es wird schon gelauscht");
    if (waitOn || roundRunning) throw new Error("es läuft noch ein Befehl");

    listenActive = true;
    const result = { trace_log, duration_ms: duration, total: 0, ids: [],
                       probes: [], hints: [] };
    const ids = new Map();
    let rest = "";
    let stopped = false;
    const startedAt = Date.now();

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
      const tail = t.slice(1).join(" ");
      let e = ids.get(t[0]);
      if (!e) {
        if (ids.size >= 2000) return;
        e = { id: t[0], n: 0, variants: new Set(), tail: "" };
        ids.set(t[0], e);
      }
      e.n += 1;
      e.tail = tail;
      if (e.variants.size < 200) e.variants.add(tail);
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
      // Any character ends listening in; the prompt after it reports that the
      // dongle is ready again.
      await writeRaw("\r");
      const until = Date.now() + 2500;
      while (!stopped && Date.now() < until) {
        await new Promise((w) => setTimeout(w, 25));
      }
      if (rest.trim()) row(rest.trim());
      if (!stopped) hint("keine Eingabeaufforderung nach dem Stopp");
    } finally {
      listener = null;
      buffer = "";
      // Reset the dongle - AT commands only.
      for (const b of HANDSHAKE) {
        try { await command(b, 5000, true); } catch (failure) { /* carry on */ }
      }
      latestAddress = null;
      changeFailed.clear();
      culpritResponses = 0;
      listenActive = false;
    }

    const seconds = Math.max(1, (Date.now() - startedAt) / 1000);
    result.ids = Array.from(ids.values())
      .sort((a, b) => b.n - a.n)
      .map((e) => ({ id: e.id, n: e.n,
                     perSec: Math.round(e.n / (duration / 1000) * 10) / 10,
                     variants: e.variants.size, tail: e.tail }));
    report(`Mithören beendet: ${result.total} Frames, ${result.ids.length} `
          + `Kennungen in ${Math.round(seconds)} s.`);
    return result;
  }

  /* ---------- Outward ---------- */

  return {
    obtainable: () => !!bt(),
    linked: () => !!writeChar,

    /* ---------- Observing (for the settings) ---------- */

    /* Where Bluetooth comes from: "nativ" (CoreBluetooth in the iOS app),
     * "web" (Web Bluetooth, Bluefy/Chrome) or "keiner". */
    transport() {
      const native = window.joltBleNative;
      if (native && native.obtainable()) return "nativ";
      return navigator.bluetooth ? "web" : "keiner";
    },

    /* A copy of the current state: none of it can be changed from outside,
     * and it can be output as JSON (diagnostic report). */
    diagnose() {
      return JSON.parse(JSON.stringify({
        linked: !!writeChar,
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

    /* The last lines of the dongle log, oldest first. */
    trace_log(onlyConspicuous) {
      const conspicuous = /FEHLER|Zeitüberschreitung|keine Antwort|verspätet|getrennt|fehlgeschlagen|NO DATA|ERROR|UNABLE|CAN ERROR|BUS/i;
      return logRing
        .filter((z) => !onlyConspicuous || conspicuous.test(z.text))
        .map((z) => ({ ...z }));
    },
    logClear() { logRing.length = 0; },

    /* Reset counters - for a clean measurement "from now on". */
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

    /* Send a single command by hand - the console in the settings.
     *
     * Afterwards the remembered address is discarded: whoever changes ATSH or
     * ATCRA by hand would otherwise let the next read round begin on the
     * wrong address, because the module believes it is still set. */
    async cli(text) {
      try { return await command(String(text).trim(), 8000); }
      finally { latestAddress = null; }
    },

    /* Forget the remembered device: the next setup asks again. */
    forget() {
      deviceRemembered = null;
      const native = window.joltBleNative;
      if (native && native.forget) native.forget();
      counter.connection.device = "";
      report("Gemerktes Gerät vergessen.");
    },
    /* `reporter` receives every line that would otherwise go into the log;
     * `dropout` is called when the connection dies - whether that is a
     * reason to reconnect is decided by the caller, not by this module. */
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
    // For the diagnostics page: raw payload bytes of a response, including
    // multi-frame reassembly.
    payload_bytes,
    NAMES,
    /* What is read out, with label and unit.
     *
     * With this the UI can show every reading without maintaining the list a
     * second time - a new data identifier then appears there by itself. The
     * read function and the target address stay inside; they are none of
     * anybody's business outside.
     *
     * `required` travels along: the charge level is the only value without
     * which a round is discarded, and that should be visible. */
    FIELDS: READINGS.flatMap((m) => [
      { name: m.name, title: m.title, unit: m.unit,
        put: m.put, required: m.required, rarely: m.rarely },
      // Values that come along from the same response belong in the table just
      // the same - otherwise it shows less than is measured. Title, unit and
      // decimals are in the table itself for them; if missing there, they
      // inherit from the main value. That is why the compressor speed does not
      // appear in watts.
      ...(m.also || []).map((w) => ({
        name: w.name,
        title: w.title || w.name,
        unit: w.unit === undefined ? m.unit : w.unit,
        put: typeof w.put === "number" ? w.put : m.put,
        required: false, rarely: m.rarely })),
    ]),

    /* What was noticed when resolving the table - empty if everything is
     * fine. The diagnostics page displays it: a typo in an address name
     * otherwise looks like a silent control unit at the car. */
    TABLE_ERROR,

    /* The resolved read function of a reading - **for
     * tools/check_readings.js**.
     *
     * Otherwise the read functions stay inside; they are none of anybody's
     * business outside. The exception is justified here: the conversions
     * turned from hand-written functions into table rows, and that no sign
     * and no divisor slipped in the process can only be checked if the check
     * can get at them. Without this access it would have to rebuild the
     * interpreter - and then replica would be compared against replica. */
    readFor(name) {
      for (const m of READINGS) {
        if (m.name === name) return m.load;
        if (m.further && m.further[name]) return m.further[name];
      }
      return null;
    },
  };
})();
