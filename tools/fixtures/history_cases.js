// Synthetic inputs for check_history_chart.js: trips with and without a planned
// profile, with and without charging stops, a measured and a computed latest point.
// The expected canvas calls in history_baseline.json were recorded from
// drawHistory as it stood in live.js before it moved into history-chart.js.
function profile(n, km, startSoc, endSoc) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const f = i / (n - 1);
    out.push({ km: km * f, soc: startSoc + (endSoc - startSoc) * f,
               elevation: 300 + 120 * Math.sin(f * 6) });
  }
  return out;
}

function measured(n, km, startSoc, perKm, withDriven, lastReported) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const f = i / Math.max(1, n - 1);
    const p = { km: km * f, soc: startSoc - perKm * km * f, reported: i % 3 === 0 };
    if (withDriven) p.driven_km = km * f;
    out.push(p);
  }
  if (out.length) out[out.length - 1].reported = lastReported;
  return out;
}

const trip = (prof) => ({ profile: prof, vehicle: { reserve_soc: 12 } });
const plan = { stops: [{ km_on_route: 120 }, { km_on_route: 260 }] };

const cases = {
  planned_with_stops: { trip: trip(profile(40, 300, 90, 15)), history: measured(30, 140, 90, 0.3, false, true), plan },
  planned_no_plan_stops: { trip: trip(profile(25, 80, 70, 40)), history: measured(10, 30, 70, 0.3, false, false), plan: null },
  recording_own_km: { trip: trip([]), history: measured(50, 64, 80, 0.4, true, true), plan: null },
  recording_one_point: { trip: trip([]), history: measured(1, 0, 80, 0.4, true, false), plan: null },
  empty_history: { trip: trip(profile(10, 50, 60, 30)), history: [], plan },
  no_trip: { trip: null, history: measured(12, 20, 55, 0.5, true, true), plan: null },
};

module.exports = { cases };
