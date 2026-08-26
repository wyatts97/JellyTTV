import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Toaster } from 'sonner'
import App from './App'
import { ApiError } from './lib/api'
import './index.css'

const queryClient = new QueryClient({
  // App only evaluates the session query when it mounts, so a cookie that
  // expires mid-visit used to leave every other query 401ing invisibly while
  // the page sat there. Re-checking the session on any 401 lets App's existing
  // redirect to /login fire instead.
  queryCache: new QueryCache({
    onError: (error, query) => {
      if (error instanceof ApiError && error.status === 401 && query.queryKey[0] !== 'session') {
        void queryClient.invalidateQueries({ queryKey: ['session'] })
      }
    },
  }),
  defaultOptions: {
    queries: {
      // Retrying a 4xx just delays the error state - the answer will not change.
      retry: (failureCount, error) => {
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false
        return failureCount < 1
      },
      refetchOnWindowFocus: false,
      staleTime: 5_000,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
        <Toaster
          theme="dark"
          position="bottom-right"
          toastOptions={{
            style: {
              background: '#12141c',
              border: '1px solid #212533',
              color: '#c8ccd8',
            },
          }}
        />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
