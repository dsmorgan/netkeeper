import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'

import { EventStreamContext } from '@/features/events/event-stream-context'
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
 */
export function renderLinkedInPage(handlers: Record<string, Handler> = {}) {
  const calls: Call[] = []
  mockFetch(backend(defaultHandlers(handlers), calls))
  const source = new FakeEventSource('/api/v1/events')
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  const utils = render(
    <QueryClientProvider client={queryClient}>
      <EventStreamContext value={{ status: 'connected', source: source as unknown as EventSource }}>
        <LinkedInPage />
      </EventStreamContext>
    </QueryClientProvider>,
  )
  return { ...utils, calls, source, queryClient }
}
