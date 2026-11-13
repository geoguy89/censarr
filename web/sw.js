/*
 * The service worker, which exists for two reasons.
 *
 * 1. Chrome will not offer "Install app" without one that handles fetch. No
 *    service worker means Add to Home Screen gives you a bookmark with a
 *    browser bar, not an app.
 * 2. Opening an installed app to a blank page because the server was briefly
 *    unreachable is a bad first impression. The shell is cached, so the app
 *    frame appears instantly and the data loads behind it.
 *
 * What it deliberately does NOT do is cache API responses. Every screen here
 * is about what is happening right now - a queue, a job's progress, what has
 * been cleaned. A stale queue served from cache would be worse than an error,
 * because it looks correct.
 */

const VERSION = 'censarr-v12';
const SHELL = [
  '/',
  '/static/style.css?v=6',
  '/static/app.js?v=10',
  '/static/logo.svg?v=5',
  '/static/icon.png?v=5',
  '/static/manifest.json',
];

self.addEventListener('install', (event) => {
  // Take over straight away rather than waiting for every tab to close; a
  // service worker that needs the user to quit the app to update is one that
  // never updates.
  event.waitUntil(
    caches.open(VERSION)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
      // One missing file must not leave the app with no worker at all.
      .catch(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names.filter((n) => n !== VERSION).map((n) => caches.delete(n)),
      ))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Anything live goes straight to the network, always. Serving a cached
  // queue or a cached login state would be actively misleading.
  if (url.pathname.startsWith('/api/')) return;

  // The shell: network first so an update is picked up on the next launch,
  // falling back to the cache when the server cannot be reached.
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(VERSION).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request).then(
        (hit) => hit || caches.match('/'),
      )),
  );
});
