/* Service worker de CriptoCartera.
 * Cachea el "esqueleto" de la app (HTML/CSS/JS/iconos + Chart.js) para que
 * abra al instante y funcione sin conexión. Los datos de mercado NUNCA se
 * cachean: siempre se piden frescos a la red.
 */
const CACHE = 'criptocartera-v1';

const SHELL = [
  './',
  './index.html',
  './styles.css',
  './app.js',
  './manifest.webmanifest',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/apple-touch-icon.png',
  'https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // Datos de mercado: siempre red (no cachear precios/gráficas/índice).
  const isMarketData =
    url.hostname.includes('binance') ||
    url.hostname.includes('alternative.me');
  if (isMarketData) {
    event.respondWith(fetch(req).catch(() => Response.error()));
    return;
  }

  // Esqueleto de la app: primero caché, con respaldo a red (y se guarda).
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).then((res) => {
        if (res && res.ok && (url.origin === self.location.origin || url.hostname.includes('jsdelivr'))) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }).catch(() => cached);
    })
  );
});
