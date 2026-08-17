import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Check,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Loader2,
} from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import type { ConnectionTest, JellyfinLibrary } from '@/lib/types'
import { Badge, Button, Card, CardBody, CopyRow, Field, Input, Select, Toggle } from '@/components/ui'
import { cn } from '@/lib/utils'

const STEPS = [
  'Admin account',
  'Twitch app',
  'Jellyfin',
  'URLs & EventSub',
  'Finish',
] as const

export default function Setup() {
  const [step, setStep] = useState(0)
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')

  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [twitchTest, setTwitchTest] = useState<ConnectionTest | null>(null)

  const [jellyfinUrl, setJellyfinUrl] = useState('')
  const [jellyfinKey, setJellyfinKey] = useState('')
  const [jellyfinTest, setJellyfinTest] = useState<ConnectionTest | null>(null)
  const [libraries, setLibraries] = useState<JellyfinLibrary[]>([])
  const [libraryId, setLibraryId] = useState('')

  const [selfBaseUrl, setSelfBaseUrl] = useState(window.location.origin)
  const [publicBaseUrl, setPublicBaseUrl] = useState('')
  const [eventsubEnabled, setEventsubEnabled] = useState(false)

  const testTwitch = useMutation({
    mutationFn: () => api.testTwitch(clientId, clientSecret),
    onSuccess: (result) => {
      setTwitchTest(result)
      result.ok ? toast.success(result.message) : toast.error(result.message)
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  const testJellyfin = useMutation({
    mutationFn: async () => {
      const result = await api.testJellyfin(jellyfinUrl, jellyfinKey)
      if (result.ok) {
        const libs = await api.jellyfinLibraries(jellyfinUrl, jellyfinKey)
        setLibraries(libs)
        const shows = libs.find((l) => l.collection_type === 'tvshows')
        if (shows) setLibraryId(shows.id)
      }
      return result
    },
    onSuccess: (result) => {
      setJellyfinTest(result)
      result.ok ? toast.success(result.message) : toast.error(result.message)
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  const submit = useMutation({
    mutationFn: () =>
      api.setup({
        username,
        password,
        twitch_client_id: clientId,
        twitch_client_secret: clientSecret,
        jellyfin_url: jellyfinUrl || null,
        jellyfin_api_key: jellyfinKey || null,
        jellyfin_shows_library_id: libraryId || null,
        self_base_url: selfBaseUrl || null,
        public_base_url: publicBaseUrl || null,
        eventsub_enabled: eventsubEnabled,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['session'] })
      toast.success('JellyTTV is ready')
      navigate('/')
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  const eventsubPossible = publicBaseUrl.trim().startsWith('https://')

  const canAdvance = useMemo(() => {
    switch (step) {
      case 0:
        return username.length >= 3 && password.length >= 8 && password === confirm
      case 1:
        return Boolean(clientId && clientSecret)
      case 2:
        return true
      case 3:
        return true
      default:
        return true
    }
  }, [step, username, password, confirm, clientId, clientSecret])

  const tunerBase = selfBaseUrl.replace(/\/$/, '') || window.location.origin

  return (
    <div className="mx-auto min-h-screen w-full max-w-3xl px-4 py-10">
      <header className="mb-8 flex flex-col items-center gap-3 text-center">
        <span className="grid size-12 place-items-center rounded-xl bg-gradient-to-br from-twitch-500 to-jelly-500 text-lg font-bold text-white">
          J
        </span>
        <div>
          <h1 className="text-xl font-semibold text-white">Set up JellyTTV</h1>
          <p className="mt-1 text-sm text-ink-400">
            Five steps to get Twitch channels showing up inside Jellyfin.
          </p>
        </div>
      </header>

      <ol className="mb-6 flex flex-wrap items-center justify-center gap-2 text-xs">
        {STEPS.map((label, index) => (
          <li
            key={label}
            className={cn(
              'flex items-center gap-1.5 rounded-full border px-3 py-1',
              index === step
                ? 'border-twitch-500/60 bg-twitch-600/15 text-white'
                : index < step
                  ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
                  : 'border-ink-700 text-ink-400',
            )}
          >
            {index < step ? <Check className="size-3" aria-hidden /> : <span>{index + 1}</span>}
            {label}
          </li>
        ))}
      </ol>

      <Card>
        <CardBody className="space-y-5">
          {step === 0 && (
            <>
              <StepIntro
                title="Create your admin account"
                body="This is the only account for the JellyTTV dashboard. It is separate from your Jellyfin login."
              />
              <Field label="Username">
                <Input value={username} onChange={(e) => setUsername(e.target.value)} />
              </Field>
              <Field label="Password" hint="At least 8 characters.">
                <Input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </Field>
              <Field
                label="Confirm password"
                error={confirm && confirm !== password ? 'Passwords do not match' : null}
              >
                <Input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
              </Field>
            </>
          )}

          {step === 1 && (
            <>
              <StepIntro
                title="Register a Twitch application"
                body={
                  <>
                    Create a free app at{' '}
                    <a
                      className="text-twitch-400 hover:underline"
                      href="https://dev.twitch.tv/console/apps"
                      target="_blank"
                      rel="noreferrer"
                    >
                      dev.twitch.tv/console/apps <ExternalLink className="inline size-3" />
                    </a>
                    . Set the client type to <strong>Confidential</strong> so you get a secret. Any
                    OAuth redirect URL works (e.g. <code>https://localhost</code>) — JellyTTV only
                    uses the client-credentials flow.
                  </>
                }
              />
              <Field label="Client ID">
                <Input value={clientId} onChange={(e) => setClientId(e.target.value)} />
              </Field>
              <Field label="Client Secret">
                <Input
                  type="password"
                  value={clientSecret}
                  onChange={(e) => setClientSecret(e.target.value)}
                />
              </Field>
              <div className="flex items-center gap-3">
                <Button
                  variant="outline"
                  onClick={() => testTwitch.mutate()}
                  loading={testTwitch.isPending}
                  disabled={!clientId || !clientSecret}
                >
                  Test credentials
                </Button>
                {twitchTest && (
                  <Badge tone={twitchTest.ok ? 'success' : 'danger'}>{twitchTest.message}</Badge>
                )}
              </div>
            </>
          )}

          {step === 2 && (
            <>
              <StepIntro
                title="Connect Jellyfin"
                body={
                  <>
                    Create an API key in Jellyfin under{' '}
                    <strong>Dashboard → API Keys</strong>. This is optional — without it JellyTTV
                    still writes the library, you just have to trigger scans yourself.
                  </>
                }
              />
              <Field label="Jellyfin URL" hint="As reachable from this container, e.g. http://jellyfin:8096">
                <Input
                  value={jellyfinUrl}
                  placeholder="http://jellyfin:8096"
                  onChange={(e) => setJellyfinUrl(e.target.value)}
                />
              </Field>
              <Field label="API key">
                <Input
                  type="password"
                  value={jellyfinKey}
                  onChange={(e) => setJellyfinKey(e.target.value)}
                />
              </Field>
              <div className="flex items-center gap-3">
                <Button
                  variant="outline"
                  onClick={() => testJellyfin.mutate()}
                  loading={testJellyfin.isPending}
                  disabled={!jellyfinUrl || !jellyfinKey}
                >
                  Test connection
                </Button>
                {jellyfinTest && (
                  <Badge tone={jellyfinTest.ok ? 'success' : 'danger'}>{jellyfinTest.message}</Badge>
                )}
              </div>
              {libraries.length > 0 && (
                <Field
                  label="Shows library to refresh"
                  hint="Pick the library whose folder is mounted at the media path below."
                >
                  <Select value={libraryId} onChange={(e) => setLibraryId(e.target.value)}>
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
              <Callout tone="warning">
                In the Jellyfin library settings for this folder, enable the{' '}
                <strong>NFO metadata reader</strong> and disable every internet metadata provider.
                Otherwise Jellyfin will try to match your Twitch channels against TVDB/TMDB and
                overwrite the metadata JellyTTV writes.
              </Callout>
            </>
          )}

          {step === 3 && (
            <>
              <StepIntro
                title="URLs and go-live detection"
                body="Jellyfin needs to reach JellyTTV to play streams, and Twitch needs to reach it for instant go-live webhooks."
              />
              <Field
                label="JellyTTV base URL (as Jellyfin sees it)"
                hint="Embedded in the M3U playlist and every .strm file. Use a hostname or LAN IP, not localhost."
              >
                <Input
                  value={selfBaseUrl}
                  placeholder="http://192.168.1.10:8730"
                  onChange={(e) => setSelfBaseUrl(e.target.value)}
                />
              </Field>
              <Field
                label="Public HTTPS URL (optional)"
                hint="Only needed for EventSub webhooks. Twitch requires valid HTTPS on port 443."
              >
                <Input
                  value={publicBaseUrl}
                  placeholder="https://jellyttv.example.com"
                  onChange={(e) => setPublicBaseUrl(e.target.value)}
                />
              </Field>
              <Toggle
                checked={eventsubEnabled && eventsubPossible}
                disabled={!eventsubPossible}
                onChange={setEventsubEnabled}
                label="Use EventSub webhooks for instant go-live detection"
                description={
                  eventsubPossible
                    ? 'Twitch will POST to /eventsub/callback the moment a channel goes live.'
                    : 'Needs a public https:// URL above. JellyTTV will poll every 2 minutes instead — that works fine on a LAN.'
                }
              />
              {!eventsubPossible && (
                <Callout tone="info">
                  Polling mode is fully supported: go-live is detected within ~2 minutes using a
                  single Helix request for all your channels.
                </Callout>
              )}
            </>
          )}

          {step === 4 && (
            <>
              <StepIntro
                title="Add the tuner to Jellyfin"
                body="After finishing, paste these into Jellyfin. The key is generated on save, so copy them again from Settings if they look empty."
              />
              <div className="space-y-4">
                <CopyRow label="Live TV → Tuner Devices → M3U Tuner" value={`${tunerBase}/tuner/playlist.m3u`} />
                <CopyRow label="Live TV → TV Guide Data Providers → XMLTV" value={`${tunerBase}/tuner/guide.xml`} />
              </div>
              <Callout tone="info">
                Both URLs need the tuner key appended (<code>?key=…</code>). The exact URLs including
                the key are shown on the Settings page as soon as setup completes.
              </Callout>
              <ol className="space-y-2 text-sm text-ink-300">
                <li>1. Add the M3U tuner, then the XMLTV guide provider.</li>
                <li>2. Map the guide channels if Jellyfin does not do it automatically.</li>
                <li>3. Add your media folder as a <strong>Shows</strong> library with NFO enabled.</li>
                <li>4. Add channels on the Channels page — that is it.</li>
              </ol>
            </>
          )}
        </CardBody>
      </Card>

      <div className="mt-5 flex items-center justify-between">
        <Button variant="ghost" onClick={() => setStep((s) => Math.max(0, s - 1))} disabled={step === 0}>
          <ChevronLeft className="size-4" /> Back
        </Button>
        {step < STEPS.length - 1 ? (
          <Button
            variant="primary"
            onClick={() => setStep((s) => s + 1)}
            disabled={!canAdvance}
          >
            Continue <ChevronRight className="size-4" />
          </Button>
        ) : (
          <Button variant="primary" onClick={() => submit.mutate()} loading={submit.isPending}>
            {submit.isPending ? <Loader2 className="size-4 animate-spin" /> : null}
            Finish setup
          </Button>
        )}
      </div>
    </div>
  )
}

function StepIntro({ title, body }: { title: string; body: React.ReactNode }) {
  return (
    <div className="border-b border-ink-700/70 pb-4">
      <h2 className="text-sm font-semibold text-white">{title}</h2>
      <p className="mt-1.5 text-xs leading-relaxed text-ink-400">{body}</p>
    </div>
  )
}

function Callout({
  tone,
  children,
}: {
  tone: 'info' | 'warning'
  children: React.ReactNode
}) {
  return (
    <div
      className={cn(
        'flex gap-2.5 rounded-lg border px-3.5 py-3 text-xs leading-relaxed',
        tone === 'warning'
          ? 'border-amber-500/30 bg-amber-500/10 text-amber-200'
          : 'border-jelly-500/30 bg-jelly-500/10 text-jelly-400',
      )}
    >
      <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div>{children}</div>
    </div>
  )
}
