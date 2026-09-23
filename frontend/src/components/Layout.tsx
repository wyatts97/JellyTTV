import { useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  LayoutDashboard,
  ListVideo,
  LogOut,
  Menu,
  MonitorPlay,
  PanelLeftClose,
  PanelLeftOpen,
  Settings as SettingsIcon,
  Tv,
  Wifi,
  WifiOff,
  X,
} from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/lib/api'
import { useDismissableLayer } from '@/lib/a11y'
import { useUiState } from '@/lib/uiState'
import { cn } from '@/lib/utils'
import { Button } from './ui'

const NAV = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/watch', label: 'Watch', icon: MonitorPlay, end: false },
  { to: '/channels', label: 'Channels', icon: Tv, end: false },
  { to: '/vods', label: 'VODs', icon: ListVideo, end: false },
  { to: '/jobs', label: 'Activity', icon: Activity, end: false },
  { to: '/settings', label: 'Settings', icon: SettingsIcon, end: false },
]

const EXPANDED_WIDTH = '15rem'
const RAIL_WIDTH = '4rem'

export function Layout({
  children,
  connected,
  username,
}: {
  children: ReactNode
  connected: boolean
  username: string | null
}) {
  const [mobileOpen, setMobileOpen] = useState(false)
  const drawerRef = useRef<HTMLElement>(null)
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()
  const { sidebar, toggleSidebar, theater } = useUiState()

  useDismissableLayer({
    open: mobileOpen,
    onClose: () => setMobileOpen(false),
    panelRef: drawerRef,
  })

  const logout = useMutation({
    mutationFn: api.logout,
    onSuccess: () => {
      queryClient.clear()
      toast.success('Signed out')
      navigate('/login')
    },
  })

  const rail = sidebar === 'rail'
  // The player wants every pixel; a settings form does not. Long lines of text
  // are harder to read, so only the watch page gives up the reading measure.
  const fullWidth = location.pathname.startsWith('/watch')
  // One value drives the sidebar and the content offset, so they can never
  // disagree - which is what a hard-coded `lg:pl-60` guaranteed they would.
  const sidebarWidth = theater ? '0rem' : rail ? RAIL_WIDTH : EXPANDED_WIDTH

  return (
    <div
      className="flex min-h-screen"
      style={{ '--sidebar-w': sidebarWidth } as CSSProperties}
    >
      <a
        href="#main"
        className="sr-only z-50 focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:rounded-lg focus:bg-ink-800 focus:px-3 focus:py-2 focus:text-sm focus:text-white"
      >
        Skip to content
      </a>

      {/* Sidebar: expanded, or collapsed to an icon rail. Theater mode slides
          it out of the way entirely. */}
      <aside
        aria-label="Main navigation"
        style={{ width: rail ? RAIL_WIDTH : EXPANDED_WIDTH }}
        className={cn(
          'fixed inset-y-0 left-0 z-30 hidden flex-col border-r border-ink-700/70 bg-ink-900 py-5 transition-[width,transform] duration-200 lg:flex',
          rail ? 'px-2' : 'px-4',
          theater && '-translate-x-full',
        )}
      >
        <Brand rail={rail} />
        <div className="mt-7 flex-1">
          <Nav rail={rail} />
        </div>
        <Footer
          rail={rail}
          connected={connected}
          username={username}
          onLogout={() => logout.mutate()}
          onToggle={toggleSidebar}
        />
      </aside>

      {/* Mobile drawer */}
      {mobileOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="absolute inset-0 bg-black/70"
            onClick={() => setMobileOpen(false)}
            aria-hidden
          />
          <aside
            ref={drawerRef}
            aria-label="Main navigation"
            className="absolute inset-y-0 left-0 flex w-64 flex-col border-r border-ink-700/70 bg-ink-900 px-4 py-5"
          >
            <div className="flex items-center justify-between">
              <Brand />
              <Button
                variant="ghost"
                size="icon"
                onClick={() => setMobileOpen(false)}
                aria-label="Close menu"
              >
                <X className="size-4" />
              </Button>
            </div>
            <div className="mt-7 flex-1" onClick={() => setMobileOpen(false)}>
              <Nav />
            </div>
            <Footer
              connected={connected}
              username={username}
              onLogout={() => logout.mutate()}
            />
          </aside>
        </div>
      )}

      {/* Content */}
      <div className="flex min-w-0 flex-1 flex-col lg:pl-[var(--sidebar-w)]">
        {!theater && (
          <header className="sticky top-0 z-30 flex items-center gap-3 border-b border-ink-700/70 bg-ink-950/85 px-4 py-3 backdrop-blur lg:hidden">
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setMobileOpen(true)}
              aria-label="Open menu"
            >
              <Menu className="size-5" />
            </Button>
            <Brand compact />
          </header>
        )}
        <main
          id="main"
          className={cn(
            'w-full flex-1',
            theater ? 'px-0 py-0' : fullWidth ? 'px-3 py-4 sm:px-4 lg:px-6' : 'mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8',
          )}
        >
          {children}
        </main>
      </div>
    </div>
  )
}

