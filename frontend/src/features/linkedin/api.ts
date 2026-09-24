/**
 * Every call the LinkedIn page makes, through the generated client
 * (`openapi-fetch` over `schema.d.ts`; P2-12, built on P2-10's `/linkedin`
 * resource). Nothing here hand-writes a URL shape or a response type.
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'

import type {
  BrowserLaunch,
  BudgetStatus,
  Heat,
  LinkedInStatus,
  Pin,
  RunAccepted,
  RunKind,
  RunPage,
  RunStatus,
  Run as RunType,
  Schedule,
} from './types'

/** A failed call, carrying the status so a caller can tell 409 from 422 from 503. */
export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/** FastAPI's `{"detail": ...}`, dug out; every non-2xx here answers one shape or the other. */
export function apiError(error: unknown, response: Response, what: string): ApiError {
  const detail = (error as { detail?: unknown } | null | undefined)?.detail
  if (typeof detail === 'string' && detail !== '') {
    return new ApiError(detail, response.status)
  }
  if (Array.isArray(detail)) {
    const first = detail[0] as { msg?: unknown } | undefined
    if (typeof first?.msg === 'string' && first.msg !== '') {
      return new ApiError(first.msg, response.status)
    }
  }
  return new ApiError(`${what} returned ${response.status}`, response.status)
}

export const linkedinKeys = {
  all: ['linkedin'] as const,
  status: () => [...linkedinKeys.all, 'status'] as const,
  browser: () => [...linkedinKeys.all, 'browser'] as const,
  budget: () => [...linkedinKeys.all, 'budget'] as const,
  heat: () => [...linkedinKeys.all, 'heat'] as const,
  pins: () => [...linkedinKeys.all, 'pins'] as const,
  schedule: () => [...linkedinKeys.all, 'schedule'] as const,
  runs: () => [...linkedinKeys.all, 'runs'] as const,
  runList: (params: RunsParams) => [...linkedinKeys.runs(), 'list', params] as const,
  run: (runId: number) => [...linkedinKeys.runs(), 'detail', runId] as const,
}

// --- reads -------------------------------------------------------------------------

export const statusQuery = queryOptions({
  queryKey: linkedinKeys.status(),
  queryFn: async ({ signal }): Promise<LinkedInStatus> => {
    const { data, error, response } = await api.GET('/api/v1/linkedin/status', { signal })
    if (data === undefined) throw apiError(error, response, 'GET /api/v1/linkedin/status')
    return data
  },
})

export const browserQuery = queryOptions({
  queryKey: linkedinKeys.browser(),
  queryFn: async ({ signal }): Promise<BrowserLaunch> => {
    const { data, error, response } = await api.GET('/api/v1/linkedin/browser', { signal })
    if (data === undefined) throw apiError(error, response, 'GET /api/v1/linkedin/browser')
    return data
  },
  // Config-derived and unchanging for the life of the process; no need to refetch it often.
  staleTime: 60_000,
})

export const budgetQuery = queryOptions({
  queryKey: linkedinKeys.budget(),
  queryFn: async ({ signal }): Promise<BudgetStatus> => {
    const { data, error, response } = await api.GET('/api/v1/linkedin/budget', { signal })
    if (data === undefined) throw apiError(error, response, 'GET /api/v1/linkedin/budget')
    return data
  },
})

export const heatQuery = queryOptions({
  queryKey: linkedinKeys.heat(),
  queryFn: async ({ signal }): Promise<Heat> => {
    const { data, error, response } = await api.GET('/api/v1/linkedin/heat', { signal })
    if (data === undefined) throw apiError(error, response, 'GET /api/v1/linkedin/heat')
    return data
  },
})

export const pinsQuery = queryOptions({
  queryKey: linkedinKeys.pins(),
  queryFn: async ({ signal }): Promise<Pin[]> => {
    const { data, error, response } = await api.GET('/api/v1/linkedin/pins', { signal })
    if (data === undefined) throw apiError(error, response, 'GET /api/v1/linkedin/pins')
    return data
  },
})

