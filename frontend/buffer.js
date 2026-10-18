/* The queue of measurement points on their way to the server.
 *
 * Split from the live view because it needs only a few things from it, handed
 * in as `hooks`: the current session id, the API call, a report function, a
 * test for a not yet uploaded ("local") session, how to make that one real, and
 * what to do with the state the server answers with.
 */
window.joltBuffer = (function () {
  "use strict";

  /* Every measurement point goes into a queue first and from there to the
   * server - never directly. If the network drops, the points stay put and
   * go out together on the next attempt, with their measurement time.
   *
   * Before, `reportPosition` swallowed the failed POST. For a planned
   * trip that is harmless, the next point comes. For a recording it is
   * data loss: after twenty minutes without network up to 13 % of the route
   * was missing, and the learned factor shifted by up to 35 % -
   * invisibly, in a number that stays on the vehicle permanently.
   *
   * There is only ever **one** send operation. Two simultaneous ones could
   * overtake each other, and the server would get newer points before older ones.
   *
   * The queue is also kept in localStorage: if iOS reloads the page in the
   * background, the points should not vanish with it. The limit
   * protects against memory that grows forever; then the oldest go. */

  function create(hooks) {
    const BUFFER_MAX = 2000;
    const BATCH_MAX = 100;
    // How many points the next batch has. Equal to BATCH_MAX, unless the
    // server has just rejected a batch: then it is halved until the
    // one bad point is identified.
    let batchSize = BATCH_MAX;
    let buffer = [];
    let bufferSession = null;
    let bufferRun = null;
    let bufferAgain = false;
    let withoutGridReported = false;
    let supplied_later = 0;
    let rejected = 0;              // individual points rejected by the server

    function bufferStorage(id) { return "jolt-puffer-" + id; }

    function forSession(id) {
      if (bufferSession === id) return;
      bufferSession = id;
      buffer = [];
      try {
        const raw = JSON.parse(localStorage.getItem(bufferStorage(id)) || "[]");
        if (Array.isArray(raw)) buffer = raw;
      } catch (e) { /* no storage or corrupted: carry on without */ }
    }

    function save() {
      if (bufferSession === null) return;
      try {
        if (buffer.length) {
          localStorage.setItem(bufferStorage(bufferSession), JSON.stringify(buffer));
        } else {
          localStorage.removeItem(bufferStorage(bufferSession));
        }
      } catch (e) { /* full or blocked: then it just stays in memory */ }
    }

    function append(point) {
      forSession(hooks.sessionId());
      buffer.push(point);
      if (buffer.length > BUFFER_MAX) buffer.splice(0, buffer.length - BUFFER_MAX);
      // It is only saved when something piles up - normally the
      // point is on the server a second later.
      if (buffer.length > 1) save();
    }

    function process() {
      if (bufferRun) { bufferAgain = true; return bufferRun; }
      bufferRun = sendBuffer().finally(() => { bufferRun = null; });
      return bufferRun;
    }

    async function sendBuffer() {
      do {
        bufferAgain = false;
        if (hooks.isLocal(hooks.sessionId()) && !(await hooks.materialize())) {
          if (!withoutGridReported) {
            withoutGridReported = true;
            hooks.report("Kein Netz – die Aufzeichnung läuft lokal und wird "
              + "nachgeliefert.", "hinweis");
          }
          save();
          return;
        }
        const id = hooks.sessionId();
        while (id && buffer.length && hooks.sessionId() === id) {
          const batch = buffer.slice(0, batchSize);
          let state;
          try {
            state = await hooks.api(`/api/live/${id}/points`,
              { method: "POST", body: { points: batch } });
          } catch (failure) {
            const status = failure.status;
            if (status === 404 || status === 409) {
              // The session no longer exists or has ended: sending on
              // would mean fetching the same rejection for all eternity.
              buffer = [];
            } else if (status === 422) {
              // The server considers the batch invalid - a point in it, say
              // with a timestamp from more than two days ago. The whole
              // batch would never be accepted and would clog everything behind it.
              //
              // Here the batch used to be discarded: up to ninety-nine good ones
              // because of one bad point. Now it is halved until the
              // one point is identified; only it gets thrown out. For a hundred
              // points that costs at most seven further requests.
              if (batch.length > 1) {
                batchSize = Math.ceil(batch.length / 2);
              } else {
                buffer.splice(0, 1);
                batchSize = BATCH_MAX;
                rejected += 1;
              }
            } else if (!withoutGridReported) {
              withoutGridReported = true;
              hooks.report("Keine Verbindung zu jolt – die Messpunkte werden "
                + "gesammelt und nachgereicht.", "hinweis");
            }
            save();
            // Network gone, server overloaded or logged out: leave it lying, the
            // next point or the return of the network tries again.
            if (status !== 404 && status !== 409 && status !== 422) return;
            continue;
          }
          buffer.splice(0, batch.length);
          save();
          if (withoutGridReported) supplied_later += batch.length;
          // Show the state only when the batch has reached the present.
          // In the middle of catching up it would be the one from ten minutes ago.
          if (!buffer.length) {
            if (withoutGridReported) {
              withoutGridReported = false;
              hooks.report("Verbindung wieder da – " + supplied_later
                + " Messpunkte nachgereicht.", "hinweis");
              supplied_later = 0;
            }
            hooks.onState(state);
          }
        }
      } while (bufferAgain);
      // After a rejection it continues in small batches until the bad
      // point is found (then the size is full again) - or until nothing
      // is waiting any more. Growing after every success would hit the bad point
      // again and again: for 13 points that cost seven rejected requests
      // instead of four.
      batchSize = BATCH_MAX;
      if (rejected > 0) {
        hooks.report(`${rejected} Messpunkt${rejected === 1 ? " wurde" : "e wurden"} `
          + "vom Server abgelehnt und verworfen - die übrigen sind angekommen.",
          "hinweis");
        rejected = 0;
      }
    }

    window.addEventListener("online", () => {
      if (hooks.sessionId()) process();
    });

    return {
      forSession, append, save, process,
      length: () => buffer.length,
      // The local session got its real id: the queue follows.
      rename(local, real) { if (bufferSession === local) bufferSession = real; },
      // The trip is over. The points of a recording that still waits for the
      // network stay in storage - they are all there is.
      end(keepStored) {
        buffer = [];
        if (!keepStored) save();
        bufferSession = null;
        withoutGridReported = false;
        supplied_later = 0;
      },
    };
  }

  return { create };
})();
