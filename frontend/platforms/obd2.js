/* Platform profile standard OBD2 (SAE J1979): the emissions diagnostics every
 * car with an OBD2 socket speaks, over 11-bit CAN.
 *
 * For vehicles without a profile of their own. It offers what the legislator
 * prescribes and nothing more - no energy counters, no cell voltages, no
 * climate data. The fields of a profile are explained in `meb.js`.
 *
 * **Unverified at any vehicle.** The formulas are the ones of the standard,
 * not something measured. Two limits follow from how the standard works:
 *
 *  - The charge level is PID 5B (hybrid/EV battery pack remaining life). It
 *    is optional in the standard and many electric cars do not report it. A
 *    car that does not answer 015B cannot be recorded with this profile,
 *    because `soc_raw` is the one mandatory row.
 *  - What the standard gives is the battery's own percentage. There is no
 *    dashboard conversion in it, so `hmi` is left out and equals `bms`.
 *
 * The request is a functional broadcast on 0x7DF, the answer comes from
 * whichever control unit owns the PID (0x7E8 and up). That is why there is no
 * `cra`: the bare `ATCRA` in the handshake lets any of them through. And
 * there is no flow control to set up (`fcsh`) - on a standard address the
 * ELM327 answers a multi-frame response on its own.
 */
(window.joltPlatforms = window.joltPlatforms || {}).obd2 = {
  id: "obd2",
  title: "Standard-OBD2 (SAE J1979, andere Hersteller)",

  handshake: [
    "ATZ", "ATE0", "ATL0", "ATS0", "ATH1",
    "ATSP6", "ATCAF1", "ATST FF",
    "ATSH7DF", "ATCRA",
  ],
  protocol: "6",

  // PID 5B is one byte, 0 to 255 for 0 to 100 %.
  soc: { bms: (raw) => raw * 100 / 255 },

  addresses: {
    OBD: { sh: "7DF" },
  },

  vals: [

    { name: "soc_raw", title: "Rohwert SoC", unit: null, put: 0,
      did: "015B", address: "OBD", required: true,
      downhill: 0, len_total: 1 },

    { name: "speed_kmh", title: "Geschwindigkeit", unit: "km/h", put: 0,
      did: "010D", address: "OBD",
      downhill: 0, len_total: 1 },

    { name: "outside_temp_c", title: "Aussentemperatur", unit: "°C", put: 0,
      did: "0146", address: "OBD", rarely: 20,
      downhill: 0, len_total: 1, offset: -40 },

    // Four bytes in tenths of a kilometre. PID A6 is newer than the basic set
    // and not every car has it.
    { name: "odometer_km", title: "Kilometerstand", unit: "km", put: 1,
      did: "01A6", address: "OBD", rarely: 20,
      downhill: 0, len_total: 4, divider: 10 },

    // Control module voltage, which in a car is the 12 V network. The bounds
    // reject a unit that reports something else under this PID.
    { name: "aux12_v", title: "12-V-Bordnetz", unit: "V", put: 1,
      did: "0142", address: "OBD", rarely: 5,
      downhill: 0, len_total: 2, divider: 1000, min: 8, max: 17 },
  ],
};
