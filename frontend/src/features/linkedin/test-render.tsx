import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'

import { EventStreamContext } from '@/features/events/event-stream-context'
import type { EventStreamStatus } from '@/features/events/use-event-stream'
import { FakeEventSource } from '@/test/fake-event-source'
import { mockFetch } from '@/test/fetch'

import { LinkedInPage } from './linkedin-page'
import { backend, defaultHandlers, type Call, type Handler } from './test-support'

/**
 * The LinkedIn page on its own, against a fake backend and a fake SSE
 * connection — the page rather than the whole app, the same reason
 * `triage/test-render.tsx` renders `TriagePage` alone.
 *
 * `source` is the fake `EventSource` already "connected" (spec 14.1's one
 * shared subscription): a test calls `source.emit('run.progress', {...})` to
 * simulate the backend, exactly as `netkeeper/web/api/events.py` would send
 * it, and asserts the page updated without a reload.
 *
 * `setStatus` drives the same connection's reported status (M1: a test needs
 * `connected` -> `disconnected` -> `connected` to exercise the reconnect
 * recovery in `useRunEvents`) by re-rendering the tree with a fresh context
 * value — the fake `source` itself is untouched, since only the status a
 * consumer reads through `useEventStreamStatus()` needs to move.
 */
export function renderLinkedInPage(
  handlers: Record<string, Handler> = {},
  initialStatus: EventStreamStatus = 'connected',
) {
  const calls: Call[] = []
  mockFetch(backend(defaultHandlers(handlers), calls))
  const source = new FakeEventSource('/api/v1/events')
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  const tree = (status: EventStreamStatus) => (
    <QueryClientProvider client={queryClient}>
      <EventStreamContext value={{ status, source: source as unknown as EventSource }}>
        <LinkedInPage />
      </EventStreamContext>
    </QueryClientProvider>
  )

  const utils = render(tree(initialStatus))
  const setStatus = (status: EventStreamStatus): void => {
    utils.rerender(tree(status))
  }
  return { ...utils, calls, source, queryClient, setStatus }
}
