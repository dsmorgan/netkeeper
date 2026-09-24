import { useEffect, useRef } from 'react'

import { useEventStreamSource } from './event-stream-context'

/**
 * The wire shape of every SSE message (`Event.to_dict()`, `netkeeper/services/events.py`):
 * the whole envelope, not just the payload a publisher passed as `data=`.
 * `netkeeper/web/api/events.py` sends this JSON-encoded as the SSE `data:` field,
 * named by `event.type` as the SSE `event:` field — so `event.data` in a
 * `MessageEvent` is this envelope, and `.data` (lower case, the envelope's own
 * field) is the inner payload a caller actually wants. `tests/test_web_events.py`
 * pins the shape: `{"type": ..., "data": {...}, "user_id": ..., "ts": "...+00:00"}`.
 */
interface ServerEventEnvelope<T> {
  type: string
  data: T
  ts: string
  user_id: number | null
}

/**
 * Listens for one named SSE event type on the app's one shared connection
 * (`EventStreamProvider`; spec 14.1: the UI subscribes once, not once per page),
 * and hands the handler the envelope's inner `data` — never the envelope itself.
 * This is the one place that unwraps it; nothing downstream should ever parse
 * `event.data` a second time.
 *
 * The backend names each message by its event type (`ServerSentEvent(data=...,
 * event=event.type)` in `netkeeper/web/api/events.py`), so `EventSource`'s plain
 * `onmessage` never fires for it — only `addEventListener(type, ...)` does. This
 * re-attaches automatically after a reconnect: `useEventStream` opens a fresh
 * `EventSource` each time, `useEventStreamSource()` returns the new instance, and
 * that changed dependency reruns this effect on it. A payload that is not valid
 * JSON, or has no `data` field, is swallowed rather than thrown, so one malformed
 * message cannot take the page down; `onEvent` itself is read from a ref, kept
 * current by its own effect, so a caller passing a fresh function every render
 * does not thrash the subscription below (React's rules of hooks: a ref is
 * written in an effect, never during render) — and so the handler that runs for
 * an event is always the caller's latest one, never one captured at subscribe
 * time.
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
        const envelope = JSON.parse(event.data) as ServerEventEnvelope<T>
        handler.current(envelope.data)
      } catch {
        // Malformed payload: ignore it rather than crash the page.
      }
    }
    source.addEventListener(type, listener)
    return () => source.removeEventListener(type, listener)
  }, [source, type])
}
