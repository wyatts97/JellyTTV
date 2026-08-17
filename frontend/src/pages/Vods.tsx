import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, ExternalLink, ListVideo, RefreshCw, SkipForward, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import type { Vod, VodState } from '@/lib/types'
import {
  Badge,
  Button,
  Card,
  CardBody,
  EmptyState,
  Field,
  Progress,
  Select,
  Spinner,
} from '@/components/ui'
import { episodeTag, formatBytes, formatDate, formatDuration } from '@/lib/utils'

const STATE_TONES: Record<VodState, 'neutral' | 'info' | 'success' | 'warning' | 'danger'> = {
  pending: 'neutral',
  queued: 'info',
  downloading: 'info',
  complete: 'success',
  failed: 'danger',
  skipped: 'warning',
  purged: 'neutral',
}

const STATES: VodState[] = [
  'pending',
  'queued',
  'downloading',
  'complete',
  'failed',
  'skipped',
  'purged',
]

export default function Vods() {
  const queryClient = useQueryClient()
  const [channelId, setChannelId] = useState<number | ''>('')
  const [state, setState] = useState<VodState | ''>('')

  const { data: channels } = useQuery({ queryKey: ['channels'], queryFn: api.channels })
  const { data: vods, isLoading } = useQuery({
    queryKey: ['vods', channelId, state],
    queryFn: () =>
      api.vods({
        channelId: channelId === '' ? undefined : channelId,
        state: state === '' ? undefined : [state],
        limit: 300,
      }),
    refetchInterval: 15_000,
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['vods'] })
    void queryClient.invalidateQueries({ queryKey: ['channels'] })
  }

  const handlers = (message: string) => ({
    onSuccess: () => {
      toast.success(message)
      invalidate()
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  const download = useMutation({ mutationFn: api.downloadVod, ...handlers('Download queued') })
  const retry = useMutation({ mutationFn: api.retryVod, ...handlers('Retry queued') })
  const remove = useMutation({ mutationFn: api.deleteVodFile, ...handlers('File deleted') })
  const skip = useMutation({ mutationFn: api.skipVod, ...handlers('VOD skipped') })

  const totals = useMemo(() => {
    const rows = vods ?? []
    return {
      count: rows.length,
      bytes: rows.reduce((sum, row) => sum + (row.bytes ?? 0), 0),
    }
  }, [vods])

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-white">VODs</h1>
        <p className="mt-1 text-sm text-ink-400">
          {totals.count} shown · {formatBytes(totals.bytes)} archived on disk
        </p>
      </div>

      <Card>
        <CardBody className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Channel">
            <Select
              value={channelId}
              onChange={(e) => setChannelId(e.target.value === '' ? '' : Number(e.target.value))}
            >
              <option value="">All channels</option>
              {(channels ?? []).map((channel) => (
                <option key={channel.id} value={channel.id}>
                  {channel.display_name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="State">
            <Select value={state} onChange={(e) => setState(e.target.value as VodState | '')}>
              <option value="">Any state</option>
              {STATES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </Select>
          </Field>
        </CardBody>
      </Card>

      {isLoading ? (
        <div className="grid place-items-center py-20">
          <Spinner className="size-6" />
        </div>
      ) : !vods || vods.length === 0 ? (
        <Card>
          <EmptyState
            icon={<ListVideo className="size-7" />}
            title="No VODs match"
            description="VODs appear a few minutes after a stream ends, or immediately after you run a sync from the Channels page."
          />
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[52rem] text-sm">
              <thead className="border-b border-ink-700/70 text-left text-xs text-ink-400">
                <tr>
                  <th className="px-4 py-3 font-medium">Episode</th>
                  <th className="px-4 py-3 font-medium">Channel</th>
                  <th className="px-4 py-3 font-medium">Broadcast</th>
                  <th className="px-4 py-3 font-medium">Length</th>
                  <th className="px-4 py-3 font-medium">State</th>
                  <th className="px-4 py-3 font-medium">Size</th>
                  <th className="px-4 py-3" />
                </tr>
              </thead>
              <tbody className="divide-y divide-ink-700/70">
                {vods.map((vod) => (
                  <VodRow
                    key={vod.id}
                    vod={vod}
                    onDownload={() => download.mutate(vod.id)}
                    onRetry={() => retry.mutate(vod.id)}
                    onDelete={() => remove.mutate(vod.id)}
                    onSkip={() => skip.mutate(vod.id)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  )
}

function VodRow({
  vod,
  onDownload,
  onRetry,
  onDelete,
  onSkip,
}: {
  vod: Vod
  onDownload: () => void
  onRetry: () => void
  onDelete: () => void
  onSkip: () => void
}) {
  return (
    <tr className="align-top">
      <td className="px-4 py-3">
        <div className="flex items-start gap-3">
          {vod.thumbnail_url && (
            <img
              src={vod.thumbnail_url}
              alt=""
              className="hidden w-24 shrink-0 rounded border border-ink-700 object-cover sm:block"
              loading="lazy"
            />
          )}
          <div className="min-w-0">
            <p className="line-clamp-2 text-ink-200">{vod.title}</p>
            <p className="mt-0.5 font-mono text-[11px] text-ink-400">
              {episodeTag(vod.season, vod.episode)}
            </p>
            {vod.state === 'downloading' && (
              <div className="mt-2 w-40">
                <Progress value={vod.progress} />
                <span className="mt-1 block text-[11px] text-ink-400">
                  {vod.progress.toFixed(0)}%
                </span>
              </div>
            )}
            {vod.error && (
              <p className="mt-1.5 line-clamp-2 text-[11px] text-rose-400">{vod.error}</p>
            )}
          </div>
        </div>
      </td>
      <td className="px-4 py-3 text-ink-300">{vod.channel_login ?? '—'}</td>
      <td className="px-4 py-3 text-ink-300">{formatDate(vod.published_at)}</td>
      <td className="px-4 py-3 text-ink-300">{formatDuration(vod.duration_s)}</td>
      <td className="px-4 py-3">
        <Badge tone={STATE_TONES[vod.state]}>{vod.state}</Badge>
        <span className="mt-1 block text-[11px] text-ink-400">{vod.mode}</span>
      </td>
      <td className="px-4 py-3 text-ink-300">{vod.bytes ? formatBytes(vod.bytes) : '—'}</td>
      <td className="px-4 py-3">
        <div className="flex items-center justify-end gap-1">
          <a
            href={vod.url}
            target="_blank"
            rel="noreferrer"
            className="grid size-9 place-items-center rounded-lg text-ink-400 transition-colors hover:bg-ink-800 hover:text-ink-200"
            title="Open on Twitch"
          >
            <ExternalLink className="size-4" aria-hidden />
          </a>
          {vod.state === 'failed' ? (
            <Button variant="ghost" size="icon" onClick={onRetry} title="Retry download">
              <RefreshCw className="size-4" />
            </Button>
          ) : (
            <Button variant="ghost" size="icon" onClick={onDownload} title="Archive to disk">
              <Download className="size-4" />
            </Button>
          )}
          <Button variant="ghost" size="icon" onClick={onSkip} title="Skip (hide from Jellyfin)">
            <SkipForward className="size-4" />
          </Button>
          {vod.file_path && (
            <Button variant="ghost" size="icon" onClick={onDelete} title="Delete file from disk">
              <Trash2 className="size-4" />
            </Button>
          )}
        </div>
      </td>
    </tr>
  )
}
