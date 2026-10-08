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
const CACHE = "jolt-v25";
const SCAFFOLD = [
  "/", "/static/core.js", "/static/map.js", "/static/route.js",
  "/static/tiles.js", "/static/display.js", "/static/live.js", "/static/trips.js", "/static/vehicle.js",
  "/static/settings.js", "/static/app.js",
  // The OBD2 diagnostics page: Bluetooth needs no network, and an
  // underground car park is exactly the place where one opens it.
  "/obd", "/static/ble-plugin.js", "/static/obd-ble-native.js",
  "/static/readings.js", "/static/obd-core.js", "/static/obd.js", "/static/obd.css",
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

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        // Successful responses update the cache, so that the old version does
        // not get stuck after an update.
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch(() => caches.match(event.request)
        .then((hit) => hit || caches.match("/"))));
});

/* ---------- Notifications ----------
 *
 * The service worker also runs when the page is closed and the screen is
 * off - that is the whole reason why a plan change goes via the push service
 * and not via the open WebSocket connection.
 */
self.addEventListener("push", (event) => {
  let records = { title: "jolt", text: "Der Ladeplan hat sich geändert.", url: "/" };
  try {
    if (event.data) records = Object.assign(records, event.data.json());
  } catch (e) {
    // A payload that is not JSON does not come from jolt. Showing the default
    // is better than swallowing the message entirely.
  }

  event.waitUntil(self.registration.showNotification(records.title, {
    body: records.text,
    icon: "/static/icon.svg",
    badge: "/static/icon.svg",
    // Same tag: a new plan change replaces the old one instead of lying next
    // to it. At the wheel the current plan counts, not the history.
    day: "jolt-plan",
    renotify: true,
    data: { url: records.url || "/" },
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
      .then((timeframe) => {
        for (const f of timeframe) {
          if (f.url.indexOf(self.location.origin) === 0 && "focus" in f) {
            return f.focus();
          }
        }
        return self.clients.openWindow(destination);
      }));
});
