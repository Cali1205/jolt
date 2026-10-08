/* What jolt reads from the vehicle - as a table, not as a program.
 *
 * Previously every conversion stood as its own JavaScript function in
 * `obd-core.js`. Whoever wanted to add a data identifier had to write
 * code there; whoever wanted to correct a byte position had to read it.
 * Here both are one line with named fields - and the fields are exactly
 * the quantities that stand next to it in the MEB references anyway.
 *
 * A JS file and not a .json: it is the same table, just with comments.
 * And those carry here what would otherwise be lost - where a formula
 * comes from, which one is confirmed at the vehicle and which was
 * guessed. Besides, it loads through a script tag, without `obd-core.js`
 * having to wait for a `fetch` that runs into a dead spot on the way.
 *
 * ===================================================================
 * How a row is read
 * ===================================================================
 *
 * The payload bytes are cut from the control unit's response - that is
 * everything after the acknowledgement (`62` + the two bytes of the data
 * identifier). `b[0]` is thus the first byte **after** the
 * acknowledgement. From that:
 *
 *     raw   = the bytes `downhill` to `downhill + len_total - 1`, most
 *             significant first
 *     raw  &= mask                        (if given)
 *     val   = (raw + pre_offset) / divider * factor + offset
 *     val   = |val|                       (if amount)
 *     val   = null if val < min or val > max
 *
 * If the bytes do not reach to `downhill + len_total`, the result is
 * null - the row then stays empty instead of inventing a number.
 *
 * ===================================================================
 * The fields
 * ===================================================================
 *
 * Mandatory per entry:
 *
 *   name       Key in the raw values, e.g. "soc_raw". Changing it means
 *              changing the recording - old trips know the old name.
 *   title      What it is called in the UI.
 *   did        Data identifier as it goes out: "22" + parameter,
 *              e.g. "22028C".
 *   address    Name of an entry from `addresses` further below.
 *   downhill   First payload byte, 0-based.
 *   len_total  Number of bytes, most significant first.
 *
 * Optional, with default:
 *
 *   unit       null means: no unit.                  (default null)
 *   put        Decimal places in the display.        (default 1)
 *   divider    Divide by this number.                (default 1)
 *   factor     Multiply by this.                     (default 1)
 *   offset     Add afterwards, e.g. -40 for °C.      (default 0)
 *   pre_offset Add before dividing.                  (default 0)
 *   sign       true: two's complement instead of unsigned. (default false)
 *   amount     true: take the absolute value.        (default false)
 *   mask       Bit mask on the raw value, e.g. 1.    (default: none)
 *   min, max   Plausibility bounds; outside them the value counts as
 *              unusable and becomes null.
 *   required   true: Without this value the whole round is discarded.
 *              Exactly one entry has this - the charge level. (default false)
 *   rarely     Query only every n-th round. 0 means every one. (default 0)
 *   also       Further values from the **same** response, as a list. Each
 *              entry in it knows the same fields except `did`,
 *              `address`, `required` and `rarely` - those come from the
 *              main value, because it is the same query.
 *
 * `pre_offset` and `offset` are two fields and not one, because both
 * orders really occur: the battery current is
 * `(raw - 150000) / 100`, the battery temperature `raw / 2 - 40`. Whoever
 * mixes them up gets numbers that look plausible and are wrong.
 */
