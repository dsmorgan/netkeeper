import { createContext, useContext } from 'react'

import type { EventStream, EventStreamStatus } from './use-event-stream'

const DISCONNECTED: EventStream = { status: 'disconnected', source: null }

export const EventStreamContext = createContext<EventStream>(DISCONNECTED)

/** Status of the app-wide SSE subscription owned by `EventStreamProvider`. */
export function useEventStreamStatus(): EventStreamStatus {
  return useContext(EventStreamContext).status
}

/** The app-wide SSE connection itself, for `useServerEvent` to listen on. */
export function useEventStreamSource(): EventSource | null {
  return useContext(EventStreamContext).source
}