function Nav({ rail }: { rail?: boolean }) {
  return (
    <nav className="flex flex-col gap-1">
      {NAV.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          title={rail ? label : undefined}
          className={({ isActive }) =>
            cn(
              'group relative flex items-center rounded-lg py-2 text-sm transition-colors',
              rail ? 'justify-center px-0' : 'gap-3 px-3',
              isActive
                ? 'bg-twitch-600/15 text-white'
                : 'text-ink-300 hover:bg-ink-800 hover:text-ink-200',
            )
          }
        >
          <Icon className="size-4 shrink-0" aria-hidden />
          {rail ? (
            <>
              <span className="sr-only">{label}</span>
              {/* The label is still reachable on hover and on keyboard focus,
                  so a collapsed rail never becomes a guessing game. */}
              <span className="pointer-events-none absolute left-full z-50 ml-2 hidden whitespace-nowrap rounded-md border border-ink-700 bg-ink-800 px-2 py-1 text-xs text-ink-200 shadow-lg group-hover:block group-focus-visible:block">
                {label}
              </span>
            </>
          ) : (
            label
          )}
        </NavLink>
      ))}
    </nav>
  )
}

function Brand({ compact, rail }: { compact?: boolean; rail?: boolean }) {
  return (
    <div className={cn('flex items-center gap-2.5', rail && 'justify-center')}>
      <img
        src="/icon-192.png"
        alt="JellyTTV"
        className="size-8 shrink-0 rounded-lg"
      />
      {!compact && !rail && (
        <div className="leading-tight">
          <p className="text-sm font-semibold text-white">JellyTTV</p>
          <p className="text-xs text-ink-400">Twitch, ad-free</p>
        </div>
      )}
      {compact && <p className="text-sm font-semibold text-white">JellyTTV</p>}
    </div>
  )
}

function Footer({
  connected,
  username,
  onLogout,
  onToggle,
  rail,
}: {
  connected: boolean
  username: string | null
  onLogout: () => void
  onToggle?: () => void
  rail?: boolean
}) {
  const connection = connected ? 'Live updates on' : 'Reconnecting…'
  return (
    <div
      className={cn(
        'mt-4 space-y-3 border-t border-ink-700/70 pt-4',
        rail && 'flex flex-col items-center gap-2 space-y-0',
      )}
    >
      <div
        className={cn('flex items-center gap-2 text-xs text-ink-400', !rail && 'px-1')}
        title={rail ? connection : undefined}
      >
        {connected ? (
          <Wifi className="size-3.5 text-emerald-400" aria-hidden />
        ) : (
          <WifiOff className="size-3.5 text-amber-400" aria-hidden />
        )}
        {rail ? <span className="sr-only">{connection}</span> : connection}
      </div>
      <div className={cn('flex items-center gap-2', rail ? 'flex-col' : 'justify-between')}>
        {!rail && (
          <span className="min-w-0 truncate px-1 text-xs text-ink-300">{username ?? 'admin'}</span>
        )}
        <div className={cn('flex items-center gap-1', rail && 'flex-col')}>
          {onToggle && (
            <Button
              variant="ghost"
              size="icon"
              onClick={onToggle}
              aria-label={rail ? 'Expand sidebar' : 'Collapse sidebar'}
              title={`${rail ? 'Expand' : 'Collapse'} sidebar ([)`}
              className="hidden lg:inline-flex"
            >
              {rail ? <PanelLeftOpen className="size-4" /> : <PanelLeftClose className="size-4" />}
            </Button>
          )}
          <Button
            variant="ghost"
            size="icon"
            onClick={onLogout}
            aria-label="Sign out"
            title={rail ? `Sign out (${username ?? 'admin'})` : 'Sign out'}
          >
            <LogOut className="size-4" />
          </Button>
        </div>
      </div>
    </div>
  )
}
