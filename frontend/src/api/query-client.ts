import { QueryClient, type QueryClientConfig } from '@tanstack/react-query'

/** How many times a query is retried after its first failure, at most. */
export const MAX_QUERY_RETRIES = 2

/**
 * The HTTP status a failed call carried, if it carried one.
 *
 * Each feature module throws its own error class (`ApiError`, `ApiFailure`,
 * `TriageError`, …), and every one of them exposes a numeric `status`. Reading
 * the field rather than checking `instanceof` against each class keeps this
 * policy from needing an import per feature, and from silently missing the
 * next one somebody adds.
 */
function statusOf(error: unknown): number | null {
  if (typeof error !== 'object' || error === null || !('status' in error)) return null
  const { status } = error
  return typeof status === 'number' ? status : null
}

/**
 * Whether a failed query is worth asking again.
 *
 * A 4xx is the backend's considered answer: a 404, 409 or 422 comes back
 * identically every time, so retrying only makes the person wait (#93 measured
 * seven seconds for a missing import run). A network failure — `fetch`
 * rejecting, so no status at all — or a 5xx may be transient, and gets
 * `MAX_QUERY_RETRIES` more tries.
 */
export function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  if (failureCount >= MAX_QUERY_RETRIES) return false
  const status = statusOf(error)
  return status === null || status >= 500
}

/**
 * The app's `QueryClient`. `main.tsx` builds the production one from this, and
 * the test renderers pass overrides on top, so both start from the same
 * defaults and differ only where a test says so.
 */
export function createQueryClient(config: QueryClientConfig = {}): QueryClient {
  const { defaultOptions = {}, ...rest } = config
  return new QueryClient({
    ...rest,
    defaultOptions: {
      ...defaultOptions,
      queries: { retry: shouldRetryQuery, ...defaultOptions.queries },
    },
  })
}
