import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  Activity,
  HardDrive,
  Radio,
  RefreshCw,
  Tv,
  Users,
  Zap,
} from 'lucide-react'
import { api } from '@/lib/api'
import type { Dashboard as DashboardData, LiveChannel } from '@/lib/types'
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  LiveBadge,
  Progress,
  Spinner,
} from '@/components/ui'
import { formatBytes, formatNumber, formatRelative, formatUptime } from '@/lib/utils'

export default function Dashboard() {
  const { data, isLoading } = useQuery({
    queryKey: ['dashboard'],
    queryFn: api.dashboard,
    refetchInterval: 30_000,
  })

  if (isLoading || !data) {
    return (
      <div className="grid place-items-center py-24">
        <Spinner className="size-6" />
      </div>
    )
  }

  const diskUsedPct = data.disk.total ? (data.disk.used / data.disk.total) * 100 : 0

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-white">Dashboard</h1>
        <p className="mt-1 text-sm text-ink-400">
          {data.channels.enabled} of {data.channels.total} channels enabled · JellyTTV v{data.version}
        </p>
      </div>

      <SetupWarnings data={data} />

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          icon={<Radio className="size-4" />}
          label="Live now"
          value={String(data.channels.live)}
          tone={data.channels.live > 0 ? 'live' : 'neutral'}
        />
        <Stat icon={<Tv className="size-4" />} label="Channels" value={String(data.channels.total)} />
        <Stat
          icon={<Activity className="size-4" />}
          label="Episodes published"
          value={formatNumber(data.vods.complete ?? 0)}
          hint={`${formatNumber(data.vods.queued ?? 0)} queued · ${formatNumber(
            data.vods.failed ?? 0,
          )} failed`}
        />
        <Stat
          icon={<HardDrive className="size-4" />}
          label="Library on disk"
          value={formatBytes(data.disk.library)}
          hint={`${formatBytes(data.disk.free)} free`}
        >
          <Progress value={diskUsedPct} className="mt-3" />
        </Stat>
      </div>

      <Card>
        <CardHeader
          title="Live channels"
          description="Streams currently playable through the Jellyfin Live TV tuner."
          action={
            <Badge tone={data.eventsub.mode === 'webhook' ? 'success' : 'info'}>
              <Zap className="size-3" aria-hidden />
              {data.eventsub.mode === 'webhook' ? 'EventSub webhooks' : 'Polling every 2m'}
            </Badge>
          }
        />
        {data.live.length === 0 ? (
          <EmptyState
            icon={<Radio className="size-7" />}
            title="Nobody is live right now"
            description="When a tracked channel goes live it appears here and in Jellyfin's guide within seconds (webhooks) or ~2 minutes (polling)."
          />
        ) : (
          <div className="grid gap-4 p-5 sm:grid-cols-2 xl:grid-cols-3">
            {data.live.map((channel) => (
              <LiveCard key={channel.id} channel={channel} />
            ))}
          </div>
        )}
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Active jobs"
            description="Syncs, downloads and library writes in flight."
            action={
              <Link to="/jobs" className="text-xs text-twitch-400 hover:underline">
                View all
              </Link>
            }
          />
          {data.jobs.length === 0 ? (
            <EmptyState icon={<RefreshCw className="size-6" />} title="Nothing running" />
          ) : (
            <ul className="divide-y divide-ink-700/70">
              {data.jobs.map((job) => (
                <li key={job.id} className="flex items-center justify-between gap-3 px-5 py-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm text-ink-200">{job.type}</p>
                    <p className="truncate text-xs text-ink-400">
                      {job.message ?? job.key ?? '—'}
                    </p>
                  </div>
                  <Badge tone={job.state === 'running' ? 'info' : 'neutral'}>{job.state}</Badge>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card>
          <CardHeader title="Recent activity" />
          {data.logs.length === 0 ? (
            <EmptyState icon={<Activity className="size-6" />} title="No activity yet" />
          ) : (
            <ul className="divide-y divide-ink-700/70">
              {data.logs.slice(0, 10).map((entry) => (
                <li key={entry.id} className="flex items-start justify-between gap-3 px-5 py-3">
                  <div className="min-w-0">
                    <p className="text-sm text-ink-200">{entry.message}</p>
                    <p className="text-xs text-ink-400">{entry.category}</p>
                  </div>
                  <span className="shrink-0 text-xs text-ink-400">
                    {formatRelative(entry.created_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  )
}

function SetupWarnings({ data }: { data: DashboardData }) {
  const warnings: string[] = []
  if (!data.setup.twitch) warnings.push('Twitch credentials are missing — nothing can be tracked.')
  if (!data.setup.jellyfin)
    warnings.push('Jellyfin is not connected — library scans must be triggered manually.')
  if (data.eventsub.enabled && !data.eventsub.healthy && data.eventsub.total > 0)
    warnings.push('Some EventSub subscriptions are unhealthy — check Settings → EventSub.')
  if (data.eventsub.enabled && !data.eventsub.possible)
    warnings.push('EventSub is enabled but no public HTTPS URL is set — falling back to polling.')

  if (warnings.length === 0) return null

  return (
    <div className="space-y-2">
      {warnings.map((warning) => (
        <div
          key={warning}
          className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-xs text-amber-200"
        >
          {warning}{' '}
          <Link to="/settings" className="underline">
            Open settings
          </Link>
        </div>
      ))}
    </div>
  )
}

function Stat({
  icon,
  label,
  value,
  hint,
  tone = 'neutral',
  children,
}: {
  icon: React.ReactNode
  label: string
  value: string
  hint?: string
  tone?: 'neutral' | 'live'
  children?: React.ReactNode
}) {
  return (
    <Card>
      <CardBody>
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-ink-400">{label}</span>
          <span className={tone === 'live' ? 'text-rose-400' : 'text-ink-400'}>{icon}</span>
        </div>
        <p className="mt-2 text-2xl font-semibold text-white">{value}</p>
        {hint && <p className="mt-1 text-xs text-ink-400">{hint}</p>}
        {children}
      </CardBody>
    </Card>
  )
}

function LiveCard({ channel }: { channel: LiveChannel }) {
  return (
    <div className="overflow-hidden rounded-lg border border-ink-700/70 bg-ink-850">
      {channel.thumbnail_url ? (
        <img
          src={channel.thumbnail_url}
          alt=""
          className="aspect-video w-full object-cover"
          loading="lazy"
        />
      ) : (
        <div className="grid aspect-video w-full place-items-center bg-ink-800 text-ink-400">
          <Radio className="size-6" aria-hidden />
        </div>
      )}
      <div className="space-y-2 p-3.5">
        <div className="flex items-center gap-2">
          {channel.avatar_url && (
            <img src={channel.avatar_url} alt="" className="size-6 rounded-full" loading="lazy" />
          )}
          <span className="min-w-0 flex-1 truncate text-sm font-medium text-white">
            {channel.display_name}
          </span>
          <LiveBadge />
        </div>
        <p className="line-clamp-2 text-xs text-ink-300">{channel.title ?? 'Untitled stream'}</p>
        <div className="flex items-center gap-3 text-[11px] text-ink-400">
          {channel.game && <span className="truncate">{channel.game}</span>}
          <span className="flex items-center gap-1">
            <Users className="size-3" aria-hidden />
            {formatNumber(channel.viewers)}
          </span>
          <span>{formatUptime(channel.started_at)}</span>
        </div>
      </div>
    </div>
  )
}
