/**
 * Per-device UI preferences.
 *
 * Sidebar state, player volume, chat width: things that should survive a
 * reload but mean nothing on the server. Every access is wrapped, because
 * `localStorage` throws in private windows and when site data is blocked - and
 * a blocked preference must never take the UI down with it.
 */

const PREFIX = 'jellyttv.'

export const PREF = {
  sidebar: `${PREFIX}ui.sidebar`,
  chatWidth: `${PREFIX}player.chatWidth`,
  chat: `${PREFIX}player.chat`,
  mode: `${PREFIX}player.mode`,
  muted: `${PREFIX}player.muted`,
  volume: `${PREFIX}player.volume`,
} as const

export function readPref<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key)
    if (raw === null) return fallback
    try {
      return JSON.parse(raw) as T
    } catch {
      // Written before these values were stored as JSON: a bare string like
      // `bridged`. Honour it rather than silently resetting the preference.
      return typeof fallback === 'string' ? (raw as unknown as T) : fallback
    }
  } catch {
    return fallback
  }
}

export function writePref(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    /* a preference, not state - losing it is not worth an error */
  }
}

/** Read a preference constrained to a known set, falling back when it is not one. */
export function readEnumPref<T extends string>(key: string, fallback: T, allowed: readonly T[]): T {
  const value = readPref<T>(key, fallback)
  return allowed.includes(value) ? value : fallback
}
