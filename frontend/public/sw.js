/* JellyTTV service worker: installable app shell + go-live Web Push.
 *
 * Handwritten on purpose - no build plugin - so what it caches and what it
 * leaves alone is explicit. Streams, playlists and the API are never touched:
 * a cached playlist is a frozen stream, and a cached API answer is a lie.
 */

const VERSION = 'v1'
const SHELL_CACHE = `jellyttv-shell-${VERSION}`
const ASSET_CACHE = `jellyttv-assets-${VERSION}`
const SHELL = ['/', '/manifest.webmanifest', '/favicon.svg', '/icon-192.png', '/icon-512.png']

// Never intercepted: live data, streams and the tuner.
const PASSTHROUGH = ['/api/', '/hls/', '/stream/', '/tuner/', '/vod/', '/eventsub/']

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL))
      .catch(() => undefined)
      .then(() => self.skipWaiting()),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key.startsWith('jellyttv-') && key !== SHELL_CACHE && key !== ASSET_CACHE)
            .map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (event) => {
  const request = event.request
  if (request.method !== 'GET') return
  const url = new URL(request.url)
  if (url.origin !== self.location.origin) return
  if (PASSTHROUGH.some((prefix) => url.pathname.startsWith(prefix))) return

  // Hashed build output never changes under a given name: cache-first.
  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(
      caches.open(ASSET_CACHE).then(async (cache) => {
        const hit = await cache.match(request)
        if (hit) return hit
        const response = await fetch(request)
        if (response.ok) cache.put(request, response.clone())
        return response
      }),
    )
    return
  }

  // Navigations: always try the network (so a new build is picked up at once),
  // falling back to the cached shell when offline.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone()
            caches.open(SHELL_CACHE).then((cache) => cache.put('/', copy))
          }
          return response
        })
        .catch(async () => (await caches.match('/')) || Response.error()),
    )
  }
})

self.addEventListener('push', (event) => {
  let data = {}
  try {
    data = event.data ? event.data.json() : {}
  } catch {
    data = { title: 'JellyTTV', body: event.data ? event.data.text() : '' }
  }
  const title = data.title || 'JellyTTV'
  const options = {
    body: data.body || '',
    icon: data.icon || '/icon-192.png',
    badge: '/icon-192.png',
    image: data.image || undefined,
    tag: data.tag || undefined,
    renotify: Boolean(data.tag),
    data: { url: data.url || '/' },
  }
  event.waitUntil(self.registration.showNotification(title, options))
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const target = new URL(event.notification.data?.url || '/', self.location.origin).href
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async (windows) => {
      for (const client of windows) {
        if (new URL(client.url).origin !== self.location.origin) continue
        await client.focus()
        if ('navigate' in client) {
          try {
            await client.navigate(target)
          } catch {
            /* uncontrolled client: fall through to postMessage */
            client.postMessage({ type: 'navigate', url: target })
          }
        }
        return
      }
      await self.clients.openWindow(target)
    }),
  )
})

// The push service rotated this browser's subscription: re-subscribe with the
// same server key and tell the backend, so go-live pushes keep arriving.
self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil(
    (async () => {
      const key = event.oldSubscription?.options?.applicationServerKey
      if (!key) return
      const subscription = await self.registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: key,
      })
      const json = subscription.toJSON()
      await fetch('/api/push/subscriptions', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys }),
      })
    })(),
  )
})
