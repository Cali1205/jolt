/* Turning the dongle's answer into numbers: the pure half of reading the car.
 *
 * No connection, no state. `formula` builds the conversion of a table row from
 * a platform profile, `payload_bytes` cuts the payload out of a response (also one that
 * is spread over several CAN frames), `evaluate` does both for one reading.
 * The connection to the dongle stays in obd-core.js, which loads after this file.
 */
window.joltObdDecode = (function () {
  "use strict";

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
  /* Parse vehicle GPS coordinates from DID 22 24 30: two 14-byte null-padded
   * ISO-8859-1 DMS strings (degree sign 0xB0) followed by two trailing bytes.
   * Assigns by hemisphere letter rather than field position; returns null
   * unless both N/S and E/W are present and plausible. */
  function gpsDms(bytes) {
    if (!bytes || !bytes.length) return null;
    // Unicode code points U+0000..U+00FF match ISO-8859-1 one-to-one (0xB0 -> °).
    const parts = String.fromCharCode(...bytes).split("\0");
    let lat = null, lon = null;
    for (const part of parts) {
      const m = /(\d+)°(\d+)'([\d.]+)"([NSEW])/.exec(part);
      if (!m) continue;
      const deg = Number(m[1]), min = Number(m[2]), sec = Number(m[3]);
      if (!Number.isFinite(sec) || min >= 60 || sec >= 60) continue;
      const val = (deg + min / 60 + sec / 3600)
        * (m[4] === "S" || m[4] === "W" ? -1 : 1);
      if ((m[4] === "N" || m[4] === "S") && Math.abs(val) <= 90) lat = val;
      else if ((m[4] === "E" || m[4] === "W") && Math.abs(val) <= 180) lon = val;
    }
    if (lat === null || lon === null || (lat === 0 && lon === 0)) return null;
    return { lat, lon };
  }

  const DECODERS = {
    gps_lat: (bytes) => { const g = gpsDms(bytes); return g ? g.lat : null; },
    gps_lon: (bytes) => { const g = gpsDms(bytes); return g ? g.lon : null; },
  };

  function withPut(fn, row) {
    if (typeof row.put === "number" && row.put > 3) {
      const wrapped = (bytes) => fn(bytes);
      wrapped.put = row.put;
      return wrapped;
    }
    return fn;
  }

  function formula(row) {
    if (typeof row.decode === "function") return withPut(row.decode, row);
    if (typeof row.decode === "string") {
      return withPut(DECODERS[row.decode] || (() => null), row);
    }
    const downhill = row.downhill | 0;
    const len_total = row.len_total || 1;
    const divider = typeof row.divider === "number" ? row.divider : 1;
    const factor = typeof row.factor === "number" ? row.factor : 1;
    const offset = row.offset || 0;
    const pre_offset = row.pre_offset || 0;

    return withPut((bytes) => {
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
    }, row);
  }

  /* The acknowledgement of a request: the service byte plus 0x40, then the
   * rest of the request unchanged. `22028C` is answered with `62028C`
   * (UDS), `015B` with `415B` (standard OBD2). */
  function acknowledgement(request) {
    const service = parseInt(request.slice(0, 2), 16) + 0x40;
    return service.toString(16).toUpperCase().padStart(2, "0")
           + request.slice(2).toUpperCase();
  }

  /* Split a response into payload bytes. Everything before the
   * acknowledgement is sender identifier and ISO-TP header, everything after
   * it is payload. */
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

  function payload_bytes(raw, request) {
    const joined = multiframe(raw);
    const hex = (joined || raw.replace(/[^0-9A-Fa-f]/g, "")).toUpperCase();
    const ack = acknowledgement(request);
    const index = hex.indexOf(ack);
    if (index < 0) return null;
    const rest = hex.slice(index + ack.length);
    const bytes = [];
    for (let i = 0; i + 1 < rest.length; i += 2) {
      bytes.push(parseInt(rest.slice(i, i + 2), 16));
    }
    return bytes;
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
      if (w !== null) {
        const scale = Math.pow(10, Math.max(3, readFn.put || 0));
        further[name] = Math.round(w * scale) / scale;
      }
    }
    return { val, further };
  }

  /* The vehicle identification number in an answer to `0902` (standard
   * OBD2: one count byte, then 17 ASCII characters) or `22F190` (UDS: the 17
   * characters right away). Searched rather than cut at a fixed place, so
   * padding before or after it does not matter. Null without one. */
  function vin(response, request) {
    const bytes = response ? payload_bytes(response, request) : null;
    if (!bytes) return null;
    const text = String.fromCharCode(...bytes);
    const hit = /[A-HJ-NPR-Z0-9]{17}/.exec(text);
    return hit ? hit[0] : null;
  }

  /* What the VIN the dongle read says about the vehicle chosen for a
   * recording (`vehicles`: the app's list, each with `id` and `vin`):
   *
   *   match    the chosen vehicle has this VIN
   *   switch   another vehicle has it: `vehicle` is the one the dongle is in
   *   foreign  the chosen vehicle has a different VIN and no other has this one
   *   learn    nobody has it and the chosen vehicle has none yet
   *   unknown  no VIN was read
   */
  function vinVerdict(read, chosenId, vehicles) {
    const chosen = vehicles.find((v) => v.id === chosenId) || null;
    if (!read) return { kind: "unknown", vehicle: chosen };
    const owner = vehicles.find((v) => v.vin === read);
    if (owner) return { kind: owner.id === chosenId ? "match" : "switch", vehicle: owner };
    return { kind: chosen && chosen.vin ? "foreign" : "learn", vehicle: chosen };
  }

  return { formula, evaluate, payload_bytes, vin, vinVerdict, gpsDms, DECODERS };
})();
