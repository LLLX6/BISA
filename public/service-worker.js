const CACHE = 'bisa-pwa-v0.1.1-demo-catalog';
const BASE = new URL('./', self.registration.scope).pathname;
const PUBLIC_ASSETS = [
  BASE, `${BASE}index.html`, `${BASE}manifest.webmanifest`,
  `${BASE}assets/styles/bisa.css`, `${BASE}assets/scripts/bisa-app.js`,
  `${BASE}assets/brand/bisa-logo.svg`, `${BASE}assets/brand/bisa-mark.svg`
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(PUBLIC_ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('bisa-pwa-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith(`${BASE}api/`)) return;
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).then(response => {
      const copy = response.clone(); caches.open(CACHE).then(cache => cache.put(`${BASE}index.html`, copy)); return response;
    }).catch(() => caches.match(`${BASE}index.html`)));
    return;
  }
  event.respondWith(caches.match(request).then(hit => hit || fetch(request).then(response => {
    if (response.ok) caches.open(CACHE).then(cache => cache.put(request, response.clone()));
    return response;
  })));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const route = event.notification?.data?.route || BASE;
  event.waitUntil(clients.matchAll({type:'window', includeUncontrolled:true}).then(items => {
    const client = items[0];
    if (client) { client.postMessage({type:'bisa:notification', route}); return client.focus(); }
    return clients.openWindow(route);
  }));
});
