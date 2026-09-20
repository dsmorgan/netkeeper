import { useEffect, useState } from 'react'

export type EventStreamStatus = 'connected' | 'disconnected'

const INITIAL_DELAY_MS = 1_000
const MAX_DELAY_MS = 30_000

/** Exponential backoff: 1s, 2s, 4s, ... capped at 30s. */
export function backoffDelay(attempt: number): number {
  return Math.min(MAX_DELAY_MS, INITIAL_DELAY_MS * 2 ** attempt)
}

/**
 * Keeps one EventSource open to the backend's SSE stream (spec 14.1: the UI
 * subscribes once) and reports whether it is connected.
 *
 * The browser's built-in retry gives up after a non-200 response, and the
 * endpoint does not exist until P0-04 lands, so this hook owns reconnection:
 * close on error, reopen after a growing delay, reset the delay once a
 * connection opens. Message handling arrives with the first consumer.
 */
export function useEventStream(url: string): EventStreamStatus {
  const [status, setStatus] = useState<EventStreamStatus>('disconnected')

  useEffect(() => {
    // Not in this runtime (jsdom, for one): stay calmly disconnected.
    if (typeof EventSource === 'undefined') {
      return
    }

    let source: EventSource | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    let attempt = 0
    let stopped = false

    const connect = () => {
      if (stopped) {
        return
      }
      source = new EventSource(url)
      source.onopen = () => {
        attempt = 0
        setStatus('connected')
      }
      source.onerror = () => {
        source?.close()
        source = null
        setStatus('disconnected')
        timer = setTimeout(connect, backoffDelay(attempt))
        attempt += 1
      }
    }

    connect()

    return () => {
      stopped = true
      clearTimeout(timer)
      source?.close()
    }
  }, [url])

  return status
}
