/**
 * Akshaya Vistara Service Worker — App Shell + Offline Strategy
 *
 * Strategy:
 *   - App shell (HTML skeleton, Bootstrap CSS/JS from CDN) → cache on install
 *   - API/form requests → network first, no caching
 *   - Static assets (CSS, JS, images) → cache first, fallback to network
 *   - Navigation requests → network first, fallback to offline page
 */

const CACHE_VERSION  = "akshaya_vistara-v1";
const OFFLINE_URL    = "/static/offline.html";

// Static assets to pre-cache on install
const PRECACHE_URLS = [
  "/core/dashboard/",
  "/static/manifest.json",
  // Note: Bootstrap/Chart.js come from CDN and are cached by the browser
  // We only pre-cache our own static files here
];

// ── Install: pre-cache critical assets ──────────────────────────────────────
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => {
      console.log("[SW] Pre-caching app shell");
      return cache.addAll(PRECACHE_URLS).catch((err) => {
        // Don't fail install if dashboard isn't accessible (unauthenticated)
        console.warn("[SW] Pre-cache partial failure (expected if not logged in):", err);
      });
    })
  );
  self.skipWaiting();
});

// ── Activate: clean up old caches ───────────────────────────────────────────
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) =>
      Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_VERSION)
          .map((name) => {
            console.log("[SW] Deleting old cache:", name);
            return caches.delete(name);
          })
      )
    )
  );
  self.clients.claim();
});

// ── Fetch: smart routing strategy ───────────────────────────────────────────
self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // 1. Skip non-GET and cross-origin requests
  if (req.method !== "GET" || url.origin !== location.origin) {
    return;
  }

  // 2. Skip admin, API, form endpoints — always network
  if (
    url.pathname.startsWith("/admin/") ||
    url.pathname.startsWith("/accounts/login") ||
    url.pathname.startsWith("/accounts/logout")
  ) {
    return;
  }

  // 3. Static files → cache first, then network
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req).then((response) => {
          if (response && response.status === 200) {
            const clone = response.clone();
            caches.open(CACHE_VERSION).then((cache) => cache.put(req, clone));
          }
          return response;
        });
      })
    );
    return;
  }

  // 4. Media files → network only (don't cache uploaded bills/images)
  if (url.pathname.startsWith("/media/")) {
    return;
  }

  // 5. Navigation (HTML pages) → network first, offline fallback
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((response) => {
          // Cache successful navigations for offline use
          if (response && response.status === 200) {
            const clone = response.clone();
            caches.open(CACHE_VERSION).then((cache) => cache.put(req, clone));
          }
          return response;
        })
        .catch(() => {
          // Offline: serve cached version if available, else offline page
          return caches.match(req).then(
            (cached) => cached || caches.match(OFFLINE_URL) || new Response(
              "<html><body><h2>Akshaya Vistara is offline</h2>"
              + "<p>Please check your connection and try again.</p></body></html>",
              { headers: { "Content-Type": "text/html" } }
            )
          );
        })
    );
    return;
  }
});

// ── Push Notifications (future use) ─────────────────────────────────────────
self.addEventListener("push", (event) => {
  if (!event.data) return;
  const data = event.data.json();
  event.waitUntil(
    self.registration.showNotification(data.title || "Akshaya Vistara", {
      body: data.body || "",
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/icon-192.png",
    })
  );
});
