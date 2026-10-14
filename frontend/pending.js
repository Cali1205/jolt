/* A recording that starts without network.
 *
 * Starting a recording needs the server only to hand out a session number -
 * everything the car and the phone measure can be collected locally and
 * delivered later (the measurement queue in live.js already does that for
 * dead spots). So: ask the server briefly; if it does not answer, start with
 * a local number ("local-...") and create the recording on the server as soon
 * as the network is back, with the **real start time**. The points carry
 * their own measurement times, so nothing is lost or shifted.
 *
 * The same start sent twice (the answer got lost on a bad connection) finds
 * the recording it already created: the server matches on vehicle and start
 * time.
 *
 * Ending without network works the same way: the end is remembered and
 * carried out - together with the remaining points - when the server can be
 * reached again. That part runs on its own timer (`watch`), independent of
 * which view is open.
 */
window.joltPending = (function () {
  "use strict";

  const KEY = "jolt-aufz-offen";
  const PREFIX = "local-";
  const START_TIMEOUT_MS = 6000;
  const SYNC_TIMEOUT_MS = 20000;
  const SYNC_EVERY_MS = 30000;
  const BATCH = 100;

  const isLocal = (id) => typeof id === "string" && id.startsWith(PREFIX);

  function store() {
    try { return window.localStorage; } catch (e) { return null; }
  }

  function read() {
    const s = store();
    if (!s) return null;
    try {
      const rec = JSON.parse(s.getItem(KEY) || "null");
      return rec && typeof rec === "object" && isLocal(rec.id) ? rec : null;
    } catch (e) { return null; }
  }

  function write(rec) {
    const s = store();
    if (!s) return;
    try {
      if (rec) s.setItem(KEY, JSON.stringify(rec));
      else s.removeItem(KEY);
    } catch (e) { /* full or blocked: then it only lives in the caller's memory */ }
  }

  const bufferKey = (id) => "jolt-puffer-" + id;

  /* Create the recording on the server - or locally, if it cannot be reached.
   * A server that answers with an error (unknown vehicle, ...) is not
   * "offline": that error goes to the caller. */
  async function start(api, body) {
    const started_at = new Date().toISOString();
    const full = Object.assign({}, body, { started_at });
    try {
      const reply = await api("/api/live/recording",
        { method: "POST", body: full, timeout: START_TIMEOUT_MS });
      return Object.assign({ pending: false }, reply);
    } catch (failure) {
      if (failure && failure.status) throw failure;
      const rec = { id: PREFIX + Date.now(), body: full, end_requested: false,
                    session_id: null };
      write(rec);
      return { session_id: rec.id, trip_id: null, recording: true, pending: true };
    }
  }

  let materializing = null;

  /* Make the local recording a real one. Returns `{session_id}` or null while
   * the server cannot be reached. An error from the server (the start is too
   * old, the vehicle is gone) drops the local recording and is thrown. */
  function materialize(api) {
    if (materializing) return materializing;
    materializing = (async () => {
      const rec = read();
      if (!rec) return null;
      if (rec.session_id) return { session_id: rec.session_id };
      try {
        const reply = await api("/api/live/recording",
          { method: "POST", body: rec.body, timeout: SYNC_TIMEOUT_MS });
        rec.session_id = reply.session_id;
        write(rec);
        return { session_id: reply.session_id };
      } catch (failure) {
        if (failure && failure.status) { clear(); throw failure; }
        return null;
      }
    })().finally(() => { materializing = null; });
    return materializing;
  }

  /* Forget the record and the points stored under its local id. */
  function clear() {
    const rec = read();
    write(null);
    const s = store();
    if (rec && s) { try { s.removeItem(bufferKey(rec.id)); } catch (e) { /* none */ } }
  }

  /* Forget only the record: the points now belong to the real session. */
  function release() { write(null); }

  function renameBuffer(from, to) {
    const s = store();
    if (!s) return;
    try {
      const raw = s.getItem(bufferKey(from));
      if (raw !== null) s.setItem(bufferKey(to), raw);
      s.removeItem(bufferKey(from));
    } catch (e) { /* stays under the old key; the in-memory queue carries on */ }
  }

  function requestEnd() {
    const rec = read();
    if (rec) { rec.end_requested = true; write(rec); }
  }

  function saveRest(rec, points) {
    const s = store();
    if (!s) return;
    try { s.setItem(bufferKey(rec.id), JSON.stringify(points)); } catch (e) { /* none */ }
  }

  /* Carry out a remembered end: create the recording if necessary, deliver
   * the stored points in batches, end it. True when everything is done. */
  async function syncEnd(api) {
    const rec = read();
    if (!rec || !rec.end_requested) return false;
    let made;
    try { made = await materialize(api); } catch (failure) { return true; }
    if (!made) return false;
    const id = made.session_id;
    const s = store();
    let points = [];
    try { points = JSON.parse((s && s.getItem(bufferKey(rec.id))) || "[]") || []; }
    catch (e) { points = []; }
    let size = BATCH;
    while (points.length) {
      const batch = points.slice(0, size);
      try {
        await api(`/api/live/${id}/points`,
          { method: "POST", body: { points: batch }, timeout: SYNC_TIMEOUT_MS });
        points.splice(0, batch.length);
        size = BATCH;
      } catch (failure) {
        const status = failure && failure.status;
        if (status === 422) {
          // A bad point: halve until it is found, then drop only that one.
          if (batch.length > 1) size = Math.ceil(batch.length / 2);
          else { points.splice(0, 1); size = BATCH; }
        } else if (status === 404 || status === 409) {
          points = [];
        } else {
          saveRest(rec, points);
          return false;
        }
      }
      saveRest(rec, points);
    }
    try { await api(`/api/live/${id}/end`, { method: "POST", timeout: SYNC_TIMEOUT_MS }); }
    catch (failure) {
      if (!(failure && failure.status)) return false;   // network: try again later
    }
    clear();
    return true;
  }

  let watching = false;
  let syncing = false;

  /* Retry a remembered end now and then, and when the network returns. */
  function watch(api, onDone) {
    if (watching) return;
    watching = true;
    const run = async () => {
      if (syncing) return;
      syncing = true;
      try { if (await syncEnd(api) && onDone) onDone(); }
      catch (e) { /* next time */ }
      finally { syncing = false; }
    };
    setInterval(run, SYNC_EVERY_MS);
    window.addEventListener("online", run);
    setTimeout(run, 3000);
  }

  return { isLocal, start, materialize, clear, release, renameBuffer, requestEnd,
           syncEnd, watch, load: read };
})();
