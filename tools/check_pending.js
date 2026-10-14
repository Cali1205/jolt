#!/usr/bin/env node
// Checks the recording that starts (and ends) without network -
// frontend/pending.js. No browser, no network: the API is a stand-in that
// can be switched off or made to answer with an error.
//
//     node tools/check_pending.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "pending.js"), "utf8");

let failure = 0;
function verify(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) failure++;
}

function world() {
  const data = {};
  const calls = [];
  const state = { grid: true, status: null, badPoint: null, startAnswer: 41 };
  const storage = {
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => { data[k] = String(v); },
    removeItem: (k) => { delete data[k]; },
  };
  const window = { localStorage: storage, addEventListener() {} };
  const api = async (url, opt) => {
    calls.push({ url, body: opt && opt.body });
    if (!state.grid) throw new Error("Server nicht erreichbar.");
    if (state.status) {
      const e = new Error("HTTP " + state.status); e.status = state.status; throw e;
    }
    if (url === "/api/live/recording") return { session_id: state.startAnswer, trip_id: 5 };
    if (state.badPoint && opt.body && opt.body.points
        && opt.body.points.some((p) => p.timestamp === state.badPoint)) {
      const e = new Error("HTTP 422"); e.status = 422; throw e;
    }
    return {};
  };
  const context = { window, Date, JSON, Promise, Object, setTimeout, clearTimeout,
                    setInterval, Math };
  vm.createContext(context);
  vm.runInContext(source, context);
  return { P: window.joltPending, data, calls, state, api };
}

(async () => {
  console.log("Start");
  let w = world();
  let r = await w.P.start(w.api, { vehicle_id: 1, lat: 1, lon: 2, soc: 50, name: "x" });
  verify(r.pending === false && r.session_id === 41,
         "mit Netz kommt die echte Sitzung zurück, nichts wird lokal gemerkt",
         JSON.stringify(r));
  verify(!w.data["jolt-aufz-offen"] && w.calls[0].body.started_at,
         "der Start trägt seine Startzeit mit (der Server erkennt eine Wiederholung daran)",
         JSON.stringify(w.calls[0]));

  w = world(); w.state.grid = false;
  r = await w.P.start(w.api, { vehicle_id: 1, lat: 1, lon: 2, soc: 50, name: "x" });
  const rec = JSON.parse(w.data["jolt-aufz-offen"] || "null");
  verify(r.pending === true && w.P.isLocal(r.session_id) && rec && rec.id === r.session_id,
         "ohne Netz startet die Aufzeichnung lokal und merkt sich den Start",
         JSON.stringify(r));
  verify(rec.body.vehicle_id === 1 && rec.body.started_at,
         "mit Fahrzeug und Startzeit", JSON.stringify(rec));

  w = world(); w.state.status = 404;
  let thrown = null;
  try { await w.P.start(w.api, { vehicle_id: 9, lat: 1, lon: 2 }); }
  catch (e) { thrown = e; }
  verify(thrown && thrown.status === 404 && !w.data["jolt-aufz-offen"],
         "eine Absage des Servers ist kein 'offline': der Fehler geht an den Aufrufer",
         String(thrown));

  console.log("\nAnlegen auf dem Server");
  w = world(); w.state.grid = false;
  r = await w.P.start(w.api, { vehicle_id: 1, lat: 1, lon: 2, soc: 50 });
  verify((await w.P.materialize(w.api)) === null && w.data["jolt-aufz-offen"],
         "ohne Netz bleibt die lokale Aufzeichnung stehen");
  w.state.grid = true;
  const made = await w.P.materialize(w.api);
  const sent = w.calls[w.calls.length - 1];
  verify(made && made.session_id === 41 && sent.body.started_at === JSON.parse(w.data["jolt-aufz-offen"]).body.started_at,
         "mit Netz wird sie mit der echten Startzeit angelegt", JSON.stringify(made));
  const before = w.calls.length;
  verify((await w.P.materialize(w.api)).session_id === 41 && w.calls.length === before,
         "ein zweiter Aufruf legt nichts doppelt an");

  w = world(); w.state.grid = false;
  await w.P.start(w.api, { vehicle_id: 1, lat: 1, lon: 2 });
  w.state.grid = true; w.state.status = 422;
  thrown = null;
  try { await w.P.materialize(w.api); } catch (e) { thrown = e; }
  verify(thrown && !w.data["jolt-aufz-offen"],
         "lehnt der Server den Start ab (zu alt), wird er nicht ewig wiederholt");

  console.log("\nPunkte umbenennen");
  w = world();
  w.data["jolt-puffer-local-1"] = "[1]";
  w.P.renameBuffer("local-1", 77);
  verify(w.data["jolt-puffer-77"] === "[1]" && !("jolt-puffer-local-1" in w.data),
         "die gemerkten Punkte wandern auf die echte Sitzung");

  console.log("\nBeenden ohne Netz");
  w = world(); w.state.grid = false;
  r = await w.P.start(w.api, { vehicle_id: 1, lat: 1, lon: 2, soc: 50 });
  const pts = Array.from({ length: 250 }, (_, i) =>
    ({ lat: 1, lon: 2, soc: 50, timestamp: "2026-10-14T10:" + String(i).padStart(2, "0") + ":00Z" }));
  w.data["jolt-puffer-" + r.session_id] = JSON.stringify(pts);
  verify((await w.P.syncEnd(w.api)) === false,
         "ohne gemerktes Ende tut es nichts");
  w.P.requestEnd();
  verify((await w.P.syncEnd(w.api)) === false && w.data["jolt-aufz-offen"],
         "ohne Netz bleibt alles liegen");
  w.state.grid = true; w.calls.length = 0;
  const done = await w.P.syncEnd(w.api);
  const posts = w.calls.filter((c) => /points$/.test(c.url));
  verify(done === true && w.calls[0].url === "/api/live/recording"
         && posts.length === 3 && posts.map((c) => c.body.points.length).join() === "100,100,50"
         && w.calls[w.calls.length - 1].url === "/api/live/41/end",
         "mit Netz: Sitzung anlegen, Punkte in Päckchen, dann beenden",
         JSON.stringify(w.calls.map((c) => c.url)));
  verify(!w.data["jolt-aufz-offen"] && !("jolt-puffer-" + r.session_id in w.data),
         "danach ist nichts mehr auf dem Gerät übrig");

  w = world(); w.state.grid = false;
  r = await w.P.start(w.api, { vehicle_id: 1, lat: 1, lon: 2 });
  const bad = "2026-10-14T10:03:00Z";
  w.data["jolt-puffer-" + r.session_id] = JSON.stringify(
    Array.from({ length: 8 }, (_, i) => ({ lat: 1, lon: 2, soc: 50,
      timestamp: "2026-10-14T10:0" + i + ":00Z" })));
  w.P.requestEnd(); w.state.grid = true; w.state.badPoint = bad; w.calls.length = 0;
  const done2 = await w.P.syncEnd(w.api);
  const ok = w.calls.filter((c) => /points$/.test(c.url) && !c.body.points.some((p) => p.timestamp === bad))
    .reduce((n, c) => n + c.body.points.length, 0);
  verify(done2 === true && ok === 7,
         "ein einzelner abgelehnter Punkt kostet nur sich selbst, die übrigen kommen an",
         `fertig=${done2}, angekommen=${ok}`);

  console.log(failure ? `\n${failure} Pruefung(en) fehlgeschlagen.` : "\nAlle Pruefungen bestanden.");
  process.exit(failure ? 1 : 0);
})();
