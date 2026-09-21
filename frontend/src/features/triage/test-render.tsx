import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'

import { mockFetch } from '@/test/fetch'

import { TriagePage } from './triage-page'
import { createFakeBackend, type FakeBackend, type FakeBackendOptions } from './test-backend'

/**
 * The triage screen on its own, against the fake backend.
 *
 * The page rather than the whole app: the shell's health poll and `/me` would
 * otherwise show up in the request counts the throughput test asserts on, and
 * the routing is covered once, separately.
 */
export function renderTriage(
  options: FakeBackendOptions & {
    /** An already-built backend, when a test has to arrange its state first. */
    backend?: FakeBackend
    /** Wrapped around the backend, to fail or delay a specific request. */
    intercept?: (request: Request, next: FakeBackend['handler']) => Promise<Response>
  } = {},
) {
  const backend = options.backend ?? createFakeBackend(options)
  const { intercept } = options
  mockFetch(
    intercept === undefined ? backend.handler : (request) => intercept(request, backend.handler),
  )
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <TriagePage />
    </QueryClientProvider>,
  )
  return { ...utils, backend, queryClient }
}

/** The name the card is showing, as the heading renders it. */
export async function currentName(): Promise<string> {
  const card = await screen.findByTestId('triage-card')
  const heading = card.querySelector('h2')
  return heading?.textContent ?? ''
}
