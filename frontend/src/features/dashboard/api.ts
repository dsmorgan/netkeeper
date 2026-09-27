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
import { runsQuery } from '@/features/linkedin/api'

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

// --- at a glance (P3-12) --------------------------------------------------------------

export type NextFirePage = components['schemas']['NextFirePage']
export type NextFire = components['schemas']['NextFireOut']
export type ChangedJobPage = components['schemas']['ChangedJobPage']
export type ChangedJob = components['schemas']['ChangedJobOut']
export type Inbound = components['schemas']['InboundOut']

export const dashboardKeys = {
  all: ['dashboard'] as const,
  nextFires: () => [...dashboardKeys.all, 'next-fires'] as const,
  changedJobs: () => [...dashboardKeys.all, 'changed-jobs'] as const,
  inbound: () => [...dashboardKeys.all, 'inbound'] as const,
}

/**
 * The next campaign steps due, soonest first (`GET /dashboard/next-fires`).
 *
 * The engine moves these every minute and says nothing when it does (there is
 * no server event for a tick), so this polls once a minute, as often as the
 * tick itself runs.
 */
export const nextFiresQuery = queryOptions({
  queryKey: dashboardKeys.nextFires(),
  queryFn: async ({ signal }): Promise<NextFirePage> => {
    const { data, response } = await api.GET('/api/v1/dashboard/next-fires', { signal })
    if (data === undefined) {
      throw new Error(`GET /api/v1/dashboard/next-fires returned ${response.status}`)
    }
    return data
  },
  refetchInterval: 60_000,
  retry: false,
})

/** Contacts whose position changed in the last 30 days (`GET /dashboard/changed-jobs`). */
export const changedJobsQuery = queryOptions({
  queryKey: dashboardKeys.changedJobs(),
  queryFn: async ({ signal }): Promise<ChangedJobPage> => {
    const { data, response } = await api.GET('/api/v1/dashboard/changed-jobs', { signal })
    if (data === undefined) {
      throw new Error(`GET /api/v1/dashboard/changed-jobs returned ${response.status}`)
    }
    return data
  },
  retry: false,
})

/** Inbound messages in the last seven days, and whether replies are detected yet. */
export const inboundQuery = queryOptions({
  queryKey: dashboardKeys.inbound(),
  queryFn: async ({ signal }): Promise<Inbound> => {
    const { data, response } = await api.GET('/api/v1/dashboard/inbound', { signal })
    if (data === undefined) {
      throw new Error(`GET /api/v1/dashboard/inbound returned ${response.status}`)
    }
    return data
  },
  retry: false,
})

/** The newest LinkedIn run, whatever it was: the browser card's last-run line. */
export const lastRunQuery = runsQuery({ limit: 1, offset: 0 })
