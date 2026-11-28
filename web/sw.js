const CACHE_NAME = "comfinder-pwa-v4";
const PRECACHE = [
  "/comfinder/manifest.json",
  "/comfinder/favicon.ico",
  "/comfinder/icon.svg",
  "/comfinder/icon-32.png",
  "/comfinder/icon-192.png",
  "/comfinder/icon-512.png",
  "/comfinder/apple-touch-icon.png",
  "/comfinder/fonts/Iosevka-Regular.woff2",
  "/comfinder/fonts/Iosevka-Bold.woff2"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/comfinder/api/") || url.pathname === "/comfinder/ws") {
    return;
  }
  if (req.mode === "navigate" || url.pathname === "/comfinder/" || url.pathname === "/comfinder") {
    event.respondWith(
      fetch(req, { cache: "no-store" })
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((c) => c.put(req, copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }
  if (PRECACHE.includes(url.pathname)) {
    event.respondWith(
      caches.match(req).then((cached) => {
        const net = fetch(req).then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((c) => c.put(req, copy)).catch(() => {});
          }
          return res;
        }).catch(() => cached);
        return cached || net;
      })
    );
  }
});
