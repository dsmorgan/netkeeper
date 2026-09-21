import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'

import type {
  ColumnMapping,
  Decision,
  ImportRun,
  ImportRowPage,
  ImportRunPage,
  Inspection,
  PresetList,
  PreviewRow,
  Resolution,
  RollbackResult,
} from './types'

/** A failed call, carrying the status so a caller can tell 409 from 422. */
export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * The backend's own explanation of a failure, or a plain status line.
 *
 * The import routes answer 404, 409, and 422 with FastAPI's `{"detail": ...}`,
 * which the OpenAPI export declares as an empty body, so the shape is checked
 * here at runtime rather than trusted from the generated types.
 */
export function apiError(error: unknown, response: Response, what: string): ApiError {
  const detail = (error as { detail?: unknown } | null | undefined)?.detail
  if (typeof detail === 'string' && detail !== '') {
    return new ApiError(detail, response.status)
  }
  if (Array.isArray(detail)) {
    const first: unknown = detail[0]
    const message = (first as { msg?: unknown } | undefined)?.msg
    if (typeof message === 'string' && message !== '') {
      return new ApiError(message, response.status)
    }
  }
  return new ApiError(`${what} returned ${response.status}`, response.status)
}

export const importKeys = {
  all: ['imports'] as const,
  presets: () => ['imports', 'presets'] as const,
  runs: (limit: number, offset: number) => ['imports', 'runs', { limit, offset }] as const,
  run: (runId: number) => ['imports', 'run', runId] as const,
  rows: (runId: number, resolution: Resolution | null, limit: number, offset: number) =>
    ['imports', 'run', runId, 'rows', { resolution, limit, offset }] as const,
}

/** Built-in presets and the user's saved ones (spec 10.5 step 2). */
export const presetsQuery = queryOptions({
  queryKey: importKeys.presets(),
  queryFn: async ({ signal }): Promise<PresetList> => {
    const { data, error, response } = await api.GET('/api/v1/imports/presets', { signal })
    if (data === undefined) throw apiError(error, response, 'GET /api/v1/imports/presets')
    return data
  },
  staleTime: 60_000,
})

/** Import runs, newest first. */
export function runsQuery(limit: number, offset: number) {
  return queryOptions({
    queryKey: importKeys.runs(limit, offset),
    queryFn: async ({ signal }): Promise<ImportRunPage> => {
      const { data, error, response } = await api.GET('/api/v1/imports', {
        params: { query: { limit, offset } },
        signal,
      })
      if (data === undefined) throw apiError(error, response, 'GET /api/v1/imports')
      return data
    },
  })
}

export function runQuery(runId: number) {
  return queryOptions({
    queryKey: importKeys.run(runId),
    queryFn: async ({ signal }): Promise<ImportRun> => {
      const { data, error, response } = await api.GET('/api/v1/imports/{run_id}', {
        params: { path: { run_id: runId } },
        signal,
      })
      if (data === undefined) throw apiError(error, response, `GET /api/v1/imports/${runId}`)
      return data
    },
  })
}

/** A run's rows in file order, optionally only those with one resolution. */
export async function fetchRows(
  runId: number,
  { resolution = null, limit = 100, offset = 0 }: RowsOptions = {},
  signal?: AbortSignal,
): Promise<ImportRowPage> {
  const { data, error, response } = await api.GET('/api/v1/imports/{run_id}/rows', {
    params: {
      path: { run_id: runId },
      query: { limit, offset, ...(resolution === null ? {} : { resolution }) },
    },
    signal,
  })
  if (data === undefined) throw apiError(error, response, `GET /api/v1/imports/${runId}/rows`)
  return data
}

export function rowsQuery(runId: number, options: RowsOptions = {}) {
  const { resolution = null, limit = 100, offset = 0 } = options
  return queryOptions({
    queryKey: importKeys.rows(runId, resolution, limit, offset),
    queryFn: ({ signal }) => fetchRows(runId, { resolution, limit, offset }, signal),
  })
}

export interface RowsOptions {
  resolution?: Resolution | null
  limit?: number
  offset?: number
}

/** A file's columns and the mapping a preset gives them. Nothing is stored. */
export async function inspectFile(body: {
  content: string
  preset?: string | null
  mapping?: ColumnMapping | null
}): Promise<Inspection> {
  const { data, error, response } = await api.POST('/api/v1/imports/inspect', { body })
  if (data === undefined) throw apiError(error, response, 'POST /api/v1/imports/inspect')
  return data
}

/** Read a file into a draft run: one row per data row, counts for the whole file. */
export async function createRun(body: {
  filename: string
  content: string
  preset?: string | null
  mapping?: ColumnMapping | null
}): Promise<ImportRun> {
  const { data, error, response } = await api.POST('/api/v1/imports', {
    body: { ...body, source_kind: 'csv' },
  })
  if (data === undefined) throw apiError(error, response, 'POST /api/v1/imports')
  return data
}

/** Rows the preview resolves; the API's own default and ceiling for the screen (spec 10.5). */
export const PREVIEW_ROWS = 20

/**
 * Resolve the first rows against the database as it is now, writing nothing.
 *
 * A POST that reads: the service resolves inside a savepoint it rolls back, so
 * it needs a writer session. It is modelled as a query all the same, because
 * that is what it is to the screen.
 */
export async function previewRun(
  runId: number,
  limit: number,
  signal?: AbortSignal,
): Promise<PreviewRow[]> {
  const { data, error, response } = await api.POST('/api/v1/imports/{run_id}/preview', {
    params: { path: { run_id: runId }, query: { limit } },
    signal,
  })
  if (data === undefined) throw apiError(error, response, `POST /api/v1/imports/${runId}/preview`)
  return data
}

export function previewQuery(runId: number, limit: number = PREVIEW_ROWS) {
  return queryOptions({
    queryKey: ['imports', 'run', runId, 'preview', limit] as const,
    queryFn: ({ signal }) => previewRun(runId, limit, signal),
    refetchOnWindowFocus: false,
  })
}

/**
 * Apply a draft run in one transaction.
 *
 * Answers 409 while a candidate row has no decision; the wizard keeps the
 * button out of reach until every candidate is decided or `skipUndecided` is
 * chosen deliberately, so that status is a bug here rather than a flow.
 */
export async function commitRun(
  runId: number,
  decisions: Decision[],
  skipUndecided: boolean,
): Promise<ImportRun> {
  const { data, error, response } = await api.POST('/api/v1/imports/{run_id}/commit', {
    params: { path: { run_id: runId } },
    body: { decisions, skip_undecided: skipUndecided },
  })
  if (data === undefined) throw apiError(error, response, `POST /api/v1/imports/${runId}/commit`)
  return data
}

/** Undo a committed run, and only what that run did. */
export async function rollbackRun(runId: number): Promise<RollbackResult> {
  const { data, error, response } = await api.POST('/api/v1/imports/{run_id}/rollback', {
    params: { path: { run_id: runId } },
  })
  if (data === undefined) throw apiError(error, response, `POST /api/v1/imports/${runId}/rollback`)
  return data
}

/** Save a column mapping under a name, replacing any earlier preset by that name. */
export async function savePreset(name: string, mapping: Record<string, string>) {
  const { data, error, response } = await api.PUT('/api/v1/imports/presets/{name}', {
    params: { path: { name } },
    body: { mapping },
  })
  if (data === undefined) throw apiError(error, response, `PUT /api/v1/imports/presets/${name}`)
  return data
}
