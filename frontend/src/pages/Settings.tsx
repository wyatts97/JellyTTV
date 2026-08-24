import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Bell, KeyRound, RefreshCw, RotateCcw, Save, Zap } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError, VOD_MODE_LABELS } from '@/lib/api'
import type { JellyfinLibrary, Settings as SettingsData, VodMode } from '@/lib/types'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CopyRow,
  Field,
  Input,
  Select,
  Spinner,
  Toggle,
} from '@/components/ui'
import { formatBytes } from '@/lib/utils'

type Draft = Record<string, unknown>

export default function Settings() {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Draft>({})
  const [secrets, setSecrets] = useState<Record<string, string>>({})
  const [libraries, setLibraries] = useState<JellyfinLibrary[]>([])

  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  const diagnostics = useQuery({ queryKey: ['diagnostics'], queryFn: api.diagnostics })

  useEffect(() => {
    setDraft({})
    setSecrets({})
  }, [settings.dataUpdatedAt])

  const save = useMutation({
    mutationFn: () => api.saveSettings({ ...draft, ...secrets }),
    onSuccess: () => {
      toast.success('Settings saved')
      void queryClient.invalidateQueries({ queryKey: ['settings'] })
      void queryClient.invalidateQueries({ queryKey: ['diagnostics'] })
      void queryClient.invalidateQueries({ queryKey: ['dashboard'] })
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  const rotate = useMutation({
    mutationFn: api.rotateTunerToken,
    onSuccess: () => {
      toast.success('Tuner key rotated — update the URLs in Jellyfin')
      void queryClient.invalidateQueries({ queryKey: ['settings'] })
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  const testTwitch = useMutation({
    mutationFn: () => api.testTwitch(),
    onSuccess: (r) => (r.ok ? toast.success(r.message) : toast.error(r.message)),
    onError: (error: ApiError) => toast.error(error.message),
  })

  const testJellyfin = useMutation({
    mutationFn: async () => {
      const result = await api.testJellyfin()
      if (result.ok) setLibraries(await api.jellyfinLibraries())
      return result
    },
    onSuccess: (r) => (r.ok ? toast.success(r.message) : toast.error(r.message)),
    onError: (error: ApiError) => toast.error(error.message),
  })

  const refreshJellyfin = useMutation({
    mutationFn: api.refreshJellyfin,
    onSuccess: () => toast.success('Library scan requested'),
    onError: (error: ApiError) => toast.error(error.message),
  })

  const reconcile = useMutation({
    mutationFn: api.reconcileEventsub,
    onSuccess: () => toast.success('EventSub reconcile queued'),
    onError: (error: ApiError) => toast.error(error.message),
  })

  const testNotify = useMutation({
    mutationFn: api.testNotification,
    onSuccess: (r) => (r.ok ? toast.success(r.message) : toast.error(r.message)),
    onError: (error: ApiError) => toast.error(error.message),
  })

  if (settings.isLoading || !settings.data) {
    return (
      <div className="grid place-items-center py-24">
        <Spinner className="size-6" />
      </div>
    )
  }

  const current = settings.data
  const get = <K extends keyof SettingsData>(key: K): SettingsData[K] =>
    (draft[key as string] as SettingsData[K]) ?? current[key]
  const set = (key: string, value: unknown) => setDraft((d) => ({ ...d, [key]: value }))
  const setSecret = (key: string, value: string) => setSecrets((s) => ({ ...s, [key]: value }))
  const dirty = Object.keys(draft).length > 0 || Object.keys(secrets).length > 0

  const publicUrl = String(get('public_base_url') ?? '')
  const eventsubPossible = publicUrl.startsWith('https://')

  return (
    <div className="space-y-6 pb-24">
      <div>
        <h1 className="text-lg font-semibold text-white">Settings</h1>
        <p className="mt-1 text-sm text-ink-400">
          Everything here applies immediately after saving. JellyTTV v{diagnostics.data?.version}
        </p>
      </div>

      {/* ------------------------------------------------------ Jellyfin URLs */}
      <Card>
        <CardHeader
          title="Add to Jellyfin"
          description="Paste these into Dashboard → Live TV. The tvg-id values match the guide, so channel mapping is automatic."
          action={
            <Button
              size="sm"
              variant="outline"
              onClick={() => rotate.mutate()}
              loading={rotate.isPending}
            >
              <RotateCcw className="size-3.5" /> Rotate key
            </Button>
          }
        />
        <CardBody className="space-y-4">
          <CopyRow label="Tuner Devices → M3U Tuner" value={current.m3u_url} />
          <CopyRow label="TV Guide Data Providers → XMLTV" value={current.xmltv_url} />
          <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3.5 py-3 text-xs leading-relaxed text-amber-200">
            <AlertTriangle className="mr-1.5 inline size-3.5" aria-hidden />
            In the Jellyfin library that points at{' '}
            <code className="font-mono">{diagnostics.data?.paths.media_root}</code>, enable the{' '}
            <strong>NFO metadata reader</strong> and disable every internet metadata provider —
            otherwise Jellyfin overwrites JellyTTV's metadata with TVDB/TMDB matches.
          </div>
        </CardBody>
      </Card>

      {/* --------------------------------------------------------- Twitch */}
      <Card>
        <CardHeader
          title="Twitch"
          description="App credentials from dev.twitch.tv/console/apps."
          action={
            <Button size="sm" variant="outline" onClick={() => testTwitch.mutate()} loading={testTwitch.isPending}>
              Test
            </Button>
          }
        />
        <CardBody className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Client ID">
              <Input
                value={String(get('twitch_client_id') ?? '')}
                onChange={(e) => set('twitch_client_id', e.target.value)}
              />
            </Field>
            <Field
              label="Client secret"
              hint={current.twitch_client_secret_set ? 'Stored — leave blank to keep it.' : 'Not set.'}
            >
              <Input
                type="password"
                placeholder={current.twitch_client_secret_set ? '••••••••' : ''}
                value={secrets.twitch_client_secret ?? ''}
                onChange={(e) => setSecret('twitch_client_secret', e.target.value)}
              />
            </Field>
          </div>
          <Field
            label="User OAuth token (optional)"
            hint={
              (current.twitch_user_token_set ? 'Stored. ' : '') +
              'Unlocks 1440p/H.265 and reduces stitched ads. Format: the raw token, without the "oauth:" prefix.'
            }
          >
            <Input
              type="password"
              placeholder={current.twitch_user_token_set ? '••••••••' : ''}
              value={secrets.twitch_user_token ?? ''}
              onChange={(e) => setSecret('twitch_user_token', e.target.value)}
            />
          </Field>
        </CardBody>
      </Card>

      {/* -------------------------------------------------------- Jellyfin */}
      <Card>
        <CardHeader
          title="Jellyfin"
          description="Used to trigger library scans after JellyTTV writes new episodes."
          action={
            <div className="flex gap-2">
              <Button size="sm" variant="outline" onClick={() => testJellyfin.mutate()} loading={testJellyfin.isPending}>
                Test
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => refreshJellyfin.mutate()}
                loading={refreshJellyfin.isPending}
              >
                <RefreshCw className="size-3.5" /> Scan now
              </Button>
            </div>
          }
        />
        <CardBody className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Jellyfin URL">
              <Input
                placeholder="http://jellyfin:8096"
                value={String(get('jellyfin_url') ?? '')}
                onChange={(e) => set('jellyfin_url', e.target.value)}
              />
            </Field>
            <Field
              label="API key"
              hint={current.jellyfin_api_key_set ? 'Stored — leave blank to keep it.' : 'Not set.'}
            >
              <Input
                type="password"
                placeholder={current.jellyfin_api_key_set ? '••••••••' : ''}
                value={secrets.jellyfin_api_key ?? ''}
                onChange={(e) => setSecret('jellyfin_api_key', e.target.value)}
              />
            </Field>
          </div>
          {libraries.length > 0 && (
            <Field label="Shows library" hint="Refreshing one library is much cheaper than a full scan.">
              <Select
                value={String(get('jellyfin_shows_library_id') ?? '')}
                onChange={(e) => set('jellyfin_shows_library_id', e.target.value)}
              >
                <option value="">Full library scan</option>
                {libraries.map((lib) => (
                  <option key={lib.id} value={lib.id}>
                    {lib.name}
                    {lib.collection_type ? ` (${lib.collection_type})` : ''}
                  </option>
                ))}
              </Select>
            </Field>
          )}
          <Toggle
            checked={Boolean(get('jellyfin_auto_refresh'))}
            onChange={(v) => set('jellyfin_auto_refresh', v)}
            label="Refresh Jellyfin automatically"
            description="Debounced by 60 seconds after a library write."
          />
        </CardBody>
      </Card>

      {/* ------------------------------------------------- URLs / EventSub */}
      <Card>
        <CardHeader
          title="URLs & go-live detection"
          action={
            <Button size="sm" variant="outline" onClick={() => reconcile.mutate()} loading={reconcile.isPending}>
              <Zap className="size-3.5" /> Reconcile
            </Button>
          }
        />
        <CardBody className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="JellyTTV base URL (as Jellyfin sees it)"
              hint="Embedded in the M3U and every .strm file. Changing it rewrites them all."
            >
              <Input
                placeholder="http://192.168.1.10:8730"
                value={String(get('self_base_url') ?? '')}
                onChange={(e) => set('self_base_url', e.target.value)}
              />
            </Field>
            <Field label="Public HTTPS URL" hint="Required for EventSub webhooks (Twitch needs port 443 + valid TLS).">
              <Input
                placeholder="https://jellyttv.example.com"
                value={publicUrl}
                onChange={(e) => set('public_base_url', e.target.value)}
              />
            </Field>
          </div>
          <div className="flex items-center gap-2">
            <Badge tone={current.eventsub_enabled && current.eventsub_possible ? 'success' : 'info'}>
              {current.eventsub_enabled && current.eventsub_possible
                ? 'Webhook mode'
                : 'Polling mode (every 2 min)'}
            </Badge>
            {current.eventsub_callback_url && (
              <code className="truncate font-mono text-[11px] text-ink-400">
                {current.eventsub_callback_url}
              </code>
            )}
          </div>
          <Toggle
            checked={Boolean(get('eventsub_enabled')) && eventsubPossible}
            disabled={!eventsubPossible}
            onChange={(v) => set('eventsub_enabled', v)}
            label="Use EventSub webhooks"
            description={
              eventsubPossible
                ? 'Instant go-live detection. Polling stays on as a safety net.'
                : 'Set a public https:// URL above to enable this.'
            }
          />
        </CardBody>
      </Card>

      {/* ------------------------------------------------- Notifications */}
      <Card>
        <CardHeader
          title="Go-live notifications"
          description="Push a notification when a tracked channel starts streaming."
          action={
            <Button
              size="sm"
              variant="outline"
              onClick={() => testNotify.mutate()}
              loading={testNotify.isPending}
            >
              <Bell className="size-3.5" /> Send test
            </Button>
          }
        />
        <CardBody className="space-y-4">
          <Toggle
            checked={Boolean(get('notify_on_live'))}
            onChange={(v) => set('notify_on_live', v)}
            label="Notify when a channel goes live"
            description="Delivered through the Streamyfin companion plugin on your Jellyfin server. Jellyfin's own web app cannot receive push notifications."
          />
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="Title"
              hint="Placeholders: {display_name} {login} {title} {game} {viewers}"
            >
              <Input
                placeholder="{display_name} is live"
                value={String(get('notify_title_template') ?? '')}
                onChange={(e) => set('notify_title_template', e.target.value)}
              />
            </Field>
            <Field label="Body" hint="Falls back to the category if this renders empty.">
              <Input
                placeholder="{title}"
                value={String(get('notify_body_template') ?? '')}
                onChange={(e) => set('notify_body_template', e.target.value)}
              />
            </Field>
          </div>
        </CardBody>
      </Card>

      {/* --------------------------------------------- Streaming behaviour */}
      <Card>
        <CardHeader title="Streaming" description="How stream bytes get from Twitch to Jellyfin." />
        <CardBody className="space-y-1">
          <Toggle
            checked={Boolean(get('proxy_enabled'))}
            onChange={(v) => set('proxy_enabled', v)}
            label="Proxy playlists through JellyTTV"
            description="Required for ad stripping. Turning this off makes JellyTTV redirect Jellyfin straight to Twitch."
          />
          <Toggle
            checked={Boolean(get('strip_ads'))}
            disabled={!get('proxy_enabled')}
            onChange={(v) => set('strip_ads', v)}
            label="Strip stitched ad segments"
            description="Removes segments inside twitch-stitched-ad date ranges and inserts a discontinuity."
          />
          <Toggle
            checked={Boolean(get('proxy_segments'))}
            disabled={!get('proxy_enabled')}
            onChange={(v) => set('proxy_segments', v)}
            label="Proxy video segments too"
            description="Off (recommended) redirects segments to Twitch's CDN. Turn on only if the Jellyfin host cannot reach Twitch directly — it costs bandwidth and CPU."
          />
          <Toggle
            checked={Boolean(get('tuner_include_offline'))}
            onChange={(v) => set('tuner_include_offline', v)}
            label="Keep offline channels in the tuner"
            description="Recommended: Jellyfin keys channels by id, so removing them churns its database and loses favourites."
          />
          <div className="grid gap-4 pt-3 sm:grid-cols-2">
            <Field label="Default stream quality">
              <Select
                value={String(get('default_quality') ?? 'best')}
                onChange={(e) => set('default_quality', e.target.value)}
              >
                {['best', '1080p', '720p', '480p', '360p', 'worst'].map((q) => (
                  <option key={q} value={q}>
                    {q}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Guide window" hint="Hours of programme data in the XMLTV output.">
              <Input
                type="number"
                min={6}
                max={336}
                value={Number(get('guide_window_hours') ?? 48)}
                onChange={(e) => set('guide_window_hours', Number(e.target.value))}
              />
            </Field>
          </div>
        </CardBody>
      </Card>

      {/* ------------------------------------------------- Channel defaults */}
      <Card>
        <CardHeader title="Defaults for new channels" description="Existing channels keep their own settings." />
        <CardBody className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="VOD handling">
            <Select
              value={String(get('default_vod_mode') ?? 'strm')}
              onChange={(e) => set('default_vod_mode', e.target.value as VodMode)}
            >
              <option value="off">{VOD_MODE_LABELS.off}</option>
              <option value="strm">{VOD_MODE_LABELS.strm}</option>
              <option value="archive">{VOD_MODE_LABELS.archive}</option>
            </Select>
          </Field>
          <Field label="Keep newest" hint="episodes (blank = unlimited)">
            <Input
              type="number"
              min={0}
              value={String(get('default_retention_keep_count') ?? '')}
              onChange={(e) =>
                set('default_retention_keep_count', e.target.value === '' ? null : Number(e.target.value))
              }
            />
          </Field>
          <Field label="Max size" hint="GB per channel">
            <Input
              type="number"
              min={0}
              step="0.5"
              value={String(get('default_retention_max_gb') ?? '')}
              onChange={(e) =>
                set('default_retention_max_gb', e.target.value === '' ? null : Number(e.target.value))
              }
            />
          </Field>
          <Field label="Max age" hint="days">
            <Input
              type="number"
              min={0}
              value={String(get('default_retention_max_age_days') ?? '')}
              onChange={(e) =>
                set('default_retention_max_age_days', e.target.value === '' ? null : Number(e.target.value))
              }
            />
          </Field>
        </CardBody>
      </Card>

      {/* ------------------------------------------------------ Diagnostics */}
      <Card>
        <CardHeader title="Diagnostics" description="Paste this into a bug report." />
        <CardBody>
          {diagnostics.data ? (
            <dl className="grid gap-4 text-xs sm:grid-cols-2">
              <Diag label="streamlink" value={diagnostics.data.binaries.streamlink ?? 'missing'} />
              <Diag label="yt-dlp" value={diagnostics.data.binaries['yt-dlp'] ?? 'missing'} />
              <Diag label="ffmpeg" value={diagnostics.data.binaries.ffmpeg ?? 'missing'} />
              <Diag
                label="Media root"
                value={`${diagnostics.data.paths.media_root} ${
                  diagnostics.data.paths.media_root_writable ? '(writable)' : '(NOT WRITABLE)'
                }`}
              />
              <Diag label="Database" value={diagnostics.data.paths.database} />
              <Diag
                label="Disk"
                value={`${formatBytes(diagnostics.data.disk.library)} library · ${formatBytes(
                  diagnostics.data.disk.free,
                )} free`}
              />
              <Diag
                label="EventSub subscriptions"
                value={`${diagnostics.data.eventsub.total} (${
                  diagnostics.data.eventsub.healthy ? 'healthy' : 'check status'
                })`}
              />
              <Diag
                label="Max concurrent downloads"
                value={String(diagnostics.data.config.max_concurrent_downloads)}
              />
            </dl>
          ) : (
            <Spinner />
          )}
        </CardBody>
      </Card>

      <ChangePassword username={current.admin_username ?? 'admin'} />

      {dirty && (
        <div className="fixed inset-x-0 bottom-0 z-30 border-t border-ink-700 bg-ink-900/95 px-4 py-3 backdrop-blur lg:pl-60">
          <div className="mx-auto flex max-w-7xl items-center justify-between gap-4">
            <p className="text-xs text-ink-400">You have unsaved changes.</p>
            <div className="flex gap-2">
              <Button
                variant="ghost"
                onClick={() => {
                  setDraft({})
                  setSecrets({})
                }}
              >
                Discard
              </Button>
              <Button variant="primary" onClick={() => save.mutate()} loading={save.isPending}>
                <Save className="size-4" /> Save changes
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function Diag({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-ink-400">{label}</dt>
      <dd className="mt-0.5 truncate font-mono text-ink-200" title={value}>
        {value}
      </dd>
    </div>
  )
}

function ChangePassword({ username }: { username: string }) {
  const [name, setName] = useState(username)
  const [password, setPassword] = useState('')

  const change = useMutation({
    mutationFn: () => api.changePassword(name, password),
    onSuccess: () => {
      toast.success('Credentials updated')
      setPassword('')
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  return (
    <Card>
      <CardHeader title="Admin account" />
      <CardBody className="grid items-end gap-4 sm:grid-cols-3">
        <Field label="Username">
          <Input value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="New password" hint="At least 8 characters.">
          <Input
            type="password"
            value={password}
            autoComplete="new-password"
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <Button
          variant="secondary"
          onClick={() => change.mutate()}
          loading={change.isPending}
          disabled={password.length < 8}
        >
          <KeyRound className="size-4" /> Update
        </Button>
      </CardBody>
    </Card>
  )
}
