/* Service Worker: das Gerüst offline halten, Daten nie.
 *
 * Zwischengespeichert werden ausschliesslich die eigenen statischen Dateien.
 * Antworten der API bleiben aussen vor - ein Ladeplan aus dem Cache wäre
 * schlimmer als gar keiner: Er sähe aus wie ein Plan, stammte aber aus einer
 * Zeit, in der der Ladestand ein anderer war.
 *
 * Der Nutzen ist trotzdem real: Bei einem Balken Empfang lädt die Oberfläche
 * sofort, statt auf ein Gerüst zu warten, das sich ohnehin nicht geändert hat.
 */
// Bei jeder Änderung am Gerüst hochzählen: Der Name ist der einzige Hebel,
// mit dem ein alter Cache verworfen wird (siehe "activate").
const CACHE = "jolt-v25";
const SCAFFOLD = [
  "/", "/static/core.js", "/static/map.js", "/static/route.js",
  "/static/tiles.js", "/static/display.js", "/static/live.js", "/static/trips.js", "/static/vehicle.js",
  "/static/settings.js", "/static/app.js",
  // Die OBD2-Diagnoseseite: Bluetooth braucht kein Netz, und
  // eine Tiefgarage ist genau der Ort, an dem man sie aufruft.
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
  if (url.origin !== self.location.origin) return;   // Kartenkacheln
  if (url.pathname.startsWith("/api/")) return;      // nie zwischenspeichern

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        // Erfolgreiche Antworten aktualisieren den Cache, damit nach einem
        // Update nicht die alte Version festhängt.
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch(() => caches.match(event.request)
        .then((hit) => hit || caches.match("/"))));
});

/* ---------- Benachrichtigungen ----------
 *
 * Der Service Worker läuft auch, wenn die Seite geschlossen und der Bildschirm
 * aus ist - das ist der ganze Grund, warum eine Planänderung über den
 * Push-Dienst geht und nicht über die offene WebSocket-Verbindung.
 */
self.addEventListener("push", (event) => {
  let records = { title: "jolt", text: "Der Ladeplan hat sich geändert.", url: "/" };
  try {
    if (event.data) records = Object.assign(records, event.data.json());
  } catch (e) {
    // Eine Nutzlast, die kein JSON ist, kommt nicht von jolt. Die Vorgabe
    // anzuzeigen ist besser, als die Meldung ganz zu verschlucken.
  }

  event.waitUntil(self.registration.showNotification(records.title, {
    body: records.text,
    icon: "/static/icon.svg",
    badge: "/static/icon.svg",
    // Gleicher tag: Eine neue Planänderung ersetzt die alte, statt sich
    // daneben zu legen. Am Steuer zählt der aktuelle Plan, nicht die Historie.
    day: "jolt-plan",
    renotify: true,
    data: { url: records.url || "/" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const destination = (event.notification.data && event.notification.data.url) || "/";

  // Ein bereits offenes Fenster in den Vordergrund holen, statt ein zweites
  // zu öffnen: Sonst stehen nach drei Meldungen drei jolt-Tabs offen, und in
  // keinem läuft die Live-Verbindung, die man gerade braucht.
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
