import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Archive,
  FolderTree,
  Link2,
  Plus,
  RefreshCw,
  Trash2,
  Tv,
  Users,
} from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError, VOD_MODE_LABELS } from '@/lib/api'
import type { Channel, SeasonScheme, VodMode } from '@/lib/types'
import {
  Badge,
  Button,
  Card,
  CardBody,
  EmptyState,
  Field,
  Input,
  LiveBadge,
  Modal,
  Select,
  Spinner,
  Toggle,
} from '@/components/ui'
import { formatBytes, formatNumber, formatRelative, formatUptime } from '@/lib/utils'

const QUALITIES = ['best', '1080p', '720p', '480p', '360p', 'worst']

export default function Channels() {
  const queryClient = useQueryClient()
  const [addOpen, setAddOpen] = useState(false)
  const [editing, setEditing] = useState<Channel | null>(null)

  const { data: channels, isLoading } = useQuery({
    queryKey: ['channels'],
    queryFn: api.channels,
    refetchInterval: 60_000,
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['channels'] })
    void queryClient.invalidateQueries({ queryKey: ['dashboard'] })
  }

  const sync = useMutation({
    mutationFn: (id: number) => api.syncChannel(id),
    onSuccess: () => toast.success('VOD sync queued'),
    onError: (error: ApiError) => toast.error(error.message),
  })

  const publish = useMutation({
    mutationFn: (id: number) => api.publishChannel(id),
    onSuccess: () => toast.success('Library write queued'),
    onError: (error: ApiError) => toast.error(error.message),
  })

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-white">Channels</h1>
          <p className="mt-1 text-sm text-ink-400">
            Each channel becomes a Live TV channel and a Jellyfin series.
          </p>
        </div>
        <Button variant="primary" onClick={() => setAddOpen(true)}>
          <Plus className="size-4" /> Add channel
        </Button>
      </div>

      {isLoading ? (
        <div className="grid place-items-center py-20">
          <Spinner className="size-6" />
        </div>
      ) : !channels || channels.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Tv className="size-7" />}
            title="No channels yet"
            description="Add a Twitch channel by name or URL. JellyTTV will publish it to the Live TV guide and start tracking its VODs."
            action={
              <Button variant="primary" onClick={() => setAddOpen(true)}>
                <Plus className="size-4" /> Add your first channel
              </Button>
            }
          />
        </Card>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {channels.map((channel) => (
            <ChannelCard
              key={channel.id}
              channel={channel}
              onEdit={() => setEditing(channel)}
              onSync={() => sync.mutate(channel.id)}
              onPublish={() => publish.mutate(channel.id)}
              busy={sync.isPending || publish.isPending}
            />
          ))}
        </div>
      )}

      <AddChannelModal open={addOpen} onClose={() => setAddOpen(false)} onDone={invalidate} />
      {editing && (
        <EditChannelModal
          key={editing.id}
          channel={editing}
          onClose={() => setEditing(null)}
          onDone={invalidate}
        />
      )}
    </div>
  )
}

