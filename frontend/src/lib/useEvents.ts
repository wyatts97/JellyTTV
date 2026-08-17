import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { AppEvent } from './types'

/**
 * Subscribes to the backend SSE feed and invalidates the relevant React Query
 * caches so the UI reflects worker activity without polling.
 */
export function useEventStream(enabled: boolean) {
  const queryClient = useQueryClient()
  const [connected, setConnected] = useState(false)
  const [lastEvent, setLastEvent] = useState<AppEvent | null>(null)
  const retry = useRef<number>(0)

  useEffect(() => {
    if (!enabled) return

    let source: EventSource | null = null
    let timer: number | undefined
    let closed = false

    const connect = () => {
      if (closed) return
      source = new EventSource('/api/events')

      source.onopen = () => {
        setConnected(true)
        retry.current = 0
      }

      source.onmessage = (message) => {
        let event: AppEvent
        try {
          event = JSON.parse(message.data)
        } catch {
          return
        }
        if (event.type === 'ping') return
        setLastEvent(event)

        switch (event.type) {
          case 'channel.live':
          case 'channel.offline':
          case 'channel.updated':
          case 'channels.changed':
            queryClient.invalidateQueries({ queryKey: ['channels'] })
            queryClient.invalidateQueries({ queryKey: ['dashboard'] })
            break
          case 'vods.synced':
          case 'vod.progress':
            queryClient.invalidateQueries({ queryKey: ['vods'] })
            queryClient.invalidateQueries({ queryKey: ['dashboard'] })
            break
          case 'library.published':
            queryClient.invalidateQueries({ queryKey: ['channels'] })
            break
          case 'job.started':
          case 'job.finished':
          case 'job.failed':
            queryClient.invalidateQueries({ queryKey: ['jobs'] })
            queryClient.invalidateQueries({ queryKey: ['dashboard'] })
            break
          case 'log':
            queryClient.invalidateQueries({ queryKey: ['dashboard'] })
            break
          default:
            break
        }
      }

      source.onerror = () => {
        setConnected(false)
        source?.close()
        source = null
        if (closed) return
        retry.current = Math.min(retry.current + 1, 6)
        timer = window.setTimeout(connect, 1000 * 2 ** retry.current)
      }
    }

    connect()

    return () => {
      closed = true
      if (timer) window.clearTimeout(timer)
      source?.close()
      setConnected(false)
    }
  }, [enabled, queryClient])

  return { connected, lastEvent }
}
