const CACHE_NAME = 'nifty-dashboard-v1';
const STATIC_ASSETS = [
  './',
  './index.html',
  './manifest.json'
];

// 1. Install Event - Pre-cache core UI assets
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(STATIC_ASSETS);
    }).then(() => self.skipWaiting())
  );
});

// 2. Activate Event - Clean up old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME)
          .map((name) => caches.delete(name))
      );
    }).then(() => self.clients.claim())
  );
});

// 3. Fetch Event - Handle Live Data vs Static Assets
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Strategy for data.json: Network-only with cache fallback
  if (url.pathname.includes('data.json')) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          // Clone and store fresh JSON data in cache
          const responseClone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, responseClone));
          return response;
        })
        .catch(() => caches.match(event.request)) // Fallback to last known cached JSON if offline
    );
    return;
  }

  // Strategy for App Shell (HTML, icons): Cache First, fallback to Network
  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      if (cachedResponse) {
        return cachedResponse;
      }
      return fetch(event.request);
    })
  );
});