function ChannelCard({
  channel,
  onEdit,
  onSync,
  onPublish,
  busy,
}: {
  channel: Channel
  onEdit: () => void
  onSync: () => void
  onPublish: () => void
  busy: boolean
}) {
  const counts = channel.vod_counts
  return (
    <Card>
      <CardBody className="space-y-4">
        <div className="flex items-start gap-3">
          {channel.avatar_url ? (
            <img src={channel.avatar_url} alt="" className="size-11 rounded-full" loading="lazy" />
          ) : (
            <div className="grid size-11 place-items-center rounded-full bg-ink-700 text-ink-300">
              <Tv className="size-5" aria-hidden />
            </div>
          )}
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h3 className="min-w-0 truncate text-sm font-semibold text-white">
                {channel.display_name}
              </h3>
              {channel.is_live && <LiveBadge />}
              {!channel.enabled && <Badge tone="warning">Disabled</Badge>}
            </div>
            <p className="truncate text-xs text-ink-400">
              twitch.tv/{channel.twitch_login} · {channel.tvg_id}
            </p>
          </div>
        </div>

        {channel.is_live && (
          <div className="rounded-lg border border-ink-700 bg-ink-850 p-3">
            <p className="line-clamp-2 text-xs text-ink-200">{channel.live_title}</p>
            <div className="mt-1.5 flex items-center gap-3 text-[11px] text-ink-400">
              {channel.live_game && <span className="truncate">{channel.live_game}</span>}
              <span className="flex items-center gap-1">
                <Users className="size-3" aria-hidden /> {formatNumber(channel.live_viewers)}
              </span>
              <span>{formatUptime(channel.live_started_at)}</span>
            </div>
          </div>
        )}

        <dl className="grid grid-cols-2 gap-3 text-xs">
          <Detail
            label="VOD mode"
            value={
              <span className="flex items-center gap-1.5">
                {channel.vod_mode === 'archive' ? (
                  <Archive className="size-3.5" aria-hidden />
                ) : channel.vod_mode === 'strm' ? (
                  <Link2 className="size-3.5" aria-hidden />
                ) : null}
                {VOD_MODE_LABELS[channel.vod_mode]}
              </span>
            }
          />
          <Detail label="Quality" value={channel.quality} />
          <Detail
            label="Episodes"
            value={`${formatNumber(counts.complete ?? 0)} of ${formatNumber(counts.total ?? 0)}`}
          />
          <Detail label="On disk" value={formatBytes(counts.archived_bytes ?? 0)} />
          <Detail label="Last sync" value={formatRelative(channel.last_vod_sync_at)} />
          <Detail
            label="Live in guide"
            value={channel.live_enabled ? 'Yes' : 'Hidden'}
          />
        </dl>

        <div className="flex items-center gap-1.5 rounded-lg border border-ink-700 bg-ink-850 px-3 py-2">
          <FolderTree className="size-3.5 shrink-0 text-ink-400" aria-hidden />
          <code className="min-w-0 truncate font-mono text-[11px] text-ink-300">
            {channel.library_path}
          </code>
        </div>

        {channel.last_error && (
          <p className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
            {channel.last_error}
          </p>
        )}

        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" onClick={onSync} disabled={busy}>
            <RefreshCw className="size-3.5" /> Sync VODs
          </Button>
          <Button size="sm" variant="outline" onClick={onPublish} disabled={busy}>
            <FolderTree className="size-3.5" /> Rewrite library
          </Button>
          <Button size="sm" variant="secondary" onClick={onEdit}>
            Configure
          </Button>
        </div>
      </CardBody>
    </Card>
  )
}

function Detail({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="text-ink-400">{label}</dt>
      <dd className="mt-0.5 text-ink-200">{value}</dd>
    </div>
  )
}

function AddChannelModal({
  open,
  onClose,
  onDone,
}: {
  open: boolean
  onClose: () => void
  onDone: () => void
}) {
  const [value, setValue] = useState('')
  const [vodMode, setVodMode] = useState<VodMode | ''>('')

  const add = useMutation({
    mutationFn: () =>
      api.addChannel({ channel: value, ...(vodMode ? { vod_mode: vodMode } : {}) }),
    onSuccess: (channel) => {
      toast.success(`Added ${channel.display_name}`)
      setValue('')
      setVodMode('')
      onDone()
      onClose()
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Add a Twitch channel"
      description="Paste a channel name or any twitch.tv URL."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={() => add.mutate()}
            loading={add.isPending}
            disabled={!value.trim()}
          >
            Add channel
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Channel" hint="e.g. shroud or https://www.twitch.tv/shroud">
          <Input
            value={value}
            autoFocus
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && value.trim()) add.mutate()
            }}
          />
        </Field>
        <Field label="VOD handling" hint="Leave as default to use the global setting.">
          <Select value={vodMode} onChange={(e) => setVodMode(e.target.value as VodMode | '')}>
            <option value="">Use default</option>
            <option value="strm">{VOD_MODE_LABELS.strm} — no disk usage</option>
            <option value="archive">{VOD_MODE_LABELS.archive} — full download</option>
            <option value="off">{VOD_MODE_LABELS.off} — live only</option>
          </Select>
        </Field>
      </div>
    </Modal>
  )
}

