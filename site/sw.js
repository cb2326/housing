// Open Housing Atlas app helper. Always tries the network first, so the app shows the same, current data as the
// website; when there is no connection it falls back to the last copy it saw, so the app still opens offline.
const CACHE = 'oha-v1';
const SHELL = ['/', '/manifest.webmanifest', '/icons/icon-192.png', '/icons/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  // only this site's own files and the map's code libraries and fonts; everything else (map tiles, Ko-fi,
  // visitor counts) goes straight through and is not stored on the phone
  const own = url.origin === location.origin;
  if (!own && !/unpkg\.com|cdn\.jsdelivr\.net|cdnjs\.cloudflare\.com|fonts\.(googleapis|gstatic)\.com/.test(url.host)) return;
  e.respondWith(
    fetch(req).then(res => {
      if (res.ok && (own || res.type === 'cors' || res.type === 'opaque')) {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(req, copy));
      }
      return res;
    }).catch(() => caches.match(req).then(hit => hit || (req.mode === 'navigate' ? caches.match('/') : Response.error())))
  );
});
