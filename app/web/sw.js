const CACHE_NAME = "budget-pwa-v6";
const SHELL_ASSETS = [
  "/",
  "/static/styles.css",
  "/static/app.js",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)),
    )),
  );
  self.clients.claim();
});

// Network-first for the app shell so a deploy lands without users having to
// "hard refresh". The pre-cached copy is used only when the network fails
// (offline). API calls bypass the SW entirely so auth headers + cookies
// don't get accidentally cached.
const APP_SHELL_PATHS = new Set([
  "/",
  "/static/styles.css",
  "/static/app.js",
  "/manifest.webmanifest",
]);

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) {
    return;
  }
  // Only intercept the explicit app shell. Everything else (API calls,
  // /me, /chat, /transactions, /session, etc.) goes straight to the network
  // — caching a stale auth response would be a real bug.
  if (!APP_SHELL_PATHS.has(url.pathname)) {
    return;
  }
  event.respondWith(
    (async () => {
      try {
        const fresh = await fetch(event.request);
        // Update cache opportunistically so an offline reload still works.
        const cache = await caches.open(CACHE_NAME);
        cache.put(event.request, fresh.clone()).catch(() => {});
        return fresh;
      } catch (_) {
        const cached = await caches.match(event.request);
        return cached || caches.match("/");
      }
    })(),
  );
});

self.addEventListener("push", (event) => {
  const data = event.data ? event.data.json() : {};
  const title = data.title || "Budget check-in";
  const options = {
    body: data.body || "Have you entered today's expenses?",
    icon: "/static/icon.svg",
    badge: "/static/icon.svg",
    data: { url: data.url || "/#add" },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data?.url || "/#add";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ("focus" in client) {
          client.navigate(url);
          return client.focus();
        }
      }
      return self.clients.openWindow(url);
    }),
  );
});
