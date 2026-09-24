/**
 * Every call the Settings page makes, through the generated client.
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import type { components } from '@/api/schema'

export type Posture = components['schemas']['PostureOut']
export type Protection = components['schemas']['ProtectionOut']

/**
 * Every protection the LinkedIn extractor has (spec section 9, P2-11), read-only.
 *
 * Built with no browser probe (`GET /posture` never attaches, CLAUDE.md), so
 * the `linkedin session` row always reads "unknown" here — a live check stays
 * `netkeeper preflight`, a terminal command.
 */
export const postureQuery = queryOptions({
  queryKey: ['posture'] as const,
  queryFn: async ({ signal }): Promise<Posture> => {
    const { data, error, response } = await api.GET('/api/v1/posture', { signal })
    if (data === undefined) {
      const detail = (error as { detail?: unknown } | undefined)?.detail
      const message = typeof detail === 'string' && detail !== '' ? detail : null
      throw new Error(message ?? `GET /api/v1/posture returned ${response.status}`)
    }
    return data
  },
})
