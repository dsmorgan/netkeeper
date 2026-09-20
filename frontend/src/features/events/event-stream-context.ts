import { createContext, useContext } from 'react'

import type { EventStreamStatus } from './use-event-stream'

export const EventStreamContext = createContext<EventStreamStatus>('disconnected')

/** Status of the app-wide SSE subscription owned by `EventStreamProvider`. */
export function useEventStreamStatus(): EventStreamStatus {
  return useContext(EventStreamContext)
}
