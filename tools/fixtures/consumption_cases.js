// Synthetic trips and a canvas stand-in for check_consumption.js.
// The expected values in consumption_baseline.json were recorded from the code as it
// stood in live.js before it moved into consumption.js.
function track(n, o) {
  const pts = []; let t = 1700000000000, gps = 0, net = 10, soc = 80;
  let seed = o.seed || 1; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  for (let i = 0; i < n; i++) {
    t += o.step || 12000; gps += (o.kmh || 60) / 3600 * (o.step || 12000) / 1000 * (0.5 + rnd());
    net += (o.kwhkm || 0.18) * (o.kmh || 60) / 3600 * (o.step || 12000) / 1000 * (rnd() * 1.5 - 0.2);
    soc -= 0.01;
    const p = { timestamp: t, gps, soc: o.noSoc ? null : Math.round(soc * 2.5) / 2.5 };
    if (!o.noCounter) p.net = net;
    pts.push(p);
  }
  return pts;
}
const cases = {
  counter_short: { pts: track(40, {}), vehicle: { capacity_kwh: 77 } },
  counter_long: { pts: track(2600, { seed: 7 }), vehicle: { capacity_kwh: 77 } },
  soc_only: { pts: track(300, { noCounter: true, seed: 3 }), vehicle: { capacity_kwh: 77 } },
  soc_no_battery: { pts: track(300, { noCounter: true }), vehicle: {} },
  too_few: { pts: track(1, {}), vehicle: { capacity_kwh: 77 } },
  standing: { pts: track(60, { kmh: 0.5 }), vehicle: { capacity_kwh: 77 } },
  recuperation: { pts: track(80, { kwhkm: -0.1, seed: 5 }), vehicle: { capacity_kwh: 77 } },
};
function canvasStub(log) {
  const ctx = new Proxy({}, { get: (z, n) => (n === "measureText" ? () => ({ width: 0 }) : (...args) => log.push([n, ...args.map((x) => typeof x === "number" ? Math.round(x * 100) / 100 : x)])),
    set: (z, n, v) => { log.push(["set:" + n, v]); return true; } });
  return { hidden: false, clientWidth: 340, clientHeight: 120, width: 0, height: 0, getContext: () => ctx };
}
module.exports = { cases, canvasStub };
