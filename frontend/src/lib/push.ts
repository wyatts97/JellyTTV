import { api } from './api'

/**
 * Browser side of go-live Web Push.
 *
 * Web Push needs a secure context (https, or localhost), a service worker, and
 * a permission prompt that the browser only shows in response to a click. On
 * iPhone and iPad it additionally only exists inside a PWA that was added to
 * the home screen - a Safari tab has no `PushManager` at all.
 */

export type PushState =
  | 'unsupported' // no service worker / PushManager in this browser
  | 'insecure' // http on something other than localhost
  | 'ios-needs-install' // iOS Safari tab: add to home screen first
  | 'denied' // the user blocked notifications for this site
  | 'subscribed'
  | 'unsubscribed'

export function isIos(): boolean {
  return /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
}

export function isStandalone(): boolean {
  return (
    window.matchMedia?.('(display-mode: standalone)').matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  )
}

function base64UrlToBytes(value: string): Uint8Array<ArrayBuffer> {
  const padded = value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (value.length % 4)) % 4)
  const raw = atob(padded)
  const bytes = new Uint8Array(new ArrayBuffer(raw.length))
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i)
  return bytes
}

function deviceLabel(): string {
  const ua = navigator.userAgent
  const browser = /Edg\//.test(ua)
    ? 'Edge'
    : /Firefox\//.test(ua)
      ? 'Firefox'
      : /Chrome\//.test(ua)
        ? 'Chrome'
        : /Safari\//.test(ua)
          ? 'Safari'
          : 'Browser'
  const os = /Android/.test(ua)
    ? 'Android'
    : isIos()
      ? 'iOS'
      : /Windows/.test(ua)
        ? 'Windows'
        : /Mac OS X/.test(ua)
          ? 'macOS'
          : /Linux/.test(ua)
            ? 'Linux'
            : 'unknown OS'
  return `${browser} on ${os}${isStandalone() ? ' (app)' : ''}`
}

async function registration(): Promise<ServiceWorkerRegistration | null> {
  if (!('serviceWorker' in navigator)) return null
  // `ready` never settles when nothing is registered, so check first; once a
  // registration exists, `ready` waits for its worker to become active, which
  // `pushManager.subscribe` requires.
  if (!(await navigator.serviceWorker.getRegistration('/'))) return null
  return navigator.serviceWorker.ready
}

export async function currentSubscription(): Promise<PushSubscription | null> {
  const reg = await registration()
  return (await reg?.pushManager?.getSubscription()) ?? null
}

export async function pushState(): Promise<PushState> {
  if (!window.isSecureContext) return 'insecure'
  if (!('serviceWorker' in navigator)) return 'unsupported'
  if (!('PushManager' in window)) return isIos() && !isStandalone() ? 'ios-needs-install' : 'unsupported'
  if (Notification.permission === 'denied') return 'denied'
  return (await currentSubscription()) ? 'subscribed' : 'unsubscribed'
}

/** Must be called from a click handler: browsers only prompt on a user gesture. */
export async function subscribe(): Promise<void> {
  const permission = await Notification.requestPermission()
  if (permission !== 'granted') throw new Error('Notifications were not allowed')

  const reg = await registration()
  if (!reg) throw new Error('Service worker is not available')
  const { public_key } = await api.pushKey()
  if (!public_key) throw new Error('The server has no push key yet')

  const key = base64UrlToBytes(public_key)
  let subscription = await reg.pushManager.getSubscription()
  if (subscription) {
    // A subscription made against a different server key cannot receive our
    // pushes; replace it rather than registering a dead endpoint.
    const existing = subscription.options.applicationServerKey
    const same =
      existing !== null &&
      existing.byteLength === key.byteLength &&
      new Uint8Array(existing).every((b, i) => b === key[i])
    if (!same) {
      await subscription.unsubscribe()
      subscription = null
    }
  }
  subscription ??= await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key })

  const json = subscription.toJSON()
  if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) {
    throw new Error('The browser returned an incomplete subscription')
  }
  await api.pushSubscribe({
    endpoint: json.endpoint,
    keys: { p256dh: json.keys.p256dh, auth: json.keys.auth },
    label: deviceLabel(),
  })
}

export async function unsubscribe(): Promise<void> {
  const subscription = await currentSubscription()
  if (!subscription) return
  const endpoint = subscription.endpoint
  await subscription.unsubscribe()
  await api.pushUnsubscribe({ endpoint })
}

export function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {
      /* offline-capable shell and push are enhancements; the app works without */
    })
  })
  // Sent by the worker when a notification is tapped and this window could not
  // be navigated directly.
  navigator.serviceWorker.addEventListener('message', (event) => {
    const data = event.data as { type?: string; url?: string } | null
    if (data?.type === 'navigate' && data.url) {
      const url = new URL(data.url, window.location.origin)
      if (url.origin === window.location.origin) window.location.assign(url.href)
    }
  })
}
