import { useEffect, useState } from 'react'

export type EventStreamStatus = 'connected' | 'disconnected'

export interface EventStream {
  status: EventStreamStatus
  /**
   * The live `EventSource`, or `null` before the first connect, while
   * disconnected, and in a runtime (jsdom, for one) that has none.
   *
   * Identity changes on every reconnect — a fresh `EventSource` each time —
   * so a `useEffect` that depends on it (`useServerEvent`) re-subscribes its
   * named-event listeners automatically rather than listening on a closed
   * connection forever.
   */
  source: EventSource | null
}

const INITIAL_DELAY_MS = 1_000
const MAX_DELAY_MS = 30_000

/** Exponential backoff: 1s, 2s, 4s, ... capped at 30s. */
export function backoffDelay(attempt: number): number {
  return Math.min(MAX_DELAY_MS, INITIAL_DELAY_MS * 2 ** attempt)
}

/**
 * Keeps one EventSource open to the backend's SSE stream (spec 14.1: the UI
 * subscribes once), reports whether it is connected, and hands back the
 * connection itself so a page can listen for the named events it cares
 * about (`useServerEvent`).
 *
 * The browser's built-in retry gives up after a non-200 response, so this
 * hook owns reconnection: close on error, reopen after a growing delay,
 * reset the delay once a connection opens.
 */
export function useEventStream(url: string): EventStream {
  const [status, setStatus] = useState<EventStreamStatus>('disconnected')
  const [source, setSource] = useState<EventSource | null>(null)

  useEffect(() => {
    // Not in this runtime (jsdom, for one): stay calmly disconnected.
    if (typeof EventSource === 'undefined') {
      return
    }

    let current: EventSource | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    let attempt = 0
    let stopped = false

    const connect = () => {
      if (stopped) {
        return
      }
      current = new EventSource(url)
      setSource(current)
      current.onopen = () => {
        attempt = 0
        setStatus('connected')
      }
      current.onerror = () => {
        current?.close()
        current = null
        setStatus('disconnected')
        setSource(null)
        timer = setTimeout(connect, backoffDelay(attempt))
        attempt += 1
      }
    }

    connect()

    return () => {
      stopped = true
      clearTimeout(timer)
      current?.close()
    }
  }, [url])

  return { status, source }
}
