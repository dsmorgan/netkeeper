/**
 * Every call the LinkedIn page makes, through the generated client
 * (`openapi-fetch` over `schema.d.ts`; P2-12, built on P2-10's `/linkedin`
 * resource). Nothing here hand-writes a URL shape or a response type.
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'

import type {
  BrowserHealth,
  BrowserLaunch,
  BudgetStatus,
  Heat,
  LinkedInStatus,
  Pin,
  RunAccepted,
  RunContacts,
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
  browserHealth: () => [...linkedinKeys.all, 'browser-health'] as const,
  budget: () => [...linkedinKeys.all, 'budget'] as const,
  heat: () => [...linkedinKeys.all, 'heat'] as const,
  pins: () => [...linkedinKeys.all, 'pins'] as const,
  schedule: () => [...linkedinKeys.all, 'schedule'] as const,
  runs: () => [...linkedinKeys.all, 'runs'] as const,
  runList: (params: RunsParams) => [...linkedinKeys.runs(), 'list', params] as const,
  run: (runId: number) => [...linkedinKeys.runs(), 'detail', runId] as const,
  runContacts: (runId: number) => [...linkedinKeys.runs(), 'contacts', runId] as const,
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

/**
 * What netkeeper already knows about Chrome and the session (#181): the last
 * preflight's or run's evidence, and the newest run that could not reach Chrome.
 * The server never probes the browser to answer this.
 */
export const browserHealthQuery = queryOptions({
  queryKey: linkedinKeys.browserHealth(),
  queryFn: async ({ signal }): Promise<BrowserHealth> => {
    const { data, error, response } = await api.GET('/api/v1/linkedin/browser/health', {
      signal,
    })
    if (data === undefined) throw apiError(error, response, 'GET /api/v1/linkedin/browser/health')
    return data
  },
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

/** The last few contacts a run touched, newest first, and what happened to each (#324). */
export function runContactsQuery(runId: number) {
  return queryOptions({
    queryKey: linkedinKeys.runContacts(runId),
    queryFn: async ({ signal }): Promise<RunContacts> => {
      const { data, error, response } = await api.GET('/api/v1/linkedin/runs/{run_id}/contacts', {
        params: { path: { run_id: runId } },
        signal,
      })
      if (data === undefined) {
        throw apiError(error, response, `GET /api/v1/linkedin/runs/${runId}/contacts`)
      }
      return data
    },
  })
}

// --- runs: start, cancel, pause, resume ----------------------------------------------------

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

/**
 * Ask a running enrichment to stop at its next check and keep its place (#324). It
 * ends `paused`, and `resumeRun` continues the rest of its plan. A sync cannot be paused.
 */
export async function pauseRun(runId: number): Promise<RunType> {
  const { data, error, response } = await api.POST('/api/v1/linkedin/runs/{run_id}/pause', {
    params: { path: { run_id: runId } },
  })
  if (data === undefined) {
    throw apiError(error, response, `POST /api/v1/linkedin/runs/${runId}/pause`)
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

/**
 * Hold scheduled runs without disarming (#324): no new one starts until unpaused, a
 * run already going is not stopped, and nothing skipped while paused is replayed.
 */
export async function pauseSchedule(): Promise<Schedule> {
  const { data, error, response } = await api.POST('/api/v1/linkedin/schedule/pause')
  if (data === undefined) throw apiError(error, response, 'POST /api/v1/linkedin/schedule/pause')
  return data
}

export async function unpauseSchedule(): Promise<Schedule> {
  const { data, error, response } = await api.POST('/api/v1/linkedin/schedule/unpause')
  if (data === undefined) {
    throw apiError(error, response, 'POST /api/v1/linkedin/schedule/unpause')
  }
  return data
}

// --- the session flag and heat: confirmed manual clears (#181) ---------------------------

/**
 * Clear the session flag: `netkeeper linkedin clear-flag`. Sends the flag the
 * person was shown; the server clears only that one and answers 409 if it is gone
 * or changed since.
 */
export async function clearSessionFlag(
  outcome: string,
  flaggedAt: string,
): Promise<LinkedInStatus> {
  const { data, error, response } = await api.POST('/api/v1/linkedin/session-flag/clear', {
    body: { confirm: true, outcome, flagged_at: flaggedAt },
  })
  if (data === undefined) {
    throw apiError(error, response, 'POST /api/v1/linkedin/session-flag/clear')
  }
  return data
}

/**
 * Clear heat by hand (spec 9.7). Sends when the person saw it last raised; the
 * server refuses with 409 if it was raised again since, or there is nothing to clear.
 */
export async function clearHeat(lastRaisedAt: string): Promise<Heat> {
  const { data, error, response } = await api.POST('/api/v1/linkedin/heat/clear', {
    body: { confirm: true, last_raised_at: lastRaisedAt },
  })
  if (data === undefined) throw apiError(error, response, 'POST /api/v1/linkedin/heat/clear')
  return data
}
