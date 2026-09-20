import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, MessageSquare, MessageSquareOff, Radio, Tv, Users } from 'lucide-react'
import { api } from '@/lib/api'
import type { Channel, WatchMode } from '@/lib/types'
import { LivePlayer } from '@/components/LivePlayer'
import {
  Badge,
  Card,
  EmptyState,
  LiveBadge,
  PageHeader,
  PlayOverlay,
  QueryError,
  Skeleton,
  SkeletonCards,
} from '@/components/ui'
import { PREF, readEnumPref, readPref, writePref } from '@/lib/prefs'
import { useUiState } from '@/lib/uiState'
import { cn, formatNumber, formatUptime } from '@/lib/utils'

// Wide enough for Twitch's own embed to lay out, narrow enough to leave the
// player the majority of a laptop screen.
const CHAT_MIN_WIDTH = 260
const CHAT_MAX_WIDTH = 520
const CHAT_DEFAULT_WIDTH = 340

export default function Watch() {
  const { login } = useParams()
  return login ? <WatchChannel login={login.toLowerCase()} /> : <WatchIndex />
}

/* ------------------------------------------------------------ channel grid */
function WatchIndex() {
  const channels = useQuery({ queryKey: ['channels'], queryFn: api.channels, refetchInterval: 60_000 })

  if (channels.isLoading) {
    return (
      <div className="space-y-6">
        <PageHeader title="Watch" description="Loading channels…" />
        <SkeletonCards count={6} cardClassName="h-64" />
      </div>
    )
  }
  if (channels.error) return <QueryError error={channels.error} title="Could not load channels" />

  const rows = (channels.data ?? []).filter((c) => c.enabled && c.live_enabled)
  const live = rows.filter((c) => c.is_live)
  const offline = rows.filter((c) => !c.is_live)

  return (
    <div className="space-y-6">
      <PageHeader
        title="Watch"
        description={`${live.length} of ${rows.length} channels live · played right here, with ad breaks switched to a clean source`}
      />

      {live.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Radio className="size-7" />}
            title="Nobody is live right now"
            description="Turn on go-live notifications in Settings and your phone will tell you when someone starts."
          />
        </Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {live.map((channel) => (
            <LiveTile key={channel.id} channel={channel} />
          ))}
        </div>
      )}

      {offline.length > 0 && (
        <div>
          <h2 className="mb-3 text-sm font-medium text-ink-300">Offline</h2>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {offline.map((channel) => (
              <Link
                key={channel.id}
                to={`/watch/${channel.twitch_login}`}
                className="flex items-center gap-3 rounded-lg border border-ink-700/70 bg-ink-900 px-3 py-2.5 hover:border-ink-600"
              >
                <img
                  src={`/api/channels/${channel.id}/avatar`}
                  alt=""
                  className="size-8 rounded-full bg-ink-700 opacity-70"
                  loading="lazy"
                />
                <span className="truncate text-sm text-ink-300">{channel.display_name}</span>
              </Link>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function LiveTile({ channel }: { channel: Channel }) {
  return (
    <Link
      to={`/watch/${channel.twitch_login}`}
      className="group overflow-hidden rounded-lg border border-ink-700/70 bg-ink-850 transition-colors hover:border-twitch-500/60"
    >
      <div className="relative">
        <img
          src={`/api/channels/${channel.id}/thumbnail`}
          alt=""
          className="aspect-video w-full bg-ink-800 object-cover"
          loading="lazy"
        />
        <PlayOverlay />
      </div>
      <div className="space-y-1.5 p-3.5">
        <div className="flex items-center gap-2">
          <img src={`/api/channels/${channel.id}/avatar`} alt="" className="size-6 rounded-full bg-ink-700" />
          <span className="min-w-0 flex-1 truncate text-sm font-medium text-white">{channel.display_name}</span>
          <LiveBadge />
        </div>
        <p className="line-clamp-2 text-xs text-ink-300">{channel.live_title ?? 'Untitled stream'}</p>
        <div className="flex items-center gap-3 text-xs text-ink-400">
          {channel.live_game && <span className="truncate">{channel.live_game}</span>}
          <span className="flex items-center gap-1">
            <Users className="size-3" aria-hidden />
            {formatNumber(channel.live_viewers)}
          </span>
        </div>
      </div>
    </Link>
  )
}

/* ---------------------------------------------------------- single channel */
function WatchChannel({ login }: { login: string }) {
  const queryClient = useQueryClient()
  const { theater, setTheater } = useUiState()
  const [mode, setMode] = useState<WatchMode>(() =>
    readEnumPref(PREF.mode, 'bridged', ['bridged', 'adfree']),
  )
  const [chat, setChat] = useState<'on' | 'off'>(() => readEnumPref(PREF.chat, 'on', ['on', 'off']))
  const [chatWidth, setChatWidth] = useState(() =>
    clampChatWidth(readPref(PREF.chatWidth, CHAT_DEFAULT_WIDTH)),
  )

  // Theater mode belongs to the player, not the route: leaving the page must
  // give the sidebar and the page chrome back.
  useEffect(() => () => setTheater(false), [setTheater])

  const info = useQuery({
    queryKey: ['watch', login],
    queryFn: () => api.watchInfo(login),
    // Offline channels are re-checked often so playback starts on its own when
    // they go live (SSE also invalidates this on `channel.live`).
    refetchInterval: (query) => (query.state.data?.is_live ? 60_000 : 20_000),
  })
  const channels = useQuery({ queryKey: ['channels'], queryFn: api.channels, refetchInterval: 60_000 })

  const changeMode = useCallback((next: WatchMode) => {
    setMode(next)
    writePref(PREF.mode, next)
  }, [])
  const toggleChat = () => {
    const next = chat === 'on' ? 'off' : 'on'
    setChat(next)
    writePref(PREF.chat, next)
  }
  const commitChatWidth = useCallback((width: number) => {
    const clamped = clampChatWidth(width)
    setChatWidth(clamped)
    writePref(PREF.chatWidth, clamped)
  }, [])
  // Relative, and applied to the latest width rather than the one this handler
  // closed over: a held arrow key fires faster than React re-renders, and every
  // press after the first would otherwise recompute from the same stale value.
  const nudgeChatWidth = useCallback((delta: number) => {
    setChatWidth((current) => {
      const clamped = clampChatWidth(current + delta)
      writePref(PREF.chatWidth, clamped)
      return clamped
    })
  }, [])
  const onOffline = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['watch', login] })
  }, [queryClient, login])

  if (info.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-9 w-64" />
        <Skeleton className="aspect-video w-full rounded-xl" />
      </div>
    )
  }
  if (info.error || !info.data) return <QueryError error={info.error} title="Could not load this channel" />

  const channel = info.data
  const others = (channels.data ?? []).filter(
    (c) => c.is_live && c.enabled && c.live_enabled && c.twitch_login !== login,
  )
  const chatSrc = `https://www.twitch.tv/embed/${encodeURIComponent(login)}/chat?parent=${encodeURIComponent(
    window.location.hostname,
  )}&darkpopout`

  return (
    <div className={cn('space-y-4', theater && 'space-y-2')}>
      <div className={cn('flex items-center gap-3', theater && 'hidden')}>
        <Link
          to="/watch"
          className="grid size-8 place-items-center rounded-lg text-ink-300 hover:bg-ink-800 hover:text-white"
          aria-label="All channels"
        >
          <ArrowLeft className="size-4" />
        </Link>
        <img src={channel.avatar_url} alt="" className="size-9 rounded-full bg-ink-700" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-base font-semibold text-white">{channel.display_name}</h1>
            {channel.is_live ? <LiveBadge /> : <Badge>Offline</Badge>}
          </div>
          {channel.is_live && (
            <p className="flex flex-wrap items-center gap-x-3 text-xs text-ink-400">
              {channel.game && <span>{channel.game}</span>}
              <span className="flex items-center gap-1">
                <Users className="size-3" aria-hidden />
                {formatNumber(channel.viewers)}
              </span>
              <span>{formatUptime(channel.started_at)}</span>
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={toggleChat}
          className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-ink-300 hover:bg-ink-800 hover:text-white"
        >
          {chat === 'on' ? <MessageSquareOff className="size-4" /> : <MessageSquare className="size-4" />}
          <span className="hidden sm:inline">{chat === 'on' ? 'Hide chat' : 'Show chat'}</span>
        </button>
      </div>

      <div
        className={cn('grid gap-4', chat === 'on' && 'lg:grid-cols-[minmax(0,1fr)_var(--chat-w)]')}
        style={{ '--chat-w': `${chatWidth}px` } as CSSProperties}
      >
        <div className="min-w-0 space-y-3">
          {!channel.playable ? (
            <OfflinePanel channel={channel} message="Live playback is disabled for this channel in Channels." />
          ) : channel.is_live ? (
            <LivePlayer
              key={login}
              login={login}
              mode={mode}
              onModeChange={changeMode}
              poster={channel.thumbnail_url}
              onOffline={onOffline}
            />
          ) : (
            <OfflinePanel
              channel={channel}
              message="Offline. Playback starts here automatically when the stream goes live."
            />
          )}
          {channel.is_live && channel.title && <p className="text-sm text-ink-200">{channel.title}</p>}
        </div>

        {chat === 'on' && (
          <div className="relative lg:sticky lg:top-6 lg:self-start">
            <ChatResizer width={chatWidth} onChange={commitChatWidth} onNudge={nudgeChatWidth} />
            <aside
              className={cn(
                'h-[60vh] overflow-hidden rounded-xl border border-ink-700/70 bg-ink-900',
                theater ? 'lg:h-[calc(100vh-4rem)]' : 'lg:h-[calc(100vh-7rem)]',
              )}
            >
              <iframe
                key={login}
                src={chatSrc}
                title={`${channel.display_name} chat`}
                className="size-full"
              />
            </aside>
          </div>
        )}
      </div>

      {others.length > 0 && !theater && (
        <div>
          <h2 className="mb-2 text-sm font-medium text-ink-300">Also live</h2>
          <div className="flex gap-3 overflow-x-auto pb-2">
            {others.map((c) => (
              <Link
                key={c.id}
                to={`/watch/${c.twitch_login}`}
                className="group w-52 shrink-0 overflow-hidden rounded-lg border border-ink-700/70 bg-ink-850 hover:border-twitch-500/60"
              >
                <div className="relative">
                  <img
                    src={`/api/channels/${c.id}/thumbnail`}
                    alt=""
                    className="aspect-video w-full bg-ink-800 object-cover"
                    loading="lazy"
                  />
                  <PlayOverlay />
                </div>
                <div className="p-2">
                  <p className="truncate text-xs font-medium text-white">{c.display_name}</p>
                  <p className="truncate text-xs text-ink-400">{c.live_game ?? c.live_title ?? ''}</p>
                </div>
              </Link>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function OfflinePanel({
  channel,
  message,
}: {
  channel: { avatar_url: string; display_name: string }
  message: string
}) {
  return (
    <div className="grid aspect-video w-full place-items-center rounded-xl border border-ink-700/70 bg-ink-900 p-6 text-center">
      <div className="space-y-3">
        <img src={channel.avatar_url} alt="" className="mx-auto size-16 rounded-full bg-ink-700 opacity-80" />
        <p className="text-sm text-ink-300">{message}</p>
        <Link to="/watch" className="inline-flex items-center gap-1.5 text-xs text-twitch-400 hover:underline">
          <Tv className="size-3.5" /> Browse live channels
        </Link>
      </div>
    </div>
  )
}


function clampChatWidth(width: number): number {
  if (!Number.isFinite(width)) return CHAT_DEFAULT_WIDTH
  return Math.min(CHAT_MAX_WIDTH, Math.max(CHAT_MIN_WIDTH, Math.round(width)))
}

/**
 * Drag handle on the chat panel's left edge.
 *
 * Keyboard accessible as well as draggable: chat width is a real preference,
 * and a mouse-only control would put it out of reach for anyone who does not
 * use one. Rendered only at `lg`, where the two columns exist at all.
 */
function ChatResizer({
  width,
  onChange,
  onNudge,
}: {
  width: number
  onChange: (width: number) => void
  onNudge: (delta: number) => void
}) {
  const dragging = useRef(false)

  useEffect(() => {
    const onMove = (event: PointerEvent) => {
      if (!dragging.current) return
      // The panel is anchored to the right edge, so its width is whatever is
      // left between the pointer and that edge.
      onChange(window.innerWidth - event.clientX)
    }
    const onUp = () => {
      dragging.current = false
      document.body.style.userSelect = ''
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [onChange])

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize chat"
      aria-valuenow={width}
      aria-valuemin={CHAT_MIN_WIDTH}
      aria-valuemax={CHAT_MAX_WIDTH}
      tabIndex={0}
      onPointerDown={(event) => {
        event.preventDefault()
        dragging.current = true
        document.body.style.userSelect = 'none'
      }}
      onKeyDown={(event) => {
        if (event.key === 'ArrowLeft') onNudge(24)
        else if (event.key === 'ArrowRight') onNudge(-24)
        else return
        event.preventDefault()
      }}
      className="absolute -left-3 top-0 z-10 hidden h-full w-3 cursor-col-resize touch-none items-center justify-center lg:flex"
    >
      <span className="h-10 w-1 rounded-full bg-ink-700 transition-colors hover:bg-twitch-500" />
    </div>
  )
}
