export type VodMode = 'off' | 'strm' | 'archive'
export type SeasonScheme = 'year' | 'calendar_month'
export type VodState =
  | 'pending'
  | 'queued'
  | 'downloading'
  | 'complete'
  | 'failed'
  | 'skipped'
  | 'purged'
export type JobState = 'queued' | 'running' | 'complete' | 'failed' | 'cancelled'

export interface SessionState {
  setup_complete: boolean
  authenticated: boolean
  username: string | null
}

export interface Settings {
  setup_complete: boolean
  admin_username: string | null
  twitch_client_id: string | null
  twitch_client_secret_set: boolean
  twitch_user_token_set: boolean
  eventsub_enabled: boolean
  eventsub_possible: boolean
  public_base_url: string
  self_base_url: string
  eventsub_callback_url: string | null
  jellyfin_url: string | null
  jellyfin_api_key_set: boolean
  jellyfin_shows_library_id: string | null
  jellyfin_auto_refresh: boolean
  jellyfin_force_guide_refresh: boolean
  notify_on_live: boolean
  notify_title_template: string
  notify_body_template: string
  tuner_token: string | null
  tuner_include_offline: boolean
  proxy_enabled: boolean
  strip_ads: boolean
  proxy_segments: boolean
  twitch_player_type: string
  ad_spoofing: boolean
  default_quality: string
  guide_window_hours: number
  default_vod_mode: VodMode
  default_retention_keep_count: number | null
  default_retention_max_gb: number | null
  default_retention_max_age_days: number | null
  m3u_url: string
  xmltv_url: string
}

export interface Channel {
  id: number
  twitch_login: string
  twitch_user_id: string
  display_name: string
  avatar_url: string | null
  offline_image_url: string | null
  enabled: boolean
  live_enabled: boolean
  vod_mode: VodMode
  quality: string
  season_scheme: SeasonScheme
  series_dir: string
  retention_keep_count: number | null
  retention_max_gb: number | null
  retention_max_age_days: number | null
  is_live: boolean
  live_title: string | null
  live_game: string | null
  live_viewers: number | null
  live_started_at: string | null
  live_thumbnail_url: string | null
  last_vod_sync_at: string | null
  last_error: string | null
  tvg_id: string
  stream_url: string
  library_path: string
  vod_counts: Record<string, number>
}

export interface Vod {
  id: number
  channel_id: number
  channel_login: string | null
  twitch_video_id: string
  title: string
  url: string
  thumbnail_url: string | null
  published_at: string
  duration_s: number | null
  season: number
  episode: number
  mode: VodMode
  state: VodState
  file_path: string | null
  bytes: number | null
  progress: number
  attempts: number
  error: string | null
}

export interface Job {
  id: number
  type: string
  key: string | null
  state: JobState
  progress: number
  message: string | null
  created_at: string
  started_at?: string | null
  finished_at?: string | null
}

export interface LogEntry {
  id: number
  level: string
  category: string
  message: string
  channel_id?: number | null
  created_at: string
}

export interface LiveChannel {
  id: number
  login: string
  display_name: string
  title: string | null
  game: string | null
  viewers: number | null
  started_at: string | null
  // Always a proxy path on our own origin (/api/channels/{id}/...), never a
  // Twitch CDN url and never null: the backend serves generated placeholder
  // artwork rather than failing.
  thumbnail_url: string
  avatar_url: string
}

export interface Dashboard {
  version: string
  channels: { total: number; enabled: number; live: number }
  live: LiveChannel[]
  vods: Record<string, number>
  disk: { total: number; used: number; free: number; library: number }
  jobs: Job[]
  logs: LogEntry[]
  eventsub: {
    enabled: boolean
    possible: boolean
    mode: 'webhook' | 'polling'
    total: number
    by_status: Record<string, number>
    healthy: boolean
  }
  setup: { twitch: boolean; jellyfin: boolean }
}

export interface ConnectionTest {
  ok: boolean
  message: string
  details: Record<string, unknown>
}

export interface JellyfinLibrary {
  id: string
  name: string
  collection_type: string | null
  locations: string[]
}

export interface Diagnostics {
  version: string
  binaries: Record<string, string | null>
  paths: {
    config_dir: string
    media_root: string
    media_root_writable: boolean
    database: string
  }
  disk: { total: number; used: number; free: number; library: number }
  urls: Record<string, string | null>
  eventsub: { total: number; by_status: Record<string, number>; healthy: boolean }
  config: Record<string, number>
}

export interface AppEvent {
  type: string
  at: string
  data: Record<string, unknown>
}
