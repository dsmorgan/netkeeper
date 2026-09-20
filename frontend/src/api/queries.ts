import { queryOptions } from '@tanstack/react-query'

import { api } from './client'

/**
 * Backend liveness. Polled so the top-bar indicator stays honest; no retries
 * because a failed poll is itself the answer.
 */
export const healthQuery = queryOptions({
  queryKey: ['health'],
  queryFn: async ({ signal }) => {
    const { data, response } = await api.GET('/api/v1/health', { signal })
    if (data === undefined) {
      throw new Error(`GET /api/v1/health returned ${response.status}`)
    }
    return data
  },
  retry: false,
  refetchInterval: 15_000,
})

/** The current user, resolved by the backend's `CurrentUser` dependency (spec 14.1). */
export const meQuery = queryOptions({
  queryKey: ['me'],
  queryFn: async ({ signal }) => {
    const { data, response } = await api.GET('/api/v1/me', { signal })
    if (data === undefined) {
      throw new Error(`GET /api/v1/me returned ${response.status}`)
    }
    return data
  },
  retry: false,
  staleTime: 60_000,
})
