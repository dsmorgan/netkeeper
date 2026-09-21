/**
 * Test helpers for this feature: a query client, and a router over the offline fetch.
 *
 * Not a test file — vitest collects `*.test.tsx` only — and nothing the app
 * imports. It exists so each test says what the backend answers and then reads
 * back exactly what was asked, which is how the round-trip assertions prove the
 * request body rather than trusting the component.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import type { ReactElement } from 'react'

import { jsonResponse, mockFetch } from '@/test/fetch'

export function renderWithClient(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return {
    client,
    ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>),
  }
}

export interface SeenRequest {
  method: string
  path: string
  search: string
  body: unknown
}

export type RouteHandler = (context: {
  body: unknown
  url: URL
  seen: SeenRequest[]
}) => Response | Promise<Response>

/**
 * Routes `METHOD /path` to a handler and records every request.
 *
 * An unrouted call answers 404 with a body naming it, so a test that forgot a
 * route reads as a missing route rather than as a mysterious failure.
 */
export function mockApi(routes: Record<string, RouteHandler>): SeenRequest[] {
  const seen: SeenRequest[] = []
  mockFetch(async (request) => {
    const url = new URL(request.url)
    let body: unknown = null
    if (request.method !== 'GET' && request.method !== 'DELETE') {
      body = await request
        .clone()
        .json()
        .catch(() => null)
    }
    seen.push({ method: request.method, path: url.pathname, search: url.search, body })
    const handler = routes[`${request.method} ${url.pathname}`]
    if (handler === undefined) {
      return jsonResponse({ detail: `no route for ${request.method} ${url.pathname}` }, 404)
    }
    return handler({ body, url, seen })
  })
  return seen
}

/** The requests made to one path, in order. */
export function requestsTo(seen: SeenRequest[], method: string, path: string): SeenRequest[] {
  return seen.filter((item) => item.method === method && item.path === path)
}