function EditChannelModal({
  channel,
  onClose,
  onDone,
}: {
  channel: Channel
  onClose: () => void
  onDone: () => void
}) {
  // Keyed by channel id at the call site, so plain initial state is enough.
  const [draft, setDraft] = useState<Channel>(channel)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const save = useMutation({
    mutationFn: () =>
      api.updateChannel(draft.id, {
        display_name: draft.display_name,
        series_dir: draft.series_dir,
        enabled: draft.enabled,
        live_enabled: draft.live_enabled,
        vod_mode: draft.vod_mode,
        quality: draft.quality,
        season_scheme: draft.season_scheme,
        retention_keep_count: draft.retention_keep_count,
        retention_max_gb: draft.retention_max_gb,
        retention_max_age_days: draft.retention_max_age_days,
      }),
    onSuccess: () => {
      toast.success('Channel updated')
      onDone()
      onClose()
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  const remove = useMutation({
    mutationFn: () => api.deleteChannel(channel.id, true),
    onSuccess: () => {
      toast.success('Channel removed')
      onDone()
      onClose()
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  const patch = (values: Partial<Channel>) => setDraft({ ...draft, ...values })
  const numberOrNull = (raw: string) => (raw === '' ? null : Number(raw))

  return (
    <Modal
      open
      wide
      onClose={onClose}
      title={`Configure ${channel.display_name}`}
      description={`twitch.tv/${channel.twitch_login}`}
      footer={
        <>
          <Button
            variant="danger"
            onClick={() => (confirmDelete ? remove.mutate() : setConfirmDelete(true))}
            loading={remove.isPending}
            className="mr-auto"
          >
            <Trash2 className="size-4" />
            {confirmDelete ? 'Really delete (incl. files)' : 'Remove channel'}
          </Button>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => save.mutate()} loading={save.isPending}>
            Save changes
          </Button>
        </>
      }
    >
      <div className="space-y-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Display name" hint="Used as the Jellyfin series title.">
            <Input
              value={draft.display_name}
              onChange={(e) => patch({ display_name: e.target.value })}
            />
          </Field>
          <Field label="Folder name" hint="Renaming moves the series folder on disk.">
            <Input
              value={draft.series_dir}
              onChange={(e) => patch({ series_dir: e.target.value })}
            />
          </Field>
        </div>

        <div className="rounded-lg border border-ink-700 bg-ink-850 px-4 py-1">
          <Toggle
            checked={draft.enabled}
            onChange={(enabled) => patch({ enabled })}
            label="Enabled"
            description="Disabling stops all tracking and removes the channel from the tuner."
          />
          <div className="border-t border-ink-700/70" />
          <Toggle
            checked={draft.live_enabled}
            onChange={(live_enabled) => patch({ live_enabled })}
            label="Show in Live TV guide"
            description="Turn off to archive VODs only, without a Live TV channel."
          />
        </div>

        <div className="grid gap-4 sm:grid-cols-3">
          <Field label="VOD handling">
            <Select
              value={draft.vod_mode}
              onChange={(e) => patch({ vod_mode: e.target.value as VodMode })}
            >
              <option value="off">{VOD_MODE_LABELS.off}</option>
              <option value="strm">{VOD_MODE_LABELS.strm}</option>
              <option value="archive">{VOD_MODE_LABELS.archive}</option>
            </Select>
          </Field>
          <Field label="Quality">
            <Select value={draft.quality} onChange={(e) => patch({ quality: e.target.value })}>
              {QUALITIES.map((q) => (
                <option key={q} value={q}>
                  {q}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Season grouping">
            <Select
              value={draft.season_scheme}
              onChange={(e) => patch({ season_scheme: e.target.value as SeasonScheme })}
            >
              <option value="year">One season per year</option>
              <option value="calendar_month">One season per month</option>
            </Select>
          </Field>
        </div>

        {draft.vod_mode === 'archive' && (
          <div className="space-y-4 rounded-lg border border-ink-700 bg-ink-850 p-4">
            <p className="text-xs font-medium text-ink-300">
              Retention — leave blank to disable a rule. Applied nightly and after each download.
            </p>
            <div className="grid gap-4 sm:grid-cols-3">
              <Field label="Keep newest" hint="episodes">
                <Input
                  type="number"
                  min={0}
                  value={draft.retention_keep_count ?? ''}
                  onChange={(e) =>
                    patch({ retention_keep_count: numberOrNull(e.target.value) })
                  }
                />
              </Field>
              <Field label="Max size" hint="GB per channel">
                <Input
                  type="number"
                  min={0}
                  step="0.5"
                  value={draft.retention_max_gb ?? ''}
                  onChange={(e) => patch({ retention_max_gb: numberOrNull(e.target.value) })}
                />
              </Field>
              <Field label="Max age" hint="days">
                <Input
                  type="number"
                  min={0}
                  value={draft.retention_max_age_days ?? ''}
                  onChange={(e) =>
                    patch({ retention_max_age_days: numberOrNull(e.target.value) })
                  }
                />
              </Field>
            </div>
          </div>
        )}

        <div className="space-y-1.5 text-xs text-ink-400">
          <p className="font-medium text-ink-300">Stream URL used by Jellyfin</p>
          <code className="block truncate rounded-lg border border-ink-700 bg-ink-850 px-3 py-2 font-mono text-[11px]">
            {channel.stream_url}
          </code>
        </div>
      </div>
    </Modal>
  )
}
