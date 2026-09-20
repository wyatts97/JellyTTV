import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Hls, { type ErrorData } from 'hls.js'
import {
  Loader2,
  Maximize,
  Minimize,
  Pause,
  PictureInPicture2,
  Play,
  RectangleHorizontal,
  RotateCw,
  ShieldCheck,
  Shrink,
  Volume2,
  VolumeX,
} from 'lucide-react'
import { api, watchPlaylistUrl } from '@/lib/api'
import { PREF, readPref, writePref } from '@/lib/prefs'
import type { WatchMode } from '@/lib/types'
import { useUiState } from '@/lib/uiState'
import { cn } from '@/lib/utils'

/**
 * The built-in live player.
 *
 * hls.js plays the playlist JellyTTV's session engine produces for the browser:
 * full quality, with an ad break covered by a clean copy of the stream (a short
 * 360p bridge, then a full-quality backup). Everything that made Jellyfin's
 * ffmpeg freeze - a resolution change mid-stream, a discontinuity - is ordinary
 * input here.
 *
 * Recovery is layered, cheapest first: hls.js's own retries, then
 * `startLoad()` / `recoverMediaError()`, then a full rebuild of the source.
 * A watchdog covers the case none of those see - the clock simply stops - by
 * seeking to the live edge and, if that does not help, rebuilding.
 */

// Stall recovery, cheapest first. A pause/play nudge fixes most stalls and is
// invisible, so it comes early; seeking to the live edge loses whatever was
// buffered; a rebuild costs a reconnect. Modelled on VAFT's buffering monitor,
// which samples this often and nudges rather than reloading.
const STALL_SAMPLE_MS = 500
const STALL_NUDGE_MS = 1_500
const STALL_SEEK_MS = 6_000
const STALL_REBUILD_MS = 15_000
// A nudge that did not help must not be repeated every sample.
const STALL_NUDGE_REPEAT_MS = 5_000
// During a break the source is being switched underneath the player, and a
// nudge mid-switch fights it. Give a break longer before intervening.
const AD_BREAK_GRACE_MS = 4_000
const MAX_NETWORK_RETRIES = 6
const CONTROLS_HIDE_MS = 3_000

// In seconds, not segment counts: Twitch declares a 6s target duration for 2s
// segments, so "3 target durations" would put the player 18s behind live.
// 8s keeps a few segments of cushion for a source switch at an ad break.
const LIVE_SYNC_SECONDS = 8
const LIVE_MAX_LATENCY_SECONDS = 24

// Live playlists are cheap and the server answers a slow render with 503 +
// Retry-After rather than making the player wait, so retry them generously.
const PLAYLIST_POLICY = {
  default: {
    maxTimeToFirstByteMs: 10_000,
    maxLoadTimeMs: 20_000,
    timeoutRetry: { maxNumRetry: 4, retryDelayMs: 0, maxRetryDelayMs: 0 },
    errorRetry: { maxNumRetry: 8, retryDelayMs: 1_000, maxRetryDelayMs: 8_000 },
  },
}

type Phase = 'loading' | 'playing' | 'buffering' | 'error'

/** `1080p60`, the way Twitch and every other player spell a rendition. */
function levelLabel(level: { height?: number; bitrate?: number; attrs?: Record<string, string> }): string {
  if (!level.height) return `${Math.round((level.bitrate ?? 0) / 1000)}k`
  const fps = Number(level.attrs?.['FRAME-RATE'])
  return `${level.height}p${Number.isFinite(fps) && fps >= 50 ? '60' : ''}`
}

