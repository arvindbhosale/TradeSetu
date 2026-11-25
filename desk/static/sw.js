"use strict";
// TradeSetu service worker. It makes the desk installable and keeps the
// interface files ready, and that is all: prices, orders and account data are
// never cached, because a stale price or order status is worse than none.
const CACHE = "desk-shell-v18";
const SHELL = ["/static/app.css", "/static/app.js", "/static/theme.js", "/static/lightweight-charts.js",
  "/static/offline.html", "/static/icon-192.png", "/static/logo-mark.png", "/static/favicon.ico"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const request = event.request, url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/kite/") || url.pathname.startsWith("/dl/")) return;  // always live
  if (request.mode === "navigate") {
    // Pages carry the session; fetch them fresh and fall back to a plain offline note.
    event.respondWith(fetch(request).catch(() => caches.match("/static/offline.html")));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    // Interface files: the network copy wins so an update shows at once; the cache serves when offline.
    event.respondWith(fetch(request).then((response) => {
      if (response.ok) { const copy = response.clone(); caches.open(CACHE).then((cache) => cache.put(request, copy)); }
      return response;
    }).catch(() => caches.match(request)));
  }
});
