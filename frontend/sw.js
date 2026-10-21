/* Service worker: keep the shell offline, data never.
 *
 * Only the own static files are cached. API responses stay out - a charging
 * plan from the cache would be worse than none at all: it would look like a
 * plan, but come from a time when the charge level was a different one.
 *
 * The benefit is real nonetheless: with one bar of reception the UI loads
 * immediately instead of waiting for a shell that has not changed anyway.
 */
// Count up with every change to the shell: the name is the only lever by
// which an old cache is discarded (see "activate").
const CACHE = "jolt-v49";
const SCAFFOLD = [
  "/", "/static/app.css", "/static/motion.css", "/static/core.js", "/static/map.js", "/static/route.js",
  "/static/tiles.js", "/static/display.js", "/static/pending.js", "/static/haptics.js", "/static/sheet.js", "/static/awake.js", "/static/notify.js", "/static/consumption.js", "/static/drive-state.js", "/static/buffer.js", "/static/history-chart.js", "/static/live.js", "/static/trips.js", "/static/vehicle.js",
  "/static/settings.js", "/static/app.js",
  // The OBD2 diagnostics page: Bluetooth needs no network, and an
  // underground car park is exactly the place where one opens it.
  "/obd", "/static/ble-plugin.js", "/static/obd-ble-native.js",
  "/static/platforms/meb.js", "/static/platforms/obd2.js", "/static/platforms/ecmp.js", "/static/obd-decode.js", "/static/obd-core.js", "/static/obd.js", "/static/obd.css",
  "/manifest.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SCAFFOLD))
      .then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names.filter((n) => n !== CACHE).map((n) => caches.delete(n))))
      .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET") return;
  if (url.origin !== self.location.origin) return;   // map tiles
  if (url.pathname.startsWith("/api/")) return;      // never cache

  // The shell comes from the copy at once and is refreshed in the
  // background: the start does not wait for fifteen requests through the
  // tunnel. A change is there from the next start on (the cache name above
  // discards old copies). Whatever is not stored yet takes the old way.
  event.respondWith(
    caches.match(copyKey(url)).then((hit) => {
      if (!hit) return networkOrCache(event.request, url);
      refreshCopy(event.request, url);
      return hit;
    }));
});

function refreshCopy(request, url) {
  fetch(request).then((response) => {
    if (response.ok) {
      caches.open(CACHE).then((cache) => cache.put(copyKey(url), response));
    }
  }).catch(() => { /* no network: the copy stays */ });
}

/* How long the network gets before the copy on the device is used.
 *
 * Plain "network first" waits for the browser's own time limit when the
 * connection is bad but not gone - that is the minute of white screen in a
 * poor reception area. With this wait the page comes from the copy after a
 * few seconds; the request keeps running and refreshes the copy, so the next
 * start has the new version. With good reception nothing changes. */
const NETWORK_WAIT_MS = 2500;

// The copy is stored without the `?v=` of the page's references: one entry per
// file, always the latest one.
function copyKey(url) { return new Request(url.origin + url.pathname); }

function networkOrCache(request, url) {
  return new Promise((resolve) => {
    let answered = false;
    const answer = (response) => {
      if (answered || !response) return false;
      answered = true;
      resolve(response);
      return true;
    };
    const fromCopy = () => caches.match(copyKey(url))
      .then((hit) => hit || caches.match("/"));

    const timer = setTimeout(() => {
      fromCopy().then((hit) => answer(hit));
    }, NETWORK_WAIT_MS);

    fetch(request)
      .then((response) => {
        clearTimeout(timer);
        // Successful responses update the copy, so that the old version does
        // not get stuck after an update.
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(copyKey(url), copy));
        }
        answer(response);
      })
      .catch(() => {
        clearTimeout(timer);
        fromCopy().then((hit) => {
          if (!answer(hit)) answer(Response.error());
        });
      });
  });
}

/* ---------- Notifications ----------
 *
 * The service worker also runs when the page is closed and the screen is
 * off - that is the whole reason why a plan change goes via the push service
 * and not via the open WebSocket connection.
 */
self.addEventListener("push", (event) => {
  let payload = { title: "jolt", text: "Der Ladeplan hat sich geändert.", url: "/" };
  try {
    if (event.data) payload = Object.assign(payload, event.data.json());
  } catch (e) {
    // A payload that is not JSON does not come from jolt. Showing the default
    // is better than swallowing the message entirely.
  }

  event.waitUntil(self.registration.showNotification(payload.title, {
    body: payload.text,
    icon: "/static/icon.svg",
    badge: "/static/icon.svg",
    // Same tag: a new plan change replaces the old one instead of lying next
    // to it. At the wheel the current plan counts, not the history.
    day: "jolt-plan",
    renotify: true,
    data: { url: payload.url || "/" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const destination = (event.notification.data && event.notification.data.url) || "/";

  // Bring an already open window to the foreground instead of opening a
  // second one: otherwise after three messages three jolt tabs are open, and
  // none of them runs the live connection that is needed right now.
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true })
      .then((openWindows) => {
        for (const f of openWindows) {
          if (f.url.indexOf(self.location.origin) === 0 && "focus" in f) {
            return f.focus();
          }
        }
        return self.clients.openWindow(destination);
      }));
});
