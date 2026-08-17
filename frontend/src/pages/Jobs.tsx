import { useQuery } from '@tanstack/react-query'
import { Activity, ScrollText } from 'lucide-react'
import { api } from '@/lib/api'
import type { JobState } from '@/lib/types'
import { Badge, Card, CardHeader, EmptyState, Spinner } from '@/components/ui'
import { formatDate, formatRelative } from '@/lib/utils'

const JOB_TONES: Record<JobState, 'neutral' | 'info' | 'success' | 'danger' | 'warning'> = {
  queued: 'neutral',
  running: 'info',
  complete: 'success',
  failed: 'danger',
  cancelled: 'warning',
}

const LOG_TONES: Record<string, 'neutral' | 'info' | 'warning' | 'danger'> = {
  info: 'neutral',
  warning: 'warning',
  error: 'danger',
}

export default function Jobs() {
  const jobs = useQuery({ queryKey: ['jobs'], queryFn: () => api.jobs(150), refetchInterval: 10_000 })
  const logs = useQuery({ queryKey: ['logs'], queryFn: () => api.logs(150), refetchInterval: 20_000 })

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-white">Activity</h1>
        <p className="mt-1 text-sm text-ink-400">
          Background jobs and the event log. Useful when something is not appearing in Jellyfin.
        </p>
      </div>

      <Card className="overflow-hidden">
        <CardHeader title="Jobs" description="Newest first." />
        {jobs.isLoading ? (
          <div className="grid place-items-center py-14">
            <Spinner className="size-5" />
          </div>
        ) : !jobs.data || jobs.data.length === 0 ? (
          <EmptyState icon={<Activity className="size-6" />} title="No jobs have run yet" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[44rem] text-sm">
              <thead className="border-b border-ink-700/70 text-left text-xs text-ink-400">
                <tr>
                  <th className="px-4 py-3 font-medium">Type</th>
                  <th className="px-4 py-3 font-medium">Target</th>
                  <th className="px-4 py-3 font-medium">State</th>
                  <th className="px-4 py-3 font-medium">Result</th>
                  <th className="px-4 py-3 font-medium">Started</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-ink-700/70">
                {jobs.data.map((job) => (
                  <tr key={job.id}>
                    <td className="px-4 py-3 font-mono text-xs text-ink-200">{job.type}</td>
                    <td className="px-4 py-3 text-xs text-ink-400">{job.key ?? '—'}</td>
                    <td className="px-4 py-3">
                      <Badge tone={JOB_TONES[job.state]}>{job.state}</Badge>
                    </td>
                    <td className="max-w-md px-4 py-3 text-xs text-ink-300">
                      <span className="line-clamp-2">{job.message ?? '—'}</span>
                    </td>
                    <td className="px-4 py-3 text-xs text-ink-400">
                      {formatRelative(job.started_at ?? job.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card>
        <CardHeader title="Event log" />
        {logs.isLoading ? (
          <div className="grid place-items-center py-14">
            <Spinner className="size-5" />
          </div>
        ) : !logs.data || logs.data.length === 0 ? (
          <EmptyState icon={<ScrollText className="size-6" />} title="Nothing logged yet" />
        ) : (
          <ul className="divide-y divide-ink-700/70">
            {logs.data.map((entry) => (
              <li key={entry.id} className="flex items-start justify-between gap-4 px-5 py-3">
                <div className="min-w-0">
                  <p className="text-sm text-ink-200">{entry.message}</p>
                  <div className="mt-1 flex items-center gap-2">
                    <Badge tone={LOG_TONES[entry.level] ?? 'neutral'}>{entry.category}</Badge>
                  </div>
                </div>
                <span className="shrink-0 text-xs text-ink-400" title={formatDate(entry.created_at)}>
                  {formatRelative(entry.created_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}
