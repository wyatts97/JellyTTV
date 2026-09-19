import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BellOff, BellRing, Download, Send, Smartphone, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, type ApiError } from '@/lib/api'
import { currentSubscription, pushState, subscribe, unsubscribe, type PushState } from '@/lib/push'
import { Badge, Button } from './ui'
import { formatRelative } from '@/lib/utils'

const STATE_HINTS: Partial<Record<PushState, string>> = {
  insecure:
    'Push notifications need HTTPS. Open JellyTTV through your https:// domain (or on localhost) to enable them here.',
  unsupported: "This browser doesn't support Web Push.",
  'ios-needs-install':
    'On iPhone and iPad, tap Share → Add to Home Screen, then open JellyTTV from the home screen and enable notifications there.',
  denied:
    'Notifications are blocked for this site. Allow them in the browser’s site settings, then come back here.',
}

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

/** "This device" push opt-in, the subscribed-device list, and the PWA install button. */
export function PushDevices() {
  const queryClient = useQueryClient()
  const [state, setState] = useState<PushState | null>(null)
  const [myEndpoint, setMyEndpoint] = useState<string | null>(null)
  const [installPrompt, setInstallPrompt] = useState<BeforeInstallPromptEvent | null>(null)

  const devices = useQuery({ queryKey: ['push-subscriptions'], queryFn: api.pushSubscriptions })

  const refreshState = async () => {
    setState(await pushState())
    setMyEndpoint((await currentSubscription())?.endpoint ?? null)
  }

  useEffect(() => {
    void refreshState()
    const onPrompt = (event: Event) => {
      event.preventDefault()
      setInstallPrompt(event as BeforeInstallPromptEvent)
    }
    window.addEventListener('beforeinstallprompt', onPrompt)
    return () => window.removeEventListener('beforeinstallprompt', onPrompt)
  }, [])

  const enable = useMutation({
    mutationFn: subscribe,
    onSuccess: () => toast.success('Notifications enabled on this device'),
    onError: (error: Error) => toast.error(error.message),
    onSettled: () => {
      void refreshState()
      void queryClient.invalidateQueries({ queryKey: ['push-subscriptions'] })
    },
  })

  const disable = useMutation({
    mutationFn: unsubscribe,
    onSuccess: () => toast.success('Notifications turned off on this device'),
    onError: (error: Error) => toast.error(error.message),
    onSettled: () => {
      void refreshState()
      void queryClient.invalidateQueries({ queryKey: ['push-subscriptions'] })
    },
  })

  const remove = useMutation({
    mutationFn: (id: number) => api.pushUnsubscribe({ id }),
    onSettled: () => {
      void refreshState()
      void queryClient.invalidateQueries({ queryKey: ['push-subscriptions'] })
    },
  })

  const test = useMutation({
    mutationFn: api.pushTest,
    onSuccess: (r) => (r.ok ? toast.success(r.message) : toast.error(r.message)),
    onError: (error: ApiError) => toast.error(error.message),
    onSettled: () => void queryClient.invalidateQueries({ queryKey: ['push-subscriptions'] }),
  })

  const hint = state ? STATE_HINTS[state] : undefined

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-ink-700/70 bg-ink-850 px-4 py-3">
        <div className="min-w-0">
          <p className="text-sm text-ink-200">This device</p>
          <p className="mt-0.5 text-xs text-ink-400">
            {state === 'subscribed'
              ? 'Receiving go-live notifications. Tapping one opens the stream in the player.'
              : hint ?? 'Get a notification here when a tracked channel goes live.'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {installPrompt && (
            <Button
              size="sm"
              variant="outline"
              onClick={async () => {
                await installPrompt.prompt()
                await installPrompt.userChoice
                setInstallPrompt(null)
              }}
            >
              <Download className="size-3.5" /> Install app
            </Button>
          )}
          {state === 'subscribed' ? (
            <Button size="sm" variant="secondary" onClick={() => disable.mutate()} loading={disable.isPending}>
              <BellOff className="size-3.5" /> Turn off
            </Button>
          ) : (
            <Button
              size="sm"
              variant="primary"
              onClick={() => enable.mutate()}
              loading={enable.isPending}
              disabled={state !== 'unsubscribed'}
            >
              <BellRing className="size-3.5" /> Enable notifications
            </Button>
          )}
        </div>
      </div>

      <div>
        <div className="mb-2 flex items-center justify-between">
          <p className="text-xs font-medium text-ink-300">Subscribed devices</p>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => test.mutate()}
            loading={test.isPending}
            disabled={!devices.data?.length}
          >
            <Send className="size-3.5" /> Send test
          </Button>
        </div>
        {devices.data?.length ? (
          <ul className="divide-y divide-ink-700/70 rounded-lg border border-ink-700/70">
            {devices.data.map((device) => (
              <li key={device.id} className="flex items-center gap-3 px-3 py-2.5">
                <Smartphone className="size-4 shrink-0 text-ink-400" aria-hidden />
                <div className="min-w-0 flex-1">
                  <p className="flex items-center gap-2 truncate text-sm text-ink-200">
                    {device.label ?? 'Unknown device'}
                    {device.endpoint === myEndpoint && <Badge tone="info">this device</Badge>}
                    {device.failure_count > 0 && <Badge tone="warning">{device.failure_count} failed</Badge>}
                  </p>
                  <p className="text-[11px] text-ink-400">
                    Added {formatRelative(device.created_at)}
                    {device.last_success_at && ` · last delivered ${formatRelative(device.last_success_at)}`}
                  </p>
                </div>
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label="Remove device"
                  title="Remove device"
                  onClick={() => remove.mutate(device.id)}
                >
                  <Trash2 className="size-4" />
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="rounded-lg border border-dashed border-ink-700 px-3 py-3 text-xs text-ink-400">
            No devices yet. Enable notifications on each phone, tablet or computer you want alerts on.
          </p>
        )}
      </div>
    </div>
  )
}
