import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'

import type {
  ArchiveImportResult,
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
  /**
   * A machine-readable refusal code, when the backend sends one.
   *
   * `POST /imports/archive` puts an `ArchiveRefusalCode` on every 422 it
   * answers (#124), which is what its error guidance keys off
   * (`archive-flow.tsx`): a reworded message can no longer point a person at
   * the wrong next step. The other import routes still answer with a bare
   * `detail` string, so this is `null` for them.
   */
  readonly code: string | null

  constructor(message: string, status: number, code: string | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

/**
 * The backend's own explanation of a failure, or a plain status line.
 *
 * The import routes answer 404, 409, and 422 with FastAPI's `{"detail": ...}`,
 * which the OpenAPI export declares as an empty body, so the shape is checked
 * here at runtime rather than trusted from the generated types. An archive
 * refusal is `{"detail": "...", "code": "..."}`; `code` is also read from a
 * nested `{"detail": {"message": "...", "code": "..."}}`, which is how a route
 * that cannot give up its plain-string `detail` would have to carry one.
 */
export function apiError(error: unknown, response: Response, what: string): ApiError {
  const body = error as { detail?: unknown; code?: unknown } | null | undefined
  const nestedDetail = body?.detail as { message?: unknown; code?: unknown } | undefined
  const code =
    typeof body?.code === 'string'
      ? body.code
      : typeof nestedDetail?.code === 'string'
        ? nestedDetail.code
        : null
  const detail = body?.detail
  if (typeof detail === 'string' && detail !== '') {
    return new ApiError(detail, response.status, code)
  }
  if (typeof nestedDetail?.message === 'string' && nestedDetail.message !== '') {
    return new ApiError(nestedDetail.message, response.status, code)
  }
  if (Array.isArray(detail)) {
    const first: unknown = detail[0]
    const message = (first as { msg?: unknown } | undefined)?.msg
    if (typeof message === 'string' && message !== '') {
      return new ApiError(message, response.status, code)
    }
  }
  return new ApiError(`${what} returned ${response.status}`, response.status, code)
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
/**
 * Undo a committed run. `force` deletes the contacts it created even though
 * they have gained things since (a 409 with `created_contacts_changed`, #78);
 * it never overrides a merge or a later run that wrote over this one.
 */
export async function rollbackRun(runId: number, force = false): Promise<RollbackResult> {
  const { data, error, response } = await api.POST('/api/v1/imports/{run_id}/rollback', {
    params: { path: { run_id: runId }, query: force ? { force: true } : {} },
  })
  if (data === undefined) throw apiError(error, response, `POST /api/v1/imports/${runId}/rollback`)
  return data
}

/**
 * Upload the LinkedIn export zip, or one of its CSVs on its own, straight through.
 *
 * `POST /imports/archive` (P1-20) is the one multipart route in the API: no
 * mapping, preview, or candidate step, because that pipeline has none. It takes
 * the file as `multipart/form-data` rather than the JSON-with-text-content body
 * the rest of this module uses, so it is sent as `FormData` rather than through
 * `api.POST`'s usual JSON body. openapi-typescript renders a multipart field as
 * a plain `string` in the generated types (there is no narrower type for
 * "binary upload"), and openapi-fetch's default body serializer already passes
 * a `FormData` instance through unchanged and lets the browser set the
 * multipart boundary — so the cast below only papers over the generated type,
 * it does not change what is actually sent.
 */
export async function importArchive(file: File): Promise<ArchiveImportResult> {
  const form = new FormData()
  // No explicit filename argument: `file` is already a `File`, whose own
  // `.name` is what `FormData.append(name, aFile)` puts in the multipart
  // part on its own. Passing a third argument here — even the same string —
  // makes `FormData.append` construct a *new* File object per its own spec,
  // which is otherwise harmless but broke identity-based lookups in the test
  // harness (`fileContents` in `@/test/fetch`) for no behavioral gain.
  form.append('file', file)
  const { data, error, response } = await api.POST('/api/v1/imports/archive', {
    body: form as unknown as { file: string },
  })
  if (data === undefined) throw apiError(error, response, 'POST /api/v1/imports/archive')
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
