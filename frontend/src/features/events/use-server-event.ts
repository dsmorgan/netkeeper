import { useEffect, useRef } from 'react'

import { useEventStreamSource } from './event-stream-context'

/**
 * Listens for one named SSE event type on the app's one shared connection
 * (`EventStreamProvider`; spec 14.1: the UI subscribes once, not once per page).
 *
 * The backend names each message by its event type (`ServerSentEvent(data=...,
 * event=event.type)` in `netkeeper/web/api/events.py`), so `EventSource`'s plain
 * `onmessage` never fires for it — only `addEventListener(type, ...)` does. This
 * re-attaches automatically after a reconnect: `useEventStream` opens a fresh
 * `EventSource` each time, `useEventStreamSource()` returns the new instance, and
 * that changed dependency reruns this effect on it. A payload that is not valid
 * JSON is swallowed rather than thrown, so one malformed message cannot take the
 * page down; `onEvent` itself is read from a ref, kept current by its own
 * effect, so a caller passing a fresh function every render does not thrash
 * the subscription below (React's rules of hooks: a ref is written in an
 * effect, never during render).
 */
export function useServerEvent<T = unknown>(type: string, onEvent: (data: T) => void): void {
  const source = useEventStreamSource()
  const handler = useRef(onEvent)
  useEffect(() => {
    handler.current = onEvent
  }, [onEvent])

  useEffect(() => {
    if (source === null) {
      return
    }
    const listener = (event: MessageEvent<string>) => {
      try {
        handler.current(JSON.parse(event.data) as T)
      } catch {
        // Malformed payload: ignore it rather than crash the page.
      }
    }
    source.addEventListener(type, listener)
    return () => source.removeEventListener(type, listener)
  }, [source, type])
}