export const scheduleQuery = queryOptions({
  queryKey: linkedinKeys.schedule(),
  queryFn: async ({ signal }): Promise<Schedule> => {
    const { data, error, response } = await api.GET('/api/v1/linkedin/schedule', { signal })
    if (data === undefined) throw apiError(error, response, 'GET /api/v1/linkedin/schedule')
    return data
  },
})

export interface RunsParams {
  kind?: RunKind
  status?: RunStatus
  limit: number
  offset: number
}

export function runsQuery(params: RunsParams) {
  return queryOptions({
    queryKey: linkedinKeys.runList(params),
    queryFn: async ({ signal }): Promise<RunPage> => {
      const { data, error, response } = await api.GET('/api/v1/linkedin/runs', {
        params: { query: params },
        signal,
      })
      if (data === undefined) throw apiError(error, response, 'GET /api/v1/linkedin/runs')
      return data
    },
    placeholderData: (previous) => previous,
  })
}

export function runQuery(runId: number) {
  return queryOptions({
    queryKey: linkedinKeys.run(runId),
    queryFn: async ({ signal }): Promise<RunType> => {
      const { data, error, response } = await api.GET('/api/v1/linkedin/runs/{run_id}', {
        params: { path: { run_id: runId } },
        signal,
      })
      if (data === undefined) throw apiError(error, response, `GET /api/v1/linkedin/runs/${runId}`)
      return data
    },
  })
}

// --- runs: start, cancel, resume ----------------------------------------------------

/** Start a run by hand. `max_visits` (enrichment only) only ever lowers today's budget. */
export async function startRun(kind: RunKind, maxVisits: number | null): Promise<RunAccepted> {
  const { data, error, response } = await api.POST('/api/v1/linkedin/runs', {
    body: { kind, max_visits: maxVisits },
  })
  if (data === undefined) throw apiError(error, response, 'POST /api/v1/linkedin/runs')
  return data
}

/** Ask a running run to stop at its next check (spec 9.9): cooperative, not instant. */
export async function cancelRun(runId: number): Promise<RunType> {
  const { data, error, response } = await api.POST('/api/v1/linkedin/runs/{run_id}/cancel', {
    params: { path: { run_id: runId } },
  })
  if (data === undefined) {
    throw apiError(error, response, `POST /api/v1/linkedin/runs/${runId}/cancel`)
  }
  return data
}

/** Resume an aborted enrichment run's remaining plan, as a new run. Resumable once. */
export async function resumeRun(runId: number, maxVisits: number | null): Promise<RunAccepted> {
  const { data, error, response } = await api.POST('/api/v1/linkedin/runs/{run_id}/resume', {
    params: { path: { run_id: runId } },
    body: { max_visits: maxVisits },
  })
  if (data === undefined) {
    throw apiError(error, response, `POST /api/v1/linkedin/runs/${runId}/resume`)
  }
  return data
}

// --- pins ----------------------------------------------------------------------------

export async function pinContact(contactId: number): Promise<Pin[]> {
  const { data, error, response } = await api.POST('/api/v1/linkedin/pins', {
    body: { contact_id: contactId },
  })
  if (data === undefined) throw apiError(error, response, 'POST /api/v1/linkedin/pins')
  return data
}

export async function unpinContact(contactId: number): Promise<Pin[]> {
  const { data, error, response } = await api.DELETE('/api/v1/linkedin/pins/{contact_id}', {
    params: { path: { contact_id: contactId } },
  })
  if (data === undefined) {
    throw apiError(error, response, `DELETE /api/v1/linkedin/pins/${contactId}`)
  }
  return data
}

// --- scheduled runs: armed or not ----------------------------------------------------

/** Arm scheduled runs. Needs `confirm: true` — a person's own act, never automatic. */
export async function armSchedule(): Promise<Schedule> {
  const { data, error, response } = await api.POST('/api/v1/linkedin/schedule/arm', {
    body: { confirm: true },
  })
  if (data === undefined) throw apiError(error, response, 'POST /api/v1/linkedin/schedule/arm')
  return data
}

export async function disarmSchedule(): Promise<Schedule> {
  const { data, error, response } = await api.POST('/api/v1/linkedin/schedule/disarm')
  if (data === undefined) {
    throw apiError(error, response, 'POST /api/v1/linkedin/schedule/disarm')
  }
  return data
}