export function LivePlayer({
  login,
  mode,
  onModeChange,
  poster,
  onOffline,
}: {
  login: string
  mode: WatchMode
  onModeChange: (mode: WatchMode) => void
  poster?: string
  onOffline?: () => void
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const videoRef = useRef<HTMLVideoElement>(null)
  const hlsRef = useRef<Hls | null>(null)
  const userPaused = useRef(false)
  // Kept in a ref so a new callback identity never tears the source down.
  const onOfflineRef = useRef(onOffline)
  onOfflineRef.current = onOffline

  const [generation, setGeneration] = useState(0)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [paused, setPaused] = useState(false)
  const [muted, setMuted] = useState<boolean>(() => readPref(PREF.muted, false))
  const [volume, setVolume] = useState<number>(() => readPref(PREF.volume, 1))
  const [needsUnmute, setNeedsUnmute] = useState(false)
  const [height, setHeight] = useState<number | null>(null)
  const [latency, setLatency] = useState<number | null>(null)
  const [fullscreen, setFullscreen] = useState(false)
  const [controlsVisible, setControlsVisible] = useState(true)
  // Renditions hls.js found, plus which one is playing (-1 is auto).
  const [levels, setLevels] = useState<{ index: number; label: string }[]>([])
  const [currentLevel, setCurrentLevel] = useState(-1)
  const { theater, setTheater } = useUiState()

  const rebuild = useCallback(() => setGeneration((g) => g + 1), [])
  // Survives rebuilds, so a stream that keeps failing eventually says so.
  const networkRetries = useRef(0)
  useEffect(() => {
    networkRetries.current = 0
  }, [login, mode])

  const status = useQuery({
    queryKey: ['watch-status', login, mode],
    queryFn: () => api.watchStatus(login, mode),
    refetchInterval: 2_000,
    enabled: phase === 'playing' || phase === 'buffering',
  })

  // Read by the stall watchdog, which treats a break more patiently than an
  // ordinary stall: the source is being switched underneath the player.
  const inAdBreak = useRef(false)
  const wasInAdBreak = useRef(false)
  useEffect(() => {
    const active = status.data?.in_ad_break ?? false
    inAdBreak.current = active
    if (wasInAdBreak.current && !active) {
      // The break is over. Switching sources costs a little latency each time;
      // if it added up, rejoin the live edge rather than drifting behind.
      const video = videoRef.current
      const edge = hlsRef.current?.liveSyncPosition
      if (video && edge && video.currentTime < edge - LIVE_MAX_LATENCY_SECONDS) {
        video.currentTime = edge
      }
    }
    wasInAdBreak.current = active
  }, [status.data?.in_ad_break])

  // ------------------------------------------------------------ source setup
  useEffect(() => {
    const video = videoRef.current
    if (!video) return
    const src = watchPlaylistUrl(login, mode)
    let disposed = false
    let lastMediaRecovery = 0
    let retryTimer: number | undefined

    setPhase('loading')
    setError(null)
    setHeight(null)
    setLevels([])

    const start = () => {
      video.muted = readPref(PREF.muted, false)
      video.volume = readPref(PREF.volume, 1)
      userPaused.current = false
      video.play().catch(() => {
        // Autoplay with sound was refused; muted autoplay is always allowed.
        if (disposed) return
        video.muted = true
        setMuted(true)
        setNeedsUnmute(true)
        video.play().catch(() => setPaused(true))
      })
    }

    if (Hls.isSupported()) {
      const hls = new Hls({
        enableWorker: true,
        lowLatencyMode: false,
        liveSyncDuration: LIVE_SYNC_SECONDS,
        liveMaxLatencyDuration: LIVE_MAX_LATENCY_SECONDS,
        maxLiveSyncPlaybackRate: 1.1,
        backBufferLength: 30,
        // Let hls.js jump the small gaps a source switch can leave before the
        // watchdog below ever sees them as a stall.
        maxBufferHole: 0.5,
        nudgeMaxRetry: 8,
        manifestLoadPolicy: PLAYLIST_POLICY,
        playlistLoadPolicy: PLAYLIST_POLICY,
        // Every url in the playlist is either same-origin (cookie auth) or a
        // Twitch CDN edge that answers `Access-Control-Allow-Origin: *`.
        xhrSetup: (xhr) => {
          xhr.withCredentials = false
        },
      })
      hlsRef.current = hls
      // Safari's ManagedMediaSource needs remote playback off or an AirPlay source.
      if (!('MediaSource' in window) && 'ManagedMediaSource' in window) {
        video.disableRemotePlayback = true
      }

      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        setLevels(hls.levels.map((level, index) => ({ index, label: levelLabel(level) })))
        setCurrentLevel(hls.currentLevel)
        start()
      })
      hls.on(Hls.Events.LEVEL_SWITCHED, (_event, data) => setCurrentLevel(data.level))
      hls.on(Hls.Events.FRAG_BUFFERED, () => {
        networkRetries.current = 0
      })
      hls.on(Hls.Events.ERROR, (_event, data: ErrorData) => {
        if (disposed) return
        if (!data.fatal) return
        const code = data.response?.code
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
          if (code === 404 || code === 409) {
            setPhase('error')
            setError(code === 404 ? 'This channel is not tracked.' : 'Live playback is disabled for this channel.')
            return
          }
          if (code === 401) {
            setPhase('error')
            setError('Your session expired - sign in again.')
            return
          }
          networkRetries.current += 1
          if (networkRetries.current > MAX_NETWORK_RETRIES) {
            setPhase('error')
            setError(code === 503 ? 'The stream is unavailable - the channel may have gone offline.' : 'Lost connection to the stream.')
            if (code === 503) onOfflineRef.current?.()
            return
          }
          // hls.js's own retries are spent; start over with a fresh instance
          // (a failed manifest cannot be revived with startLoad()).
          setPhase('buffering')
          const delay = Math.min(1000 * 2 ** (networkRetries.current - 1), 10_000)
          retryTimer = window.setTimeout(() => {
            if (!disposed) rebuild()
          }, delay)
          return
        }
        if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
          const now = Date.now()
          if (now - lastMediaRecovery > 3_000) {
            lastMediaRecovery = now
            hls.recoverMediaError()
          } else {
            // Recovering twice in a row did not help: the usual culprit is an
            // audio codec change at a source switch.
            lastMediaRecovery = now
            hls.swapAudioCodec()
            hls.recoverMediaError()
          }
          return
        }
        rebuild()
      })

      hls.loadSource(src)
      hls.attachMedia(video)
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      // Old iOS without any Media Source: native HLS, driven by the browser.
      video.src = src
      video.addEventListener('loadedmetadata', start, { once: true })
    } else {
      setPhase('error')
      setError('This browser cannot play HLS video.')
    }

    return () => {
      disposed = true
      window.clearTimeout(retryTimer)
      hlsRef.current?.destroy()
      hlsRef.current = null
      video.removeAttribute('src')
      video.load()
    }
  }, [login, mode, generation, rebuild])

  // ------------------------------------------------------ media element state
  useEffect(() => {
    const video = videoRef.current
    if (!video) return
    const onPlaying = () => {
      setPhase('playing')
      setPaused(false)
    }
    const onWaiting = () => setPhase((p) => (p === 'error' ? p : 'buffering'))
    const onPause = () => setPaused(true)
    const onPlay = () => setPaused(false)
    const onResize = () => setHeight(video.videoHeight || null)
    const onVolume = () => {
      setMuted(video.muted)
      setVolume(video.volume)
    }
    video.addEventListener('playing', onPlaying)
    video.addEventListener('waiting', onWaiting)
    video.addEventListener('pause', onPause)
    video.addEventListener('play', onPlay)
    video.addEventListener('resize', onResize)
    video.addEventListener('volumechange', onVolume)
    return () => {
      video.removeEventListener('playing', onPlaying)
      video.removeEventListener('waiting', onWaiting)
      video.removeEventListener('pause', onPause)
      video.removeEventListener('play', onPlay)
      video.removeEventListener('resize', onResize)
      video.removeEventListener('volumechange', onVolume)
    }
  }, [])

  // ----------------------------------------------------------- stall watchdog
  // The clock stopping is the one failure hls.js cannot report: no error fires,
  // the player simply stops. Escalates only as far as it has to.
  useEffect(() => {
    let lastTime = -1
    let lastBuffered = -1
    let lastAdvance = Date.now()
    let lastNudge = 0
    let seeked = false

    const bufferedEnd = (video: HTMLVideoElement) =>
      video.buffered.length ? video.buffered.end(video.buffered.length - 1) : 0

    const timer = window.setInterval(() => {
      const video = videoRef.current
      if (!video) return
      const hls = hlsRef.current
      if (hls?.latency !== undefined && Number.isFinite(hls.latency)) setLatency(hls.latency)

      // A paused, hidden or not-yet-started player is not stalled.
      if (userPaused.current || document.hidden || video.readyState === 0) {
        lastAdvance = Date.now()
        lastTime = video.currentTime
        return
      }

      const buffered = bufferedEnd(video)
      if (video.currentTime !== lastTime || buffered !== lastBuffered) {
        // Either the picture or the buffer moved: not stuck.
        lastTime = video.currentTime
        lastBuffered = buffered
        lastAdvance = Date.now()
        seeked = false
        return
      }

      const stuckFor = Date.now() - lastAdvance
      const grace = inAdBreak.current ? AD_BREAK_GRACE_MS : 0
      if (stuckFor > STALL_REBUILD_MS + grace) {
        lastAdvance = Date.now()
        seeked = false
        rebuild()
      } else if (stuckFor > STALL_SEEK_MS + grace && !seeked) {
        // The nudge did not help: give up the buffer and rejoin at the edge.
        seeked = true
        const edge =
          hls?.liveSyncPosition ??
          (video.seekable.length ? video.seekable.end(video.seekable.length - 1) - 3 : null)
        if (edge !== null && edge !== undefined && Number.isFinite(edge)) video.currentTime = edge
        void video.play().catch(() => undefined)
      } else if (
        stuckFor > STALL_NUDGE_MS + grace &&
        Date.now() - lastNudge > STALL_NUDGE_REPEAT_MS
      ) {
        // The cheap fix, and the one that resolves most stalls: a pause/play
        // makes the browser re-evaluate the buffer without losing it.
        lastNudge = Date.now()
        video.pause()
        void video.play().catch(() => undefined)
      }
    }, STALL_SAMPLE_MS)
    return () => window.clearInterval(timer)
  }, [rebuild])

  // ------------------------------------------------------------ fullscreen
  useEffect(() => {
    const onChange = () => setFullscreen(document.fullscreenElement === containerRef.current)
    document.addEventListener('fullscreenchange', onChange)
    return () => document.removeEventListener('fullscreenchange', onChange)
  }, [])

  // ------------------------------------------------------------- actions
  const togglePlay = useCallback(() => {
    const video = videoRef.current
    if (!video) return
    if (video.paused) {
      userPaused.current = false
      // Resuming a live stream from a pause means rejoining at the live edge.
      const edge = hlsRef.current?.liveSyncPosition
      if (edge && video.currentTime < edge - 10) video.currentTime = edge
      void video.play()
    } else {
      userPaused.current = true
      video.pause()
    }
  }, [])

  const toggleMute = useCallback(() => {
    const video = videoRef.current
    if (!video) return
    video.muted = !video.muted
    if (!video.muted && video.volume === 0) video.volume = 0.5
    setNeedsUnmute(false)
    writePref(PREF.muted, video.muted)
  }, [])

  const changeVolume = useCallback((value: number) => {
    const video = videoRef.current
    if (!video) return
    video.volume = value
    video.muted = value === 0
    setNeedsUnmute(false)
    writePref(PREF.volume, value)
    writePref(PREF.muted, video.muted)
  }, [])

  const jumpToLive = useCallback(() => {
    const video = videoRef.current
    const edge = hlsRef.current?.liveSyncPosition
    if (video && edge) video.currentTime = edge
  }, [])

  const toggleFullscreen = useCallback(() => {
    const container = containerRef.current
    const video = videoRef.current as (HTMLVideoElement & { webkitEnterFullscreen?: () => void }) | null
    if (document.fullscreenElement) {
      void document.exitFullscreen()
    } else if (container?.requestFullscreen) {
      void container.requestFullscreen()
    } else {
      video?.webkitEnterFullscreen?.()
    }
  }, [])

  const toggleTheater = useCallback(() => setTheater(!theater), [setTheater, theater])

  const changeLevel = useCallback((index: number) => {
    const hls = hlsRef.current
    if (!hls) return
    // -1 is hls.js's "pick for me"; anything else pins the rendition.
    hls.currentLevel = index
    setCurrentLevel(index)
  }, [])

  const togglePip = useCallback(async () => {
    const video = videoRef.current
    if (!video) return
    try {
      if (document.pictureInPictureElement) await document.exitPictureInPicture()
      else await video.requestPictureInPicture()
    } catch {
      /* not supported or refused */
    }
  }, [])

  // ---------------------------------------------------------- keyboard + UI
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null
      if (target && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))) return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      switch (e.key.toLowerCase()) {
        case ' ':
        case 'k':
          e.preventDefault()
          togglePlay()
          break
        case 'm':
          toggleMute()
          break
        case 'f':
          toggleFullscreen()
          break
        case 't':
          toggleTheater()
          break
        case 'l':
          jumpToLive()
          break
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [togglePlay, toggleMute, toggleFullscreen, jumpToLive, toggleTheater])

  const hideTimer = useRef<number | undefined>(undefined)
  const pokeControls = useCallback(() => {
    setControlsVisible(true)
    window.clearTimeout(hideTimer.current)
    hideTimer.current = window.setTimeout(() => setControlsVisible(false), CONTROLS_HIDE_MS)
  }, [])
  useEffect(() => () => window.clearTimeout(hideTimer.current), [])

  const showControls = controlsVisible || paused || phase !== 'playing'
  const behindLive = latency !== null && latency > LIVE_SYNC_SECONDS + 10
  const adBreak = status.data?.in_ad_break ?? false
  const pipSupported = typeof document !== 'undefined' && 'pictureInPictureEnabled' in document && document.pictureInPictureEnabled

  return (
    <div
      ref={containerRef}
      className={cn(
        'group relative aspect-video w-full overflow-hidden bg-black',
        fullscreen ? 'rounded-none' : 'rounded-xl',
        // Theater fills the window rather than overflowing it: the aspect ratio
        // still drives the width, but never past the viewport height.
        theater && 'mx-auto max-h-[calc(100vh-2rem)] w-auto max-w-full rounded-none',
        !showControls && 'cursor-none',
      )}
      onMouseMove={pokeControls}
      onTouchStart={pokeControls}
    >
      <video
        ref={videoRef}
        className="size-full bg-black object-contain"
        playsInline
        poster={poster}
        onClick={togglePlay}
        onDoubleClick={toggleFullscreen}
      />

      {/* Centre state */}
      {(phase === 'loading' || phase === 'buffering') && (
        <div className="pointer-events-none absolute inset-0 grid place-items-center">
          <Loader2 className="size-10 animate-spin text-white/80" aria-label="Loading" />
        </div>
      )}
      {phase === 'error' && (
        <div className="absolute inset-0 grid place-items-center bg-black/75 p-6 text-center">
          <div className="space-y-3">
            <p className="text-sm text-ink-200">{error}</p>
            <button
              type="button"
              onClick={rebuild}
              className="inline-flex items-center gap-2 rounded-lg bg-twitch-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-twitch-500"
            >
              <RotateCw className="size-4" /> Retry
            </button>
          </div>
        </div>
      )}
      {needsUnmute && phase !== 'error' && (
        <button
          type="button"
          onClick={toggleMute}
          className="absolute left-3 top-3 inline-flex items-center gap-2 rounded-lg bg-black/70 px-3 py-1.5 text-sm font-medium text-white backdrop-blur hover:bg-black/85"
        >
          <VolumeX className="size-4" /> Tap to unmute
        </button>
      )}

      {/* Ad-break pill */}
      {adBreak && phase !== 'error' && (
        <div className="pointer-events-none absolute right-3 top-3 inline-flex items-center gap-1.5 rounded-full bg-emerald-600/85 px-3 py-1 text-xs font-medium text-white shadow backdrop-blur">
          <ShieldCheck className="size-3.5" aria-hidden />
          {status.data?.holding
            ? 'Ad break blocked - finding a clean source'
            : status.data?.serving_bridge
              ? `Ad break blocked - clean ${status.data?.resolution ?? 'backup'} copy`
              : 'Ad break blocked - same quality'}
        </div>
      )}

      {/* Control bar */}
      <div
        className={cn(
          'absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/85 via-black/40 to-transparent px-3 pb-2.5 pt-10 transition-opacity duration-200',
          showControls ? 'opacity-100' : 'pointer-events-none opacity-0',
        )}
      >
        <div className="flex items-center gap-1.5 text-white sm:gap-2">
          <IconButton label={paused ? 'Play (k)' : 'Pause (k)'} onClick={togglePlay}>
            {paused ? <Play className="size-5" /> : <Pause className="size-5" />}
          </IconButton>
          <IconButton label={muted ? 'Unmute (m)' : 'Mute (m)'} onClick={toggleMute}>
            {muted || volume === 0 ? <VolumeX className="size-5" /> : <Volume2 className="size-5" />}
          </IconButton>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={muted ? 0 : volume}
            onChange={(e) => changeVolume(Number(e.target.value))}
            aria-label="Volume"
            className="hidden w-20 accent-twitch-500 sm:block"
          />
          <button
            type="button"
            onClick={jumpToLive}
            title="Jump to live (l)"
            className={cn(
              'ml-1 inline-flex items-center gap-1.5 rounded px-1.5 py-0.5 text-xs font-semibold uppercase tracking-wide',
              behindLive ? 'text-ink-300 hover:text-white' : 'text-white',
            )}
          >
            <span className={cn('size-2 rounded-full', behindLive ? 'bg-ink-400' : 'bg-rose-500')} />
            Live
          </button>
          {latency !== null && (
            <span className="hidden text-xs tabular-nums text-ink-300 sm:inline" title="Delay behind the broadcast">
              {latency.toFixed(1)}s
            </span>
          )}

          <div className="flex-1" />

          {levels.length > 1 ? (
            <label className="relative">
              <span className="sr-only">Quality</span>
              <select
                value={currentLevel}
                onChange={(event) => changeLevel(Number(event.target.value))}
                className="cursor-pointer appearance-none rounded bg-white/10 px-1.5 py-1 text-xs font-medium tabular-nums text-white hover:bg-white/20 focus-visible:ring-1 [&>option]:bg-ink-900"
                title="Quality"
              >
                <option value={-1}>Auto{height ? ` (${height}p)` : ''}</option>
                {levels.map((level) => (
                  <option key={level.index} value={level.index}>
                    {level.label}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            height && (
              <span className="rounded bg-white/10 px-1.5 py-0.5 text-xs font-medium tabular-nums">
                {height}p
              </span>
            )
          )}
          <ModeSwitch mode={mode} onChange={onModeChange} />
          {pipSupported && (
            <IconButton label="Picture-in-picture" onClick={togglePip}>
              <PictureInPicture2 className="size-5" />
            </IconButton>
          )}
          <IconButton
            label={theater ? 'Exit theater mode (t)' : 'Theater mode (t)'}
            onClick={toggleTheater}
          >
            {theater ? <Shrink className="size-5" /> : <RectangleHorizontal className="size-5" />}
          </IconButton>
          <IconButton label={fullscreen ? 'Exit fullscreen (f)' : 'Fullscreen (f)'} onClick={toggleFullscreen}>
            {fullscreen ? <Minimize className="size-5" /> : <Maximize className="size-5" />}
          </IconButton>
        </div>
      </div>
    </div>
  )
}

function IconButton({
  label,
  onClick,
  children,
}: {
  label: string
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className="grid size-9 place-items-center rounded-md text-white/90 transition-colors hover:bg-white/10 hover:text-white"
    >
      {children}
    </button>
  )
}

function ModeSwitch({ mode, onChange }: { mode: WatchMode; onChange: (mode: WatchMode) => void }) {
  return (
    <div className="flex rounded-md bg-white/10 p-0.5 text-xs font-medium" role="radiogroup" aria-label="Source">
      {(
        [
          ['bridged', 'Best', 'Full quality; ad breaks switch to an ad-free source'],
          ['adfree', '360p ad-free', "Twitch's never-ad-stitched source, capped at 360p"],
        ] as const
      ).map(([value, label, hint]) => (
        <button
          key={value}
          type="button"
          role="radio"
          aria-checked={mode === value}
          title={hint}
          onClick={() => mode !== value && onChange(value)}
          className={cn(
            'rounded px-2 py-1 transition-colors',
            mode === value ? 'bg-twitch-600 text-white' : 'text-white/75 hover:text-white',
          )}
        >
          {label}
        </button>
      ))}
    </div>
  )
}
