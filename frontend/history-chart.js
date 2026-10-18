/* The curve "plan and actual over the distance" of the live view.
 *
 * Drawing only: the trip, the measured history and the charging plan come in
 * as arguments (tools/check_history_chart.js records every canvas call).
 */
window.joltHistoryChart = (function () {
  "use strict";

  const K = window.jolt;

  /* Plan and actual over the distance, in one picture.
   *
   * This is jolt's thesis as a drawing: a plan computed at departure
   * is wrong after eighty kilometres - and two curves that
   * diverge say so at a glance, whereas a tile showing
   * "-6 pp" first has to be read and put in context. Above all the
   * curve says whether things are getting better or worse; a snapshot
   * fundamentally cannot.
   *
   * It is drawn without a plan too: in a recording there is no
   * planned curve, but the measured one is then all the more what you
   * want to see.
   */
  function draw(canvas, trip, history, plan) {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const profile = (trip && trip.profile) || [];
    const reserve = trip ? trip.vehicle.reserve_soc : 10;

    // The scale depends on what there is: with a plan on the whole
    // route, without a plan on what has been driven so far.
    /* Without a plan there is no `km_on_route` - it comes from the projection
     * onto the route, and a recording has none. It is therefore null
     * for **every** point, and the curve collapsed into a vertical
     * line at the left edge. Of all places exactly where it is the
     * only thing there is to see.
     *
     * Measuring then happens along the driven track: for point i the
     * sum of the distances up to there. That is the distance really
     * covered, and therefore the right axis. */
    /* There used to be a loop here that recomputed the distance at draw time
     * from the track - `spacingKm(spur[i-1], spur[i])` for every history
     * point i. That presupposed that `spur[i]` belongs to `verlauf[i]`,
     * and it does not: the track grows with every new position, the
     * history with every new state of charge. When recording with a dongle
     * the history was many times shorter, and the curve moved
     * to the left edge as a result - exactly the error this loop
     * was meant to fix. Now each history point carries its own distance. */
    const ownKm = !profile.length;
    const distanceFrom = (v) => ownKm ? (v.driven_km || 0) : v.km;
    const maxKm = profile.length
      ? (profile[profile.length - 1].km || 1)
      : Math.max(1, ...history.map(distanceFrom));
    const left = 4, right = width - 4, upper = 8, bottom = height - 16;
    const x = (km) => left + (km / maxKm) * (right - left);
    const y = (soc) => bottom - (Math.max(0, Math.min(100, soc)) / 100)
      * (bottom - upper);

    // Elevation profile in the background. It explains the kinks in both curves -
    // without this explanation they look like measurement errors.
    if (profile.length > 1) {
      let maxElevation = 1;
      for (const p of profile) maxElevation = Math.max(maxElevation, p.elevation || 0);
      ctx.beginPath();
      ctx.moveTo(x(0), bottom);
      for (const p of profile) {
        ctx.lineTo(x(p.km), bottom - ((p.elevation || 0) / maxElevation) * (bottom - upper) * 0.3);
      }
      ctx.lineTo(x(maxKm), bottom);
      ctx.closePath();
      ctx.fillStyle = "rgba(138,151,165,.12)";
      ctx.fill();
    }

    // The reserve as a line, not a number: you see at once where the
    // measured curve is heading towards it.
    ctx.beginPath();
    ctx.setLineDash([4, 4]);
    ctx.moveTo(left, y(reserve));
    ctx.lineTo(right, y(reserve));
    ctx.strokeStyle = "rgba(226,89,106,.6)";
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(226,89,106,.75)";
    ctx.font = "10px system-ui, sans-serif";
    ctx.fillText("Reserve", left + 2, y(reserve) - 3);

    // Planned curve: muted, it is the reference and not the message.
    if (profile.length > 1) {
      ctx.beginPath();
      profile.forEach((p, i) => {
        const px = x(p.km), py = y(p.soc);
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      });
      ctx.strokeStyle = "rgba(138,151,165,.55)";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }

    // The charging stops as markers - they explain the jumps that are
    // about to come, and show how far away the next one still is.
    for (const stop of (plan && plan.stops) || []) {
      const px = x(stop.km_on_route);
      ctx.beginPath();
      ctx.moveTo(px, upper);
      ctx.lineTo(px, bottom);
      ctx.strokeStyle = "rgba(255,201,60,.35)";
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    // The measured curve. It is the message, so make it strong.
    if (history.length > 1) {
      ctx.beginPath();
      history.forEach((v, i) => {
        const px = x(distanceFrom(v)), py = y(v.soc);
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      });
      ctx.strokeStyle = "#ffc93c";
      ctx.lineWidth = 2.5;
      ctx.lineJoin = "round";
      ctx.stroke();
    }

    // Where the car is right now. A computed state of charge gets a
    // hollow dot - you should be able to see that it is not measured.
    const latest = history[history.length - 1];
    if (latest) {
      ctx.beginPath();
      ctx.arc(x(distanceFrom(latest)), y(latest.soc), 4.5, 0, Math.PI * 2);
      if (latest.reported) { ctx.fillStyle = "#ffc93c"; ctx.fill(); }
      else { ctx.strokeStyle = "#ffc93c"; ctx.lineWidth = 2; ctx.stroke(); }
    }

    ctx.fillStyle = "rgba(138,151,165,.8)";
    ctx.fillText("0", left, height - 4);
    const label = K.num(maxKm) + " km";
    ctx.fillText(label, right - ctx.measureText(label).width,
                   height - 4);
  }

  return { draw };
})();
