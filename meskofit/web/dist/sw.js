/* py123 service worker — keeps the app and your plan usable when the gym
   has no signal. Workout sets are saved on the phone and synced later by the
   app itself; this worker only caches the app shell and a few read requests. */
const VERSION = "muvmq26d";
const SHELL = "mf-shell-" + VERSION;
const DATA = "mf-data";
const IMG = "mf-img";

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL);
      const files = new Set(["/", "/manifest.webmanifest", "/favicon.svg", "/apple-touch-icon.png"]);
      try {
        const m = await (await fetch("/.vite/manifest.json", { cache: "no-store" })).json();
        for (const entry of Object.values(m)) {
          if (entry.file) files.add("/" + entry.file);
          (entry.css || []).forEach((f) => files.add("/" + f));
          (entry.assets || []).forEach((f) => files.add("/" + f));
        }
      } catch (e) {
        /* manifest missing: runtime caching still works */
      }
      await cache.addAll([...files]).catch(() => {});
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const k of await caches.keys()) if (k.startsWith("mf-shell-") && k !== SHELL) await caches.delete(k);
      await self.clients.claim();
    })(),
  );
});

const OFFLINE_API = [/^\/api\/bootstrap$/, /^\/api\/catalog$/, /^\/api\/body$/, /^\/api\/sessions(\/active)?$/, /^\/api\/history\//, /^\/api\/log\//];

async function networkFirst(req, cacheName, fallbackUrl) {
  const cache = await caches.open(cacheName);
  try {
    const res = await Promise.race([fetch(req), new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 6000))]);
    if (res && res.ok) cache.put(fallbackUrl || req, res.clone());
    return res;
  } catch (e) {
    const hit = (await cache.match(req)) || (fallbackUrl && (await caches.match(fallbackUrl)));
    if (hit) return hit;
    throw e;
  }
}

async function cacheFirst(req, cacheName) {
  const hit = await caches.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok) (await caches.open(cacheName)).put(req, res.clone());
  return res;
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;
  const p = url.pathname;
  if (p.startsWith("/api/")) {
    if (OFFLINE_API.some((re) => re.test(p))) event.respondWith(networkFirst(req, DATA));
    return;
  }
  if (p.startsWith("/assets/")) return event.respondWith(cacheFirst(req, SHELL));
  if (p.startsWith("/img/ex/")) return event.respondWith(cacheFirst(req, IMG));
  if (req.mode === "navigate") return event.respondWith(networkFirst(req, SHELL, "/"));
});
