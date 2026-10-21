/* Platform profile Stellantis e-CMP: Opel Corsa-e / Corsa Electric, Peugeot
 * e-208 and the cars that share their electric drivetrain (e-2008, Mokka-e,
 * Citroën ë-C4, DS 3 E-Tense). The fields of a profile are explained in
 * `meb.js`.
 *
 * **Unverified at any vehicle of ours.** Everything below comes from three
 * sources that agree on addresses and requests:
 *
 *  - OBDb, VauxhallOpel-Corsa-e (signalsets/v3/default.json, CC BY-SA 4.0):
 *    addresses, charge level, voltage, energy, odometer, speed, cell
 *    voltages, state of health, each with its scale.
 *  - OVMS, vehicle_fiatedoblo (MIT): the same battery and vehicle control
 *    units on the same drivetrain. It adds the battery current, which OBDb
 *    lacks, and the temperatures as signed bytes.
 *  - The PID list from the GoingElectric forum, collected in OVMS issue 719:
 *    the same requests, and "SoC calibrated = value / 512".
 *
 * Unlike the MEB, these cars use 11-bit identifiers for everything, so there
 * is no `cp` and no protocol switch: ATSP6 for the whole session. The battery
 * management listens on 0x6B4 and answers on 0x694, the vehicle control unit
 * on 0x6A2 / 0x682. Both need flow control set up by hand (`fcsh`), which is
 * the `fcm1` in the OBDb file.
 *
 * Where the sources disagree it says so at the row. The bounds are tight on
 * purpose: if a scale is wrong, the value becomes null instead of a number
 * that looks plausible.
 */
(window.joltPlatforms = window.joltPlatforms || {}).ecmp = {
  id: "ecmp",
  title: "Stellantis e-CMP (Opel Corsa-e/Electric, Peugeot e-208, e-2008, Mokka-e)",

  handshake: [
    "ATZ", "ATE0", "ATL0", "ATS0", "ATH1",
    "ATSP6", "ATCAF1", "ATST FF",
    "ATSH6B4", "ATCRA694",
  ],
  protocol: "6",

  // F190 is the UDS identifier for the VIN; which e-CMP control unit
  // answers it is not confirmed. The standard 0902 follows.
  vin: [{ address: "BMS", did: "22F190" }, { address: "VCU", did: "22F190" }],

  // 16 bit in 1/512 %. The calibrated value is the one the dashboard shows
  // (OBDb names it so), so there is no separate `hmi` conversion.
  soc: { bms: (raw) => raw / 512 },

  addresses: {
    BMS: { sh: "6B4", cra: "694", fcsh: "6B4" },
    VCU: { sh: "6A2", cra: "682", fcsh: "6A2" },
  },

  vals: [

    /* The charge level, "calibrated". OVMS's E-Doblò module lists the same
     * request once as "SOC calibrated (/512 -> %)" and once, commented as
     * guessed, as motor torque. OBDb and the forum list both call it the
     * charge level of the Corsa-e, so that is what is used. `soc_bms_pct`
     * below is the uncalibrated one; if the first drive shows D410 moving
     * with the accelerator instead of with the charge, swap the two. */
    { name: "soc_raw", title: "Rohwert SoC", unit: null, put: 0,
      did: "22D410", address: "BMS", required: true,
      downhill: 0, len_total: 2 },

    { name: "voltage_v", title: "Spannung", unit: "V", put: 1,
      did: "22D815", address: "BMS",
      downhill: 0, len_total: 2, divider: 16, min: 150, max: 500 },

    /* Battery current: bytes 1-3 in 1/64 A, offset by 1200 A. From OVMS;
     * OBDb does not have it. Positive is discharging there, which is what
     * `powerKw` in live.js assumes. */
    { name: "current_a", title: "Strom", unit: "A", put: 1,
      did: "22D816", address: "BMS",
      downhill: 1, len_total: 3, divider: 64, offset: -1200, min: -600, max: 600 },

    { name: "odometer_km", title: "Kilometerstand", unit: "km", put: 0,
      did: "22D49C", address: "BMS", rarely: 2,
      downhill: 0, len_total: 4, divider: 1000 },

    { name: "soc_bms_pct", title: "Ladestand BMS (unkalibriert)", unit: "%", put: 1,
      did: "22D810", address: "BMS", rarely: 5,
      downhill: 0, len_total: 2, divider: 512, min: 0, max: 100 },

    { name: "energy_content_kwh", title: "Energieinhalt Akku", unit: "kWh", put: 1,
      did: "22D865", address: "BMS", rarely: 10,
      downhill: 0, len_total: 2, divider: 64, min: 0, max: 80 },

    { name: "cell_min_v", title: "Zelle niedrigste Spannung", unit: "V", put: 3,
      did: "22D86F", address: "BMS", rarely: 5,
      downhill: 0, len_total: 2, divider: 1000, min: 2, max: 4.5 },
    { name: "cell_max_v", title: "Zelle hoechste Spannung", unit: "V", put: 3,
      did: "22D870", address: "BMS", rarely: 5,
      downhill: 0, len_total: 2, divider: 1000, min: 2, max: 4.5 },

    /* State of health in 1/16 %. OBDb reads bytes 1-2, OVMS the three bytes
     * minus 65536 - the same number as long as byte 0 is 1. */
    { name: "soh_pct", title: "Akkugesundheit", unit: "%", put: 1,
      did: "22D860", address: "BMS", rarely: 40,
      downhill: 1, len_total: 2, divider: 16, min: 50, max: 110 },

    // Vehicle control unit: one address change per round for the speed, the
    // temperatures only every tenth round.
    { name: "speed_kmh", title: "Tempo", unit: "km/h", put: 0,
      did: "22D402", address: "VCU",
      downhill: 0, len_total: 2, divider: 10, min: 0, max: 250 },

    { name: "outside_temp_c", title: "Aussentemperatur", unit: "°C", put: 0,
      did: "22D434", address: "VCU", rarely: 10,
      downhill: 0, len_total: 1, sign: true, min: -50, max: 70 },

    { name: "batterie_c", title: "Batterietemperatur", unit: "°C", put: 0,
      did: "22D8EF", address: "VCU", rarely: 10,
      downhill: 0, len_total: 1, sign: true, min: -40, max: 80 },
  ],
};
