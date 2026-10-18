/* Is the car being driven, standing, or parked? And may the dongle ask it?
 *
 * A small state machine, kept apart from the live view: it takes in the phone's
 * position fixes and the 12 V voltage and decides. Everything that has to
 * *happen* on a change (disconnect the dongle, fetch it back, tell the driver)
 * is handed in as `onPark` and `onDrive`, so this file touches neither the
 * dongle nor the page.
 *
 * States: "faehrt" (driving), "steht" (standing), "geparkt" (parked).
 */
window.joltDriveState = (function () {
  "use strict";

  /* Whether the car is locked cannot be found out **without** asking
   * it - and the asking itself sets off the alarm of a locked car.
   * There is no signal that the dongle or the car sends out on its own.
   * So it is worked out the other way round: reading happens
   * only if the phone knows something that cannot be the case with a locked
   * car - it is moving at driving speed.
   *
   *   fährt    from 15 km/h (two measurements in a row). On foot you
   *            do not get there, and whoever is that fast is in a car.
   *            Reading happens, and if the dongle is gone it is fetched back.
   *   steht    under 3 km/h for ten seconds. Nothing is asked any more;
   *            the connection stays, an idling dongle sends nothing onto
   *            the bus. Traffic lights, jams and charging pumps thus cost no rebuild.
   *   geparkt  the phone is on foot more than 25 m from the stopping place, or
   *            the car has been standing for three minutes: disconnect, do not
   *            reconnect. Only a drive brings it back.
   *
   * The counters in the car run over the lifetime: what was used during a pause
   * is contained in the difference of the next measurement. A gap while
   * standing therefore costs no consumption, only individual values.
   *
   * Not detectable: whoever locks before ten seconds have passed
   * can still trigger a query in the first moment. Only
   * a signal from the car itself would be certain. */
  const FAST_KMH = 15;
  const STAND_KMH = 3;
  const STANDING_TIME_MS = 10000;
  const PARK_TIME_MS = 180000;
  const PATH_M = 25;
  // If someone tapped "Dongle verbinden" ("connect dongle") by hand, they want to read while standing.
  const MANUAL_MS = 600000;

  /* The car is off - you can tell from the 12 V voltage, without asking.
   *
   * `ATRV` is measured by the ELM chip itself, nothing goes onto the CAN bus (see
   * `obd-core.js: spannung`). As long as the car is on or charging, the
   * DC/DC converter keeps the voltage up; when it goes off it falls within
   * seconds. That happens **before** locking - you switch off,
   * get out and lock - and it is thus the signal that the
   * phone's movement cannot provide.
   *
   * There is deliberately no fixed threshold: how high the voltage is
   * with the converter running differs from car to car. The comparison
   * is against the average of the last drive, and only while standing - a
   * drop during the drive is not a parking position. Whoever has never driven
   * has no basis; then the rules about standing and distance apply.
   *
   * If the car charges locked, the voltage stays up, and the
   * rules stay as they are. That is fine too: the query is then precisely
   * *not* the problem, as long as nothing is being asked any more. */
  const VOLTAGE_CASE_V = 0.7;
  const VOLTAGE_SEQUENCE = 2;
  const VOLTAGE_BASIS = 5;
  // A voltage older than this is not recorded along with a point.
  const VOLTAGE_FRESH_MS = 15000;

  function create(hooks) {
    let autoMode = true;
    try { autoMode = localStorage.getItem("jolt-dongle-auto") !== "0"; }
    catch (e) { /* without storage the default applies */ }

    let driveState = "steht";
    let standSince = null;
    let standCity = null;
    let fastSequence = 0;
    let asOfSeen = true;
    let manualUntil = 0;
    let latestPosition = null;
    let latestSpeed = null;
    let voltageBasis = [];
    let voltageSequence = 0;
    let latestVoltage = null;
    let latestVoltageTime = 0;

    function reset(state) {
      driveState = state;
      standSince = null; standCity = null; fastSequence = 0;
      asOfSeen = true; manualUntil = 0; latestPosition = null;
      voltageBasis = []; voltageSequence = 0;
    }

    /* Whether the phone's movement allows asking the car. The dongle being
     * paused or the settings listening in are the caller's business. */
    function motionAllows() {
      if (!autoMode) return true;
      return driveState === "faehrt" || Date.now() < manualUntil;
    }

    function distanceM(a, b) {
      const R = 6371000, rad = Math.PI / 180;
      const dLat = (b.lat - a.lat) * rad, dLon = (b.lon - a.lon) * rad;
      const h = Math.sin(dLat / 2) ** 2
        + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
      return 2 * R * Math.asin(Math.sqrt(h));
    }

    /* The speed from the fix - or from two fixes if the device
     * provides none (regularly the case in the browser on iOS). */
    function speedKmh(coords, place, timeMs) {
      const prior = latestPosition;
      latestPosition = { lat: place.lat, lon: place.lon, timestamp: timeMs };
      if (typeof coords.speed === "number" && coords.speed >= 0) {
        return coords.speed * 3.6;
      }
      if (!prior) return null;
      const dt = (timeMs - prior.timestamp) / 1000;
      if (dt < 1 || dt > 30) return null;
      return distanceM(prior, place) / dt * 3.6;
    }

    function park(reason) {
      if (driveState === "geparkt") return;
      // Set the state first, then let the caller disconnect: the connection
      // drop is handled by asking `readAllowed()`.
      driveState = "geparkt";
      standSince = null; standCity = null;
      asOfSeen = false;
      voltageBasis = []; voltageSequence = 0;
      hooks.onPark(reason);
    }

    function driveBegins() {
      // After a failed attempt without a dongle only again once the vehicle has stopped -
      // otherwise a bicycle ride would try to connect all the way.
      if (driveState === "geparkt" && !asOfSeen) return;
      const wasParked = driveState === "geparkt";
      driveState = "faehrt";
      standSince = null; standCity = null;
      hooks.onDrive(wasParked);
    }

    function examine(coords, timeMs) {
      if (!autoMode || !hooks.sessionActive()) return;
      const now = Date.now();
      const place = { lat: coords.latitude, lon: coords.longitude };
      const v = speedKmh(coords, place, timeMs || now);
      if (v === null) return;
      latestSpeed = v;

      if (v >= FAST_KMH) {
        standSince = null; standCity = null;
        fastSequence++;
        if (fastSequence >= 2 && driveState !== "faehrt") driveBegins();
        return;
      }
      fastSequence = 0;
      if (v < STAND_KMH) asOfSeen = true;
      if (now < manualUntil || driveState === "geparkt") return;

      if (v < STAND_KMH) {
        if (standSince === null) { standSince = now; standCity = place; }
        const as_of = now - standSince;
        if (as_of >= PARK_TIME_MS) park("Das Auto steht seit drei Minuten");
        else if (as_of >= STANDING_TIME_MS && driveState === "faehrt") driveState = "steht";
      } else if (standCity && distanceM(standCity, place) > PATH_M) {
        // Slow and far from the stopping place: you got out and are walking.
        park("Du bist vom Auto weggegangen");
      }
    }

    function avg(values) {
      const sorted = [...values].sort((a, b) => a - b);
      const m = Math.floor(sorted.length / 2);
      return sorted.length % 2 ? sorted[m] : (sorted[m - 1] + sorted[m]) / 2;
    }

    /* One 12 V reading. Learns the voltage while driving and parks when it
     * drops clearly below that for two readings in a row. */
    function voltageSample(v) {
      latestVoltage = v;
      latestVoltageTime = Date.now();
      if (latestSpeed !== null && latestSpeed >= FAST_KMH) {
        voltageBasis.push(v);
        if (voltageBasis.length > 40) voltageBasis.shift();
        voltageSequence = 0;
        return;
      }
      if (Date.now() < manualUntil || voltageBasis.length < VOLTAGE_BASIS) return;
      if (v < avg(voltageBasis) - VOLTAGE_CASE_V) {
        if (++voltageSequence >= VOLTAGE_SEQUENCE) {
          park("Die 12-V-Spannung ist gefallen, das Auto ist aus");
        }
      } else {
        voltageSequence = 0;
      }
    }

    return {
      state: () => driveState,
      reset,
      motionAllows,
      examine,
      voltageSample,
      park,
      driving: () => latestSpeed !== null && latestSpeed >= FAST_KMH,
      auto: () => autoMode,
      setAuto: (on) => { autoMode = !!on; },
      // The checkbox: remember the choice, and start over as "standing".
      chooseAuto(on) {
        autoMode = !!on;
        try { localStorage.setItem("jolt-dongle-auto", autoMode ? "1" : "0"); }
        catch (e) { /* this session only */ }
        if (autoMode) driveState = "steht";
      },
      // Someone tapped "connect dongle": reading while standing is wanted.
      manualConnect() {
        manualUntil = Date.now() + MANUAL_MS;
        if (driveState === "geparkt") driveState = "steht";
      },
      // No dongle in range after all the attempts: wait for the next stop.
      giveUp() { driveState = "geparkt"; asOfSeen = false; },
      // The latest 12 V reading while it is still fresh, else null.
      freshVoltage() {
        return latestVoltage !== null && Date.now() - latestVoltageTime < VOLTAGE_FRESH_MS
          ? latestVoltage : null;
      },
    };
  }

  return { create };
})();
