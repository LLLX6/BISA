'use strict';

const CACHE_PREFIX = 'bisa-pwa-';
const CACHE_VERSION = '0.2.0-dev';
const CACHE = `${CACHE_PREFIX}v${CACHE_VERSION}`;
const SCOPE_URL = new URL('./', self.registration.scope);
const BASE = SCOPE_URL.pathname;
const API_PREFIX = new URL('api/', SCOPE_URL).pathname;
const ROOT_API_PREFIX = '/api/';
const PUBLIC_ASSETS = [
  BASE,
  `${BASE}index.html`,
  `${BASE}manifest.webmanifest`,
  `${BASE}app-icon-192.png`,
  `${BASE}app-icon-512.png`,
  `${BASE}app-icon-maskable-192.png`,
  `${BASE}app-icon-maskable-512.png`,
  `${BASE}assets/styles/bisa.css`,
  `${BASE}assets/scripts/bisa-app.js`,
  `${BASE}assets/scripts/bisa-map.js`,
  `${BASE}vendor/leaflet.css`,
  `${BASE}vendor/leaflet.js`,
  `${BASE}assets/brand/bisa-logo.svg`,
  `${BASE}assets/brand/bisa-mark.svg`,
  `${BASE}assets/images/bisa-hero-v1.webp`,
];

function safeNotificationId(value) {
  const identifier = String(value || '').trim();
  return /^[A-Za-z0-9._:-]{1,180}$/.test(identifier) ? identifier : '';
}

function notificationRoute(identifier) {
  const route = new URL(SCOPE_URL.href);
  if (identifier) route.hash = `notification=${encodeURIComponent(identifier)}`;
  return route.href;
}

function isApiPath(pathname) {
  return pathname === '/api'
    || pathname.startsWith(ROOT_API_PREFIX)
    || pathname === API_PREFIX.slice(0, -1)
    || pathname.startsWith(API_PREFIX);
}

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE)
      .then(cache => cache.addAll(PUBLIC_ASSETS))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys
          .filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE)
          .map(key => caches.delete(key)),
      ))
      .then(() => self.registration.navigationPreload?.enable())
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);

  // Private and account APIs are always network-only and never enter PWA caches.
  if (url.origin !== SCOPE_URL.origin || isApiPath(url.pathname)) return;

  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const response = (await event.preloadResponse) || await fetch(request);
        if (response.ok) {
          const cache = await caches.open(CACHE);
          await cache.put(`${BASE}index.html`, response.clone());
        }
        return response;
      } catch {
        return (await caches.match(`${BASE}index.html`)) || Response.error();
      }
    })());
    return;
  }

  event.respondWith((async () => {
    const cached = await caches.match(request);
    if (cached) return cached;
    const response = await fetch(request);
    if (response.ok && response.type === 'basic') {
      const cache = await caches.open(CACHE);
      await cache.put(request, response.clone());
    }
    return response;
  })());
});

self.addEventListener('push', event => {
  let payload = {};
  try {
    payload = event.data?.json() || {};
  } catch {
    payload = {};
  }
  const identifier = safeNotificationId(payload.notificationId);
  event.waitUntil(self.registration.showNotification('BISA | بيسا', {
    body: 'لديك تحديث جديد في بيسا.\nYou have a new update in BISA.',
    icon: `${BASE}app-icon-192.png`,
    badge: `${BASE}app-icon-192.png`,
    tag: identifier ? `bisa:${identifier}` : 'bisa:update',
    renotify: false,
    requireInteraction: false,
    data: {notificationId: identifier},
  }));
});

self.addEventListener('pushsubscriptionchange', event => {
  event.waitUntil((async () => {
    let subscription = event.newSubscription || null;
    if (!subscription && event.oldSubscription?.options?.applicationServerKey) {
      try {
        subscription = await self.registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: event.oldSubscription.options.applicationServerKey,
        });
      } catch {
        subscription = null;
      }
    }
    const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    windows.forEach(client => client.postMessage({
      type: 'bisa:push-subscription-change',
      hasSubscription: Boolean(subscription),
    }));
  })());
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const identifier = safeNotificationId(event.notification?.data?.notificationId);
  const route = notificationRoute(identifier);
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(items => {
    const client = items.find(item => {
      const candidate = new URL(item.url);
      return candidate.origin === SCOPE_URL.origin && candidate.pathname.startsWith(BASE);
    });
    if (client) {
      client.postMessage({type: 'bisa:notification', notificationId: identifier});
      return client.focus();
    }
    return self.clients.openWindow(route);
  }));
});

self.addEventListener('message', event => {
  if (event.data?.type === 'bisa:skip-waiting') self.skipWaiting();
});
