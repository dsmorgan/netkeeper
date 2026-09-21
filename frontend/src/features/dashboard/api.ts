/**
 * What the dashboard's setup path fetches, through the generated client.
 *
 * `GET /api/v1/contacts/stats` (P1-25, spec 10.1) is the one source for
 * contact counts and triage progress; nothing here recomputes a count the
 * backend already has. The open-imports query reuses the `status` filter
 * `GET /imports` already exposes for the same reason `netkeeper import runs
 * --status draft` does (#90): a second, dashboard-only way to ask "is an
 * import still open" would just be a second dialect of the same question.
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import type { components } from '@/api/schema'

export type ContactStats = components['schemas']['ContactStatsOut']
export type ImportRunPage = components['schemas']['ImportRunPage']

/** Counts and triage progress over the live contacts (spec 10.1, P1-25). */
export const statsQuery = queryOptions({
  queryKey: ['contacts', 'stats'],
  queryFn: async ({ signal }): Promise<ContactStats> => {
    const { data, response } = await api.GET('/api/v1/contacts/stats', { signal })
    if (data === undefined) {
      throw new Error(`GET /api/v1/contacts/stats returned ${response.status}`)
    }
    return data
  },
  retry: false,
})

/**
 * The newest draft a `--dry-run` or a refused commit left open (spec 10.5, #90).
 *
 * The setup step only needs to know whether an import is mid-flight and,
 * when it is, which run to resume, so one row is enough.
 */
export const openImportsQuery = queryOptions({
  queryKey: ['imports', 'runs', { status: 'draft' as const, limit: 1, offset: 0 }],
  queryFn: async ({ signal }): Promise<ImportRunPage> => {
    const { data, response } = await api.GET('/api/v1/imports', {
      params: { query: { status: 'draft', limit: 1, offset: 0 } },
      signal,
    })
    if (data === undefined) {
      throw new Error(`GET /api/v1/imports?status=draft returned ${response.status}`)
    }
    return data
  },
  retry: false,
})
