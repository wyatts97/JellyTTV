import type {
  Channel,
  ConnectionTest,
  Dashboard,
  Diagnostics,
  JellyfinLibrary,
  Job,
  LogEntry,
  SessionState,
  Settings,
  Vod,
  VodMode,
  VodState,
} from './types'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers:
      init?.body !== undefined ? { 'Content-Type': 'application/json', ...init?.headers } : init?.headers,
    ...init,
  })

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      if (typeof body?.detail === 'string') detail = body.detail
      else if (Array.isArray(body?.detail)) detail = body.detail.map((d: any) => d.msg).join(', ')
    } catch {
      /* keep the status text */
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) return undefined as T
  const text = await response.text()
  return (text ? JSON.parse(text) : undefined) as T
}

const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

export const api = {
  session: () => request<SessionState>('/api/session'),
  login: (username: string, password: string) =>
    post<{ ok: boolean; username: string }>('/api/login', { username, password }),
  logout: () => post<{ ok: boolean }>('/api/logout'),
  setup: (payload: Record<string, unknown>) => post<{ ok: boolean }>('/api/setup', payload),
  changePassword: (username: string, password: string) =>
    post<{ ok: boolean }>('/api/password', { username, password }),

  dashboard: () => request<Dashboard>('/api/dashboard'),
  diagnostics: () => request<Diagnostics>('/api/diagnostics'),
  jobs: (limit = 100) => request<Job[]>(`/api/jobs?limit=${limit}`),
  logs: (limit = 100) => request<LogEntry[]>(`/api/logs?limit=${limit}`),

  settings: () => request<Settings>('/api/settings'),
  saveSettings: (payload: Record<string, unknown>) =>
    request<Settings>('/api/settings', { method: 'PUT', body: JSON.stringify(payload) }),
  rotateTunerToken: () => post<Settings>('/api/settings/rotate-tuner-token'),

  testTwitch: (clientId?: string, clientSecret?: string) => {
    const params = new URLSearchParams()
    if (clientId) params.set('client_id', clientId)
    if (clientSecret) params.set('client_secret', clientSecret)
    return post<ConnectionTest>(`/api/twitch/test?${params}`)
  },
  testJellyfin: (url?: string, apiKey?: string) => {
    const params = new URLSearchParams()
    if (url) params.set('url', url)
    if (apiKey) params.set('api_key', apiKey)
    return post<ConnectionTest>(`/api/jellyfin/test?${params}`)
  },
  jellyfinLibraries: (url?: string, apiKey?: string) => {
    const params = new URLSearchParams()
    if (url) params.set('url', url)
    if (apiKey) params.set('api_key', apiKey)
    return request<JellyfinLibrary[]>(`/api/jellyfin/libraries?${params}`)
  },
  refreshJellyfin: () => post<{ queued: boolean }>('/api/jellyfin/refresh'),
  reconcileEventsub: () => post<{ queued: boolean }>('/api/eventsub/reconcile'),
  eventsubStatus: () => request<Record<string, unknown>>('/api/eventsub/status'),

  channels: () => request<Channel[]>('/api/channels'),
  channel: (id: number) => request<Channel>(`/api/channels/${id}`),
  addChannel: (payload: { channel: string } & Partial<Record<string, unknown>>) =>
    post<Channel>('/api/channels', payload),
  updateChannel: (id: number, payload: Record<string, unknown>) =>
    request<Channel>(`/api/channels/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteChannel: (id: number, deleteFiles: boolean) =>
    request<void>(`/api/channels/${id}?delete_files=${deleteFiles}`, { method: 'DELETE' }),
  syncChannel: (id: number, limit = 20) =>
    post<{ queued: boolean }>(`/api/channels/${id}/sync?limit=${limit}`),
  publishChannel: (id: number) => post<{ queued: boolean }>(`/api/channels/${id}/publish`),
  channelVods: (id: number) => request<Vod[]>(`/api/channels/${id}/vods`),
  channelPreview: (id: number) =>
    request<Record<string, string | null>>(`/api/channels/${id}/preview`),

  vods: (opts: { channelId?: number; state?: VodState[]; limit?: number } = {}) => {
    const params = new URLSearchParams()
    if (opts.channelId) params.set('channel_id', String(opts.channelId))
    for (const s of opts.state ?? []) params.append('state', s)
    params.set('limit', String(opts.limit ?? 200))
    return request<Vod[]>(`/api/vods?${params}`)
  },
  vodSummary: () => request<{ by_state: Record<string, number>; total: number; archived_bytes: number }>(
    '/api/vods/summary',
  ),
  downloadVod: (id: number) => post<{ queued: boolean }>(`/api/vods/${id}/download`),
  retryVod: (id: number) => post<{ queued: boolean }>(`/api/vods/${id}/retry`),
  deleteVodFile: (id: number) => request<{ removed: boolean }>(`/api/vods/${id}/file`, { method: 'DELETE' }),
  skipVod: (id: number) => post<{ ok: boolean }>(`/api/vods/${id}/skip`),
}

export const VOD_MODE_LABELS: Record<VodMode, string> = {
  off: 'No VODs',
  strm: 'Link (.strm)',
  archive: 'Archive to disk',
}
