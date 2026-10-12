/* TEKNO CONTROL service worker: caches the app shell so the homescreen icon opens instantly.
   Live data (/api, /ws, /media) is never cached. */
const CACHE = "tekno-shell-v15";
const SHELL = ["/", "/static/app.css", "/static/js/app.js", "/static/js/bots.js", "/static/js/player.js",
  "/static/js/charts.js", "/static/js/live.js", "/static/js/control.js", "/static/js/v3.js", "/static/v3.css", "/desktop", "/static/desktop.css", "/static/js/desktop.js", "/manifest.webmanifest", "/static/icons/icon-192.png", "/static/icons/icon-512.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin) return;
  if (/^\/(api|ws|media|handoff|demo-audio|palette|reaper)/.test(u.pathname)) return;  // never cache private audio or data
  // network first (fresh code after deploys), cache as offline fallback
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request)));
});
