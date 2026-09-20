import { lazy, Suspense } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { useEventStream } from '@/lib/useEvents'
import { Layout } from '@/components/Layout'
import { UiStateProvider } from '@/lib/uiState'
import { Spinner } from '@/components/ui'
import Dashboard from '@/pages/Dashboard'
import Channels from '@/pages/Channels'
import Vods from '@/pages/Vods'
import Jobs from '@/pages/Jobs'
import Settings from '@/pages/Settings'

// Split out: hls.js is most of its weight, and only the player needs it.
const Watch = lazy(() => import('@/pages/Watch'))
import Setup from '@/pages/Setup'
import Login from '@/pages/Login'

export default function App() {
  const location = useLocation()
  const session = useQuery({
    queryKey: ['session'],
    queryFn: api.session,
    retry: 0,
    staleTime: 0,
  })

  const authenticated = session.data?.authenticated ?? false
  const { connected } = useEventStream(authenticated)

  if (session.isLoading) {
    return (
      <div className="grid min-h-screen place-items-center">
        <Spinner className="size-6" />
      </div>
    )
  }

  const setupComplete = session.data?.setup_complete ?? false

  if (!setupComplete && location.pathname !== '/setup') {
    return <Navigate to="/setup" replace />
  }
  if (setupComplete && !authenticated && location.pathname !== '/login') {
    return <Navigate to="/login" replace />
  }

  if (location.pathname === '/setup') {
    return <Setup />
  }
  if (location.pathname === '/login') {
    return <Login />
  }

  return (
    <UiStateProvider>
      <Layout connected={connected} username={session.data?.username ?? null}>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/watch" element={<WatchRoute />} />
          <Route path="/watch/:login" element={<WatchRoute />} />
          <Route path="/channels" element={<Channels />} />
          <Route path="/vods" element={<Vods />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Layout>
    </UiStateProvider>
  )
}

function WatchRoute() {
  return (
    <Suspense
      fallback={
        <div className="grid place-items-center py-24">
          <Spinner className="size-6" />
        </div>
      }
    >
      <Watch />
    </Suspense>
  )
}
