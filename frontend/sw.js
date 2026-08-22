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
const CACHE = "jolt-v2";
const GERUEST = [
  "/", "/static/core.js", "/static/karte.js", "/static/route.js",
  "/static/live.js", "/static/fahrzeug.js", "/static/app.js",
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
