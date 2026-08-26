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
const CACHE = "jolt-v8";
const GERUEST = [
  "/", "/static/core.js", "/static/karte.js", "/static/route.js",
  "/static/live.js", "/static/fahrten.js", "/static/fahrzeug.js",
  "/static/app.js",
  // Die OBD2-Diagnoseseite: Bluetooth braucht kein Netz, und
  // eine Tiefgarage ist genau der Ort, an dem man sie aufruft.
  "/obd", "/static/obd.js", "/static/obd.css",
  "/manifest.json",
];

self.addEventListener("install", (ereignis) => {
  ereignis.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(GERUEST))
      .then(() => self.skipWaiting()));
});

self.addEventListener("activate", (ereignis) => {
  ereignis.waitUntil(
    caches.keys()
      .then((namen) => Promise.all(
        namen.filter((n) => n !== CACHE).map((n) => caches.delete(n))))
      .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (ereignis) => {
  const url = new URL(ereignis.request.url);
  if (ereignis.request.method !== "GET") return;
  if (url.origin !== self.location.origin) return;   // Kartenkacheln
  if (url.pathname.startsWith("/api/")) return;      // nie zwischenspeichern

  ereignis.respondWith(
    fetch(ereignis.request)
      .then((antwort) => {
        // Erfolgreiche Antworten aktualisieren den Cache, damit nach einem
        // Update nicht die alte Version festhängt.
        if (antwort.ok) {
          const kopie = antwort.clone();
          caches.open(CACHE).then((cache) => cache.put(ereignis.request, kopie));
        }
        return antwort;
      })
      .catch(() => caches.match(ereignis.request)
        .then((treffer) => treffer || caches.match("/"))));
});

/* ---------- Benachrichtigungen ----------
 *
 * Der Service Worker läuft auch, wenn die Seite geschlossen und der Bildschirm
 * aus ist - das ist der ganze Grund, warum eine Planänderung über den
 * Push-Dienst geht und nicht über die offene WebSocket-Verbindung.
 */
self.addEventListener("push", (ereignis) => {
  let daten = { titel: "jolt", text: "Der Ladeplan hat sich geändert.", url: "/" };
  try {
    if (ereignis.data) daten = Object.assign(daten, ereignis.data.json());
  } catch (e) {
    // Eine Nutzlast, die kein JSON ist, kommt nicht von jolt. Die Vorgabe
    // anzuzeigen ist besser, als die Meldung ganz zu verschlucken.
  }

  ereignis.waitUntil(self.registration.showNotification(daten.titel, {
    body: daten.text,
    icon: "/static/icon.svg",
    badge: "/static/icon.svg",
    // Gleicher tag: Eine neue Planänderung ersetzt die alte, statt sich
    // daneben zu legen. Am Steuer zählt der aktuelle Plan, nicht die Historie.
    tag: "jolt-plan",
    renotify: true,
    data: { url: daten.url || "/" },
  }));
});

self.addEventListener("notificationclick", (ereignis) => {
  ereignis.notification.close();
  const ziel = (ereignis.notification.data && ereignis.notification.data.url) || "/";

  // Ein bereits offenes Fenster in den Vordergrund holen, statt ein zweites
  // zu öffnen: Sonst stehen nach drei Meldungen drei jolt-Tabs offen, und in
  // keinem läuft die Live-Verbindung, die man gerade braucht.
  ereignis.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true })
      .then((fenster) => {
        for (const f of fenster) {
          if (f.url.indexOf(self.location.origin) === 0 && "focus" in f) {
            return f.focus();
          }
        }
        return self.clients.openWindow(ziel);
      }));
});
