import { type ReactNode, useState } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  LayoutDashboard,
  ListVideo,
  LogOut,
  Menu,
  Settings as SettingsIcon,
  Tv,
  Wifi,
  WifiOff,
  X,
} from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Button } from './ui'

const NAV = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/channels', label: 'Channels', icon: Tv, end: false },
  { to: '/vods', label: 'VODs', icon: ListVideo, end: false },
  { to: '/jobs', label: 'Activity', icon: Activity, end: false },
  { to: '/settings', label: 'Settings', icon: SettingsIcon, end: false },
]

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
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const logout = useMutation({
    mutationFn: api.logout,
    onSuccess: () => {
      queryClient.clear()
      toast.success('Signed out')
      navigate('/login')
    },
  })

  const nav = (
    <nav className="flex flex-col gap-1">
      {NAV.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          onClick={() => setMobileOpen(false)}
          className={({ isActive }) =>
            cn(
              'flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors',
              isActive
                ? 'bg-twitch-600/15 text-white'
                : 'text-ink-300 hover:bg-ink-800 hover:text-ink-200',
            )
          }
        >
          <Icon className="size-4 shrink-0" aria-hidden />
          {label}
        </NavLink>
      ))}
    </nav>
  )

  return (
    <div className="flex min-h-screen">
      {/* Sidebar */}
      <aside className="fixed inset-y-0 left-0 hidden w-60 flex-col border-r border-ink-700/70 bg-ink-900 px-4 py-5 lg:flex">
        <Brand />
        <div className="mt-7 flex-1">{nav}</div>
        <Footer connected={connected} username={username} onLogout={() => logout.mutate()} />
      </aside>

      {/* Mobile drawer */}
      {mobileOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="absolute inset-0 bg-black/70"
            onClick={() => setMobileOpen(false)}
            aria-hidden
          />
          <aside className="absolute inset-y-0 left-0 flex w-64 flex-col border-r border-ink-700/70 bg-ink-900 px-4 py-5">
            <div className="flex items-center justify-between">
              <Brand />
              <Button variant="ghost" size="icon" onClick={() => setMobileOpen(false)} aria-label="Close menu">
                <X className="size-4" />
              </Button>
            </div>
            <div className="mt-7 flex-1">{nav}</div>
            <Footer connected={connected} username={username} onLogout={() => logout.mutate()} />
          </aside>
        </div>
      )}

      {/* Content */}
      <div className="flex min-w-0 flex-1 flex-col lg:pl-60">
        <header className="sticky top-0 z-30 flex items-center gap-3 border-b border-ink-700/70 bg-ink-950/85 px-4 py-3 backdrop-blur lg:hidden">
          <Button variant="ghost" size="icon" onClick={() => setMobileOpen(true)} aria-label="Open menu">
            <Menu className="size-5" />
          </Button>
          <Brand compact />
        </header>
        <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6 lg:px-8">{children}</main>
      </div>
    </div>
  )
}

function Brand({ compact }: { compact?: boolean }) {
  return (
    <div className="flex items-center gap-2.5">
      <span className="grid size-8 place-items-center rounded-lg bg-gradient-to-br from-twitch-500 to-jelly-500 text-sm font-bold text-white">
        J
      </span>
      {!compact && (
        <div className="leading-tight">
          <p className="text-sm font-semibold text-white">JellyTTV</p>
          <p className="text-[11px] text-ink-400">Twitch → Jellyfin</p>
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
}: {
  connected: boolean
  username: string | null
  onLogout: () => void
}) {
  return (
    <div className="mt-4 space-y-3 border-t border-ink-700/70 pt-4">
      <div className="flex items-center gap-2 px-1 text-[11px] text-ink-400">
        {connected ? (
          <>
            <Wifi className="size-3.5 text-emerald-400" aria-hidden />
            Live updates on
          </>
        ) : (
          <>
            <WifiOff className="size-3.5 text-amber-400" aria-hidden />
            Reconnecting…
          </>
        )}
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 truncate px-1 text-xs text-ink-300">{username ?? 'admin'}</span>
        <Button variant="ghost" size="icon" onClick={onLogout} aria-label="Sign out" title="Sign out">
          <LogOut className="size-4" />
        </Button>
      </div>
    </div>
  )
}
