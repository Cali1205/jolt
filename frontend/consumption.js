/* The bar chart "consumption per time section" of the live view.
 *
 * Pure of the live state: the measurement points and the vehicle come in as
 * arguments, so the maths (`sections`) can be checked without a browser
 * (tools/check_consumption.js) and the drawing (`draw`) needs only a canvas.
 */
window.joltConsumption = (function () {
  "use strict";

  const K = window.jolt;

  /* **Where the energy comes from, and what that means for the bar width.**
   *
   * First draft: sum up power (voltage times current) over time.
   * Wrong - the current changes every second,
   * reporting happens every twelve seconds. Five samples per minute are
   * not an integral but a survey; on the country road 25 % error,
   * in the city 59 %.
   *
   * Second draft: from the **state of charge**. Its quantisation error is
   * absolutely bounded (one step, 0.44 pp = 339 Wh), so relatively the
   * smaller the longer the section - from five minutes on everywhere under
   * 3 %. But only from five minutes on.
   *
   * Now: the vehicle's **energy counters**. Their difference is the
   * energy used, with 0.117 Wh resolution - almost three thousand times
   * finer than the state of charge. With that a bar per minute is no longer an
   * estimate but a measurement (0.05 % instead of 136 %).
   *
   * The bar width therefore follows the source, and not the wish:
   * one minute with the counter, five without.
   */
  const SECTION_WITH_COUNTER_S = 60;
  const SECTION_FROM_SOC_S = 300;
  /* On a long trip the minute becomes too fine.
   *
   * A bar per minute is just right over half an hour - over
   * six hours it would be 360 bars on roughly 340 pixels, i.e. 0.9 pixels
   * per bar. That is no longer a chart but a texture.
   *
   * So the section width grows with the trip, but only to round
   * values: two minutes you still read as two minutes, 87 seconds you do not.
   * Accuracy does not suffer - with the counters even the
   * minute is far above the resolution limit, wider only gets better. */
  const WIDTHS_MIN = [1, 2, 5, 10, 15, 30, 60];
  const BAR_AT_MOST = 60;

  function widthChoose(duration_ms, min_s) {
    for (const min of WIDTHS_MIN) {
      if (min * 60 < min_s) continue;
      if (duration_ms / (min * 60000) <= BAR_AT_MOST) return min * 60000;
    }
    return WIDTHS_MIN[WIDTHS_MIN.length - 1] * 60000;
  }
  // Below this distance kWh/100 km makes no sense - the car was standing.
  const BAR_MIN_KM = 0.3;

  /* **The distance per bar comes from the GPS, the energy from the counters.**
   *
   * That sounds like a step backwards - for the **total distance** of a
   * recording the odometer is precisely the better source, because
   * it neither cuts off bends nor knows dead spots. Over a single
   * minute this reverses: it resolves in whole kilometres, and one
   * minute at seventy km/h is 1.2 km. What gets measured is 1 or 2 -
   * forty percent error on the denominator. The GPS track, with a report
   * every twelve seconds, cuts off only a few percent.
   *
   * Noticed on a real trip: the kilometre column per minute stood
   * constantly at 0 or 1, and the bars fluctuated accordingly.
   *
   * The energy stays with the counters - there the resolution is 0.117 Wh
   * and thus no issue. Each quantity from the source that knows
   * it best. */
  function sections(track, car) {
    const points = track.filter((p) => typeof p.gps === "number");
    if (points.length < 2) return null;
    const withCounter = points.every((p) => typeof p.net === "number");
    const battery = car.capacity_kwh || car.battery_net_kwh;
    if (!withCounter && !battery) return null;

    const onset = points[0].timestamp;
    const bucketWidth = widthChoose(
      points[points.length - 1].timestamp - onset,
      withCounter ? SECTION_WITH_COUNTER_S : SECTION_FROM_SOC_S);
    const buckets = new Map();
    for (const p of points) {
      const n = Math.floor((p.timestamp - onset) / bucketWidth);
      if (!buckets.has(n)) buckets.set(n, []);
      buckets.get(n).push(p);
    }

    const bar = [];
    for (const [n, group] of [...buckets.entries()].sort((a, b) => a[0] - b[0])) {
      if (group.length < 2) continue;
      const first = group[0], final = group[group.length - 1];
      const km = final.gps - first.gps;
      if (km < BAR_MIN_KM) continue;
      const kwh = withCounter ? (final.net - first.net)
        : ((first.soc !== null && final.soc !== null)
           ? (first.soc - final.soc) / 100 * battery : null);
      if (kwh === null || !Number.isFinite(kwh)) continue;
      bar.push({ n, kwh100: kwh / km * 100, km });
    }
    return bar.length ? { bar, bucketWidth, withCounter } : null;
  }

  function draw(canvas, foot, track, car) {
    if (!canvas || !foot) return;
    const sections_ = sections(track, car);
    if (!sections_) { canvas.hidden = true; foot.hidden = true; return; }
    canvas.hidden = false; foot.hidden = false;

    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const vals = sections_.bar.map((b) => b.kwh100);
    // The scale generous upwards, so that an outlier does not flatten the other
    // bars, and with a zero line: recuperation goes
    // below zero, and that is exactly what you should see.
    const upper = Math.max(40, ...vals) * 1.1;
    const bottom = Math.min(0, ...vals) * 1.1;
    const span = upper - bottom || 1;
    const edge = 6, footHeight = 16;
    const area = height - footHeight - edge;
    const y = (v) => edge + (upper - v) / span * area;

    /* Labelled axis. Without it a bar chart is a shape without a
     * message - you see that one minute was more expensive than another, but
     * not whether it is twenty or forty kWh/100 km. And that is
     * the number you compare with your own feeling.
     *
     * Labels go on the left, into the area: a separate column
     * for it would be too expensive on the phone. Three lines suffice - zero, a
     * round value in between and the maximum. */
    const axis = 30;
    const split = [0];
    const step = upper > 60 ? 25 : (upper > 25 ? 10 : 5);
    for (let w = step; w < upper; w += step) split.push(w);
    for (let w = -step; w > bottom; w -= step) split.push(w);

    ctx.font = "10px system-ui, sans-serif";
    ctx.textBaseline = "middle";
    for (const w of split) {
      const yy = y(w);
      ctx.strokeStyle = w === 0 ? "#3a4652" : "#222c36";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(axis, yy); ctx.lineTo(width - edge, yy);
      ctx.stroke();
      ctx.fillStyle = "#8a97a5";
      ctx.textAlign = "right";
      ctx.fillText(String(w), axis - 4, yy);
    }
    // The unit once at the top left, not on every tick.
    ctx.fillStyle = "#8a97a5";
    ctx.textAlign = "left";
    ctx.fillText("kWh/100", axis + 3, edge + 4);

    const field = width - edge - axis;
    const b = Math.max(2, field / sections_.bar.length - 2);
    sections_.bar.forEach((bar, i) => {
      const x = axis + i * (field / sections_.bar.length);
      const high = y(bar.kwh100) - y(0);
      // Colour by height: what lies clearly above the average stands out.
      ctx.fillStyle = bar.kwh100 < 0 ? "#57c98a"
        : (bar.kwh100 > 35 ? "#e8804f" : "#ffc93c");
      ctx.fillRect(x, high < 0 ? y(bar.kwh100) : y(0),
                     b, Math.max(1, Math.abs(high)));
    });

    const average = vals.reduce((a, v) => a + v, 0) / vals.length;
    foot.children[0].textContent =
      `Verbrauch je ${sections_.bucketWidth / 60000} min`
      + (sections_.withCounter ? "" : " (aus dem Ladestand)");
    foot.children[1].textContent = `Ø ${K.num(average, 1)} kWh/100`;
  }

  return { sections, draw };
})();