window.joltReadings = {

  /* ===================================================================
   * Target addresses
   * ===================================================================
   *
   * An address consists of three parts, and the third is easily forgotten:
   * `cp` are the upper five bits of the 29-bit identifier, `sh` the lower
   * 24. For the battery management (0x17FC007B) cp = 17; for the climate
   * control unit (0x00000746) it is 00. Whoever leaves cp in place sends to
   * a completely different identifier and gets NO DATA - exactly the error
   * that held up the search for the charge level.
   *
   *   cp         upper five bits of the 29-bit identifier (ATCP)
   *   sh         send address (ATSH)
   *   cra        receive filter (ATCRA)
   *   fcsh       header for flow control on long responses
   *   trace_log  optional: ATSP number to switch to for this query. Only
   *              needed for 11-bit control units.
   */
  addresses: {
    BMS: { cp: "17", sh: "FC007B", cra: "17FE007B", fcsh: "17FC007B" },

    /* The climate control unit sits on an **11-bit identifier**, not on a
     * 29-bit one like everything else.
     *
     * 0x746 (request) and 0x7B0 (response) fit into eleven bits - that is
     * VW's classic diagnostic scheme. 0x17FC007B, where battery and vehicle
     * answer, fits only into 29. The handshake sets `ATSP7`, i.e.
     * exclusively 29 bit; a request to 0x00000746 therefore goes out as a
     * 29-bit frame, and the climate unit does not listen to that.
     *
     * That explains the finding from the third test drive: outside and
     * inside temperature were missing in **all** 77 rounds, while everything
     * on 0x17FC.... arrived 100 %. Address and conversion agree with the MEB
     * reference from spot2000 - it is the frame width.
     *
     * Unverified at the vehicle: it follows from the addresses, not from a
     * measurement. That is why these values sit behind `rarely` and drop out
     * for the session after a failure. */
    CLIMATE: { cp: "00", sh: "746", cra: "7B0", fcsh: "746", trace_log: "6" },

    /* The battery management on the 11-bit side. The same switching as with
     * the climate unit - it answers on 0x77A, not on 0x17FE007B. */
    AKKU11: { cp: "00", sh: "710", cra: "77A", fcsh: "710", trace_log: "6" },

    // Vehicle control unit: odometer and - the actual find - the power of the
    // auxiliary consumers as a ready-made number.
    VEHICLE: { cp: "17", sh: "FC0076", cra: "17FE0076", fcsh: "17FC0076" },

    // The DC/DC converter feeds the 12 V network from the high-voltage battery.
    DCDC: { cp: "17", sh: "FC00B9", cra: "17FE00B9", fcsh: "17FC00B9" },
  },

  /* ===================================================================
   * The readings
   * ===================================================================
   *
   * The data identifiers come from spot2000's MEB list and from our own
   * Android logger; confirmed at the vehicle so far is only `028C`. The
   * others are in without `required` - if one fails, the recording carries
   * on instead of failing over a side issue.
   *
   * The order is not arbitrary: reading goes from top to bottom, and a
   * change of target address costs a round trip over the serial link.
   * Values on the same address therefore belong together, and whatever
   * needs a protocol switch stands at the very bottom - if that goes
   * wrong, the mandatory values of this round have long been read.
   */
  vals: [

    { name: "soc_raw", title: "Rohwert SoC", unit: null, put: 0,
      did: "22028C", address: "BMS", required: true,
      downhill: 0, len_total: 1 },

    { name: "voltage_v", title: "Spannung", unit: "V", put: 1,
      did: "221E3B", address: "BMS",
      downhill: 0, len_total: 2, divider: 4 },

    /* Battery current.
     *
     * I had switched this to the WiCAN formula - five bytes from B5 and
     * reversed sign. That was wrong. Two independent sources agree on four
     * bytes from the first data byte and `(raw value - 150000)/100`: the MEB
     * list from spot2000 and codingABI's ESP32 logger, whose buffer index
     * demonstrably starts at the first byte after the acknowledgement - that
     * is, exactly at our b[0].
     *
     * Why the value did not arrive anyway is therefore **not** settled -
     * the formula was not it, at any rate. */
    { name: "current_a", title: "Strom", unit: "A", put: 1,
      did: "221E3D", address: "BMS",
      downhill: 0, len_total: 4, pre_offset: -150000, divider: 100 },

    /* The vehicle's energy counters - the most accurate consumption meter
     * there is here.
     *
     * They count over the lifetime what has gone into and out of the
     * battery. For consumption, not their reading counts but their
     * **difference** over a stretch of the trip - and that is more accurate
     * by orders of magnitude than anything else:
     *
     *     Charge level      step 0.44 pp  =  339 Wh
     *     Discharge counter step 1/8583   =  0.117 Wh
     *
     * Almost three thousand times finer. That turns a bar per minute from
     * noise into a measurement: what a minute at sixty km/h costs is about
     * 0.25 kWh - with the charge level 136 % error, here 0.05 %.
     *
     * Byte position and divisor come from codingABI's ESP32 logger
     * (`readAndSendHVTotalChargeDischarge`); spot2000 names the same
     * divisor. The response spans several frames - without flow control and
     * reassembly it did not arrive at all.
     *
     * **Signed.** The discharge counter comes as a negative number -
     * measured at the vehicle 0xF7141E0D. Read as an unsigned 32-bit number
     * that is 4.15 billion and thus 482 961 kWh; as a signed one -17 438.6,
     * and that is the right value. It was noticed by the cross-comparison of
     * the check page: 810 kWh/100 km lifetime consumption instead of the
     * expected 12 to 40.
     *
     * `also` fetches the charge counter from the same bytes, instead of
     * making the query a second time - a multi-frame response costs time. */
    { name: "discharge_kwh", title: "Entladen gesamt", unit: "kWh",
      put: 2, did: "221E32", address: "BMS",
      downhill: 12, len_total: 4, sign: true, divider: 8583.07, amount: true,
      also: [
        { name: "charged_kwh", title: "Geladen gesamt", unit: "kWh",
          put: 2, downhill: 8, len_total: 4, divider: 8583.07 },
      ] },

    { name: "charge_limit_a", title: "Ladegrenze", unit: "A", put: 0,
      did: "221E1B", address: "BMS",
      downhill: 0, len_total: 2, divider: 5 },

    { name: "mode", title: "Betriebsart", unit: null, put: 0,
      did: "227448", address: "BMS",
      downhill: 0, len_total: 1 },

    /* The current of the PTC heater. Times pack voltage this gives what the
     * heater alone draws - in winter the question behind the question,
     * because it is the only big consumer one can influence oneself. */
    { name: "ptc_current_a", title: "Heizstrom", unit: "A", put: 1,
      did: "221620", address: "BMS",
      downhill: 0, len_total: 1, divider: 4 },

    { name: "speed_kmh", title: "Tempo", unit: "km/h", put: 0,
      did: "22F40D", address: "BMS",
      downhill: 0, len_total: 1 },

    /* **Auxiliary consumers as a ready-made number.** Everything except the
     * drive - heating, A/C, control units, 12 V network - in kW, directly
     * from the control unit.
     *
     * Previously this was measured at standstill and extrapolated in
     * between: when the car stands, the pack power is that of the auxiliary
     * consumers. That was a usable approximation, but an approximation
     * nonetheless - it held only as long as nothing changed at the heater,
     * and not at all while driving. A measured value beats any
     * approximation. */
    { name: "aux_load_kw", title: "Nebenverbraucher", unit: "kW",
      put: 2, did: "220364", address: "VEHICLE",
      downhill: 0, len_total: 2, divider: 10 },

    /* The odometer - every round, and directly after the auxiliary
     * consumers.
     *
     * It used to stand at the end of the list with `rarely: 20`, so at the
     * 30-second cycle it was read only every ten minutes. On the first test
     * drives exactly **one** value arrived as a result - and from one value
     * no distance can be formed.
     *
     * Reading it more often costs nothing here except the query itself: it
     * sits on the same target address as the auxiliary consumers, which are
     * due every round anyway. The address change, because of which it was
     * made rare, thus does not occur in the first place.
     *
     * The resolution is one kilometre. For the distance share of a single
     * round that is too coarse, for the total distance of a trip exactly
     * right - and that is what matters. */
    { name: "odometer_km", title: "Kilometerstand", unit: "km", put: 0,
      did: "22295A", address: "VEHICLE",
      downhill: 0, len_total: 3 },

    { name: "dcdc_current_a", title: "DC/DC-Strom", unit: "A", put: 1,
      did: "22465B", address: "DCDC", rarely: 10,
      downhill: 0, len_total: 2, divider: 16 },

    /* The usable capacity of the battery, as the vehicle knows it.
     *
     * Interesting because it falls over the years - and because every
     * consumption calculated from the charge level stands and falls with it.
     * The value in the vehicle profile is a brochure figure; this one is
     * measured.
     *
     * **The conversion is not documented.** The MEB reference lists the
     * parameter with "equation missing"; known are only the unit (Wh), the
     * address and that the response has four payload bytes. What is assumed
     * is therefore the obvious thing - the 32-bit value in watt hours.
     * codingABI computes `buffer2unsignedLong() / 1310.77 / 1000`, i.e.
     * together divided by 1 310 770; WiCAN's `[B4:B5] * 50` is the same
     * formula, merely shortened to the upper two bytes. Four bytes are
     * finer.
     *
     * `min`/`max` hold the result against a plausibility bound: a car
     * battery has between 10 and 200 kWh. If the value falls out, the
     * assumption is wrong, and the row stays empty instead of inventing a
     * number.
     *
     * Read rarely, because it does not change during a trip. */
    { name: "battery_kwh", title: "Akkukapazität", unit: "kWh", put: 1,
      did: "222AB2", address: "AKKU11", rarely: 40,
      downhill: 0, len_total: 4, divider: 1310770, min: 10, max: 200 },

    /* The range that the car calculates itself. Interesting as a cross-check
     * against jolt's forecast - the same question, two answers.
     *
     * The first two data bytes - that is how codingABI reads it. WiCAN takes
     * one further on; which is right will be told by the first trip. The
     * bound catches the wrong case. */
    { name: "range_km", title: "Reichweite (Auto)", unit: "km",
      put: 0, did: "222AB6", address: "AKKU11", rarely: 10,
      downhill: 0, len_total: 2, min: 0, max: 999 },

    /* The battery temperature. It determines the charging power, and so far
     * `charging/curves.temperature_factor` takes the **outside** temperature
     * as a substitute - the comment there itself says that it underestimates
     * the cold of the battery after a night outdoors. Here is the right
     * value. */
    { name: "batterie_c", title: "Batterietemperatur", unit: "°C",
      put: 1, did: "222A0B", address: "BMS", rarely: 10,
      downhill: 0, len_total: 1, divider: 2, offset: -40 },

    /* The power of the A/C compressor - derived from two measurements, not
     * copied from a source.
     *
     * None of the three references (spot2000, WiCAN, codingABI) names a
     * conversion for `220800`; spot2000 lists it as "equation missing". The
     * response carries eleven bytes, and four of them lie in the plausible
     * watt range - guessing would have been especially tempting and
     * especially wrong.
     *
     * A difference measurement at the vehicle decided it, once with and once
     * without the compressor running:
     *
     *              b0    b1b2   b3b4   b5b6   b7
     *     off    0x10       0      0      0    0
     *     on     0x51    9408   9408   2618   14
     *     (third, partial load)  3648   3712   935    5
     *
     * From this:
     *   - `b0` bit 0 is on/off.
     *   - `b1b2` and `b3b4` run alike and much higher - target and actual
     *     speed. Read as watts, 9.4 kW would be too much for an A/C
     *     compressor.
     *   - `b5b6` is the **power in watts**: null when off, 935 at partial
     *     load, 2618 at full cooling. Exactly the profile.
     *   - `b7` is the same quantity, coarser - the ratio b5b6/b7 is exactly
     *     187 in both measurements.
     *
     * The speed fits: 3650 to 9408 revolutions is factor 2.58, 935 to
     * 2618 watts factor 2.80 - approximately proportional, so roughly equal
     * torque. The bound catches it in case that is different on another
     * vehicle after all. */
    { name: "compressor_w", title: "Klimakompressor", unit: "W",
      put: 0, did: "220800", address: "CLIMATE", rarely: 20,
      downhill: 5, len_total: 2, min: 0, max: 8000,
      also: [
        { name: "compressor_upm", title: "Kompressor-Drehzahl",
          unit: "/min", put: 0, downhill: 3, len_total: 2 },
        // Bit 0 of b0. `mask` cuts it out.
        { name: "compressor_at", title: "Kompressor an", unit: null,
          put: 0, downhill: 0, len_total: 1, mask: 1 },
      ] },

    /* Very last and only rarely: these two need a protocol switch (see
     * CLIMATE). If it goes wrong, the mandatory values of this round have
     * long been read.
     *
     * The outside temperature is the biggest single item of the cold and
     * used to go into the consumption model from a forecast. From the car it
     * is measured, from the route, at the right time. */
    { name: "outside_temp_c", title: "Aussentemperatur", unit: "°C",
      put: 1, did: "222609", address: "CLIMATE", rarely: 20,
      downhill: 0, len_total: 1, divider: 2, offset: -50 },

    { name: "inside_temp_c", title: "Innentemperatur", unit: "°C",
      put: 1, did: "222613", address: "CLIMATE", rarely: 20,
      downhill: 0, len_total: 2, divider: 5, offset: -40 },
  ],
};
