import type { ReactNode } from 'react'

import { EventStreamContext } from './event-stream-context'
import { useEventStream } from './use-event-stream'

const EVENTS_URL = '/api/v1/events'

/** Mounts once in the root layout so the whole UI shares one SSE connection. */
export function EventStreamProvider({ children }: { children: ReactNode }) {
  const stream = useEventStream(EVENTS_URL)
  return <EventStreamContext value={stream}>{children}</EventStreamContext>
}
