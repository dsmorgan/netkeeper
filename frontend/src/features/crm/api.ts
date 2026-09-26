/**
 * Every call this feature makes, through the generated client.
 *
 * `openapi-fetch` types each path against `src/api/schema.d.ts`, so a request
 * body or a response field that the backend renamed fails `tsc` here rather
 * than at runtime. Nothing in this feature calls `fetch` directly.
 *
 * Read queries carry no `staleTime`: a smart list's membership and a rule's
 * match count are computed fresh on the server for every request, and caching
 * them across an edit would show a count for a filter nobody is looking at any
 * more.
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import { detailMessage } from '@/api/errors'
import type { paths } from '@/api/schema'

import type {
  AutotagRuleOut,
  AutotagRulePreviewOut,
  ExportFormat,
  ExportPreset,
  FilterTree,
  ListKind,
  ListOut,
  RuleField,
  SavedViewOut,
  SortKey,
  TagOut,
} from './types'

/**
 * The message the server sent, or a plain fallback.
 *
 * The backend refuses a bad auto-tag pattern with a sentence written for the
 * person who typed it ("an unbounded repeat inside another one ... rewrite it
 * without the nesting"). Swallowing that and showing "request failed" would
 * turn a fixable mistake into a mystery, so this digs the sentence out of every
 * shape FastAPI can produce — including the validation-error list a schema
 * guard answers with, minus pydantic's own "Value error, " prefix.
 */
export function errorMessage(error: unknown, status: number, fallback: string): string {
  return detailMessage(error) ?? `${fallback} (HTTP ${status})`
}

export class ApiError extends Error {
  readonly status: number
  readonly body: unknown

  constructor(message: string, status: number, body: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

function fail(error: unknown, status: number, fallback: string): never {
  throw new ApiError(errorMessage(error, status, fallback), status, error)
}

// --- tags -------------------------------------------------------------------

export const tagsQuery = queryOptions({
  queryKey: ['tags'],
  queryFn: async ({ signal }): Promise<TagOut[]> => {
    const { data, error, response } = await api.GET('/api/v1/tags', { signal })
    if (data === undefined) fail(error, response.status, 'could not load the tags')
    return data
  },
})

export async function createTag(input: { name: string; color?: string | null }): Promise<TagOut> {
  const body = { kind: 'manual' as const, color: null, ...input }
  const { data, error, response } = await api.POST('/api/v1/tags', { body })
  if (data === undefined) fail(error, response.status, 'could not create the tag')
  return data
}

export async function updateTag(
  tagId: number,
  body: { name?: string | null; color?: string | null },
): Promise<TagOut> {
  const { data, error, response } = await api.PATCH('/api/v1/tags/{tag_id}', {
    params: { path: { tag_id: tagId } },
    body,
  })
  if (data === undefined) fail(error, response.status, 'could not save the tag')
  return data
}

export async function deleteTag(tagId: number): Promise<void> {
  const { error, response } = await api.DELETE('/api/v1/tags/{tag_id}', {
    params: { path: { tag_id: tagId } },
  })
  if (!response.ok) fail(error, response.status, 'could not delete the tag')
}

// --- auto-tag rules ---------------------------------------------------------

export const rulesQuery = queryOptions({
  queryKey: ['autotag-rules'],
  queryFn: async ({ signal }): Promise<AutotagRuleOut[]> => {
    const { data, error, response } = await api.GET('/api/v1/autotag-rules', { signal })
    if (data === undefined) fail(error, response.status, 'could not load the rules')
    return data
  },
})

export interface RuleInput {
  tag_id: number
  field: RuleField
  pattern: string
  enabled: boolean
}

export async function createRule(body: RuleInput): Promise<AutotagRuleOut> {
  const { data, error, response } = await api.POST('/api/v1/autotag-rules', { body })
  if (data === undefined) fail(error, response.status, 'could not create the rule')
  return data
}

export async function updateRule(
  ruleId: number,
  body: Partial<RuleInput>,
): Promise<AutotagRuleOut> {
  const { data, error, response } = await api.PATCH('/api/v1/autotag-rules/{rule_id}', {
    params: { path: { rule_id: ruleId } },
    body,
  })
  if (data === undefined) fail(error, response.status, 'could not save the rule')
  return data
}

/**
 * Puts the rules in this order. The service takes the listed ids first and the
 * rest after them, so the editor sends every id to say the whole order.
 */
export async function reorderRules(ruleIds: number[]): Promise<AutotagRuleOut[]> {
  const { data, error, response } = await api.POST('/api/v1/autotag-rules/reorder', {
    body: { rule_ids: ruleIds },
  })
  if (data === undefined) fail(error, response.status, 'could not reorder the rules')
  return data
}

export async function deleteRule(ruleId: number): Promise<void> {
  const { error, response } = await api.DELETE('/api/v1/autotag-rules/{rule_id}', {
    params: { path: { rule_id: ruleId } },
  })
  if (!response.ok) fail(error, response.status, 'could not delete the rule')
}

export async function runAllRules() {
  const { data, error, response } = await api.POST('/api/v1/autotag-rules/run', {})
  if (data === undefined) fail(error, response.status, 'could not run the rules')
  return data
}

/**
 * How many live contacts a pattern matches, and how many searches timed out.
 *
 * The preview is the rule editor's live count. It is also where an unsafe
 * pattern is refused, so its rejection carries the sentence to render.
 */
export async function previewRule(
  field: RuleField,
  pattern: string,
  signal?: AbortSignal,
): Promise<AutotagRulePreviewOut> {
  const { data, error, response } = await api.POST('/api/v1/autotag-rules/preview', {
    body: { field, pattern },
    signal,
  })
  if (data === undefined) fail(error, response.status, 'could not preview the pattern')
  return data
}

// --- lists ------------------------------------------------------------------

export const listsQuery = queryOptions({
  queryKey: ['lists'],
  queryFn: async ({ signal }): Promise<ListOut[]> => {
    const { data, error, response } = await api.GET('/api/v1/lists', { signal })
    if (data === undefined) fail(error, response.status, 'could not load the lists')
    return data
  },
  // The dashboard's "build a list" step reads `isError` to tell an unavailable
  // count apart from a real zero (P1-24 review #1): the app's client still
  // retries a 5xx or a network failure (`shouldRetryQuery`), and those retries
  // keep `isError` false — and `data` `undefined`, which the step reads as
  // zero — for several seconds after `/lists` starts failing.
  retry: false,
})

export async function createList(body: {
  name: string
  kind: ListKind
  filter?: FilterTree | null
}): Promise<ListOut> {
  const { data, error, response } = await api.POST('/api/v1/lists', { body })
  if (data === undefined) fail(error, response.status, 'could not create the list')
  return data
}

export async function updateList(
  listId: number,
  body: { name?: string | null; filter?: FilterTree | null },
): Promise<ListOut> {
  const { data, error, response } = await api.PATCH('/api/v1/lists/{list_id}', {
    params: { path: { list_id: listId } },
    body,
  })
  if (data === undefined) fail(error, response.status, 'could not save the list')
  return data
}

export async function deleteList(listId: number): Promise<void> {
  const { error, response } = await api.DELETE('/api/v1/lists/{list_id}', {
    params: { path: { list_id: listId } },
  })
  if (!response.ok) fail(error, response.status, 'could not delete the list')
}

/** One page of a list's members, as `/lists/{id}/members` returns it. */
export type MembersPage = NonNullable<
  paths['/api/v1/lists/{list_id}/members']['get']['responses'][200]['content']['application/json']
>

/**
 * One page of a list's members.
 *
 * A smart list's membership is computed from its filter on every request and is
 * never materialized (spec 10.4), so a page of it must not outlive the filter
 * that produced it. Two things keep that true, and it is worth being exact
 * about which does the work: the key sits under the `['lists']` prefix, and
 * every write in this feature invalidates that prefix, so saving a filter,
 * adding a member, or applying a bulk action all ask again. `gcTime: 0` then
 * makes sure an unmounted page is dropped rather than replayed on the way back.
 * Nothing here caches membership across an edit.
 */
export function membersQuery(listId: number, offset = 0, limit = 25) {
  return queryOptions({
    queryKey: ['lists', listId, 'members', offset, limit],
    queryFn: async ({ signal }): Promise<MembersPage> => {
      const { data, error, response } = await api.GET('/api/v1/lists/{list_id}/members', {
        params: { path: { list_id: listId }, query: { limit, offset } },
        signal,
      })
      if (data === undefined) fail(error, response.status, 'could not load the members')
      return data
    },
    gcTime: 0,
  })
}

export async function addMembers(listId: number, contactIds: number[]): Promise<number> {
  const { data, error, response } = await api.POST('/api/v1/lists/{list_id}/members', {
    params: { path: { list_id: listId } },
    body: { contact_ids: contactIds },
  })
  if (data === undefined) fail(error, response.status, 'could not add the contacts')
  return data.added
}

export async function removeMember(listId: number, contactId: number): Promise<void> {
  const { error, response } = await api.DELETE('/api/v1/lists/{list_id}/members/{contact_id}', {
    params: { path: { list_id: listId, contact_id: contactId } },
  })
  if (!response.ok) fail(error, response.status, 'could not remove the contact')
}

// --- saved views ------------------------------------------------------------

export const viewsQuery = queryOptions({
  queryKey: ['views'],
  queryFn: async ({ signal }): Promise<SavedViewOut[]> => {
    const { data, error, response } = await api.GET('/api/v1/views', { signal })
    if (data === undefined) fail(error, response.status, 'could not load the saved views')
    return data
  },
})

export async function createView(body: {
  name: string
  columns: string[]
  sort?: SortKey[]
  filter?: FilterTree | null
}): Promise<SavedViewOut> {
  const { data, error, response } = await api.POST('/api/v1/views', { body })
  if (data === undefined) fail(error, response.status, 'could not save the view')
  return data
}

/**
 * Replace any of a view's name, columns, sort, and filter.
 *
 * Every field the editor can change is spelled out here, `sort` included. The
 * endpoint leaves out what it is not sent, so a field missing from this type is
 * a field the editor silently discards — with no error, because the request
 * succeeds (#82 was the same shape).
 */
export async function updateView(
  viewId: number,
  body: {
    name?: string | null
    columns?: string[] | null
    sort?: SortKey[] | null
    filter?: FilterTree | null
  },
): Promise<SavedViewOut> {
  const { data, error, response } = await api.PATCH('/api/v1/views/{view_id}', {
    params: { path: { view_id: viewId } },
    body,
  })
  if (data === undefined) fail(error, response.status, 'could not save the view')
  return data
}

export async function deleteView(viewId: number): Promise<void> {
  const { error, response } = await api.DELETE('/api/v1/views/{view_id}', {
    params: { path: { view_id: viewId } },
  })
  if (!response.ok) fail(error, response.status, 'could not delete the view')
}

// --- counting a filter ------------------------------------------------------

export interface FilterCount {
  total: number
  /** The backend's own reading of the filter, for the builder's header. */
  describe: string
}

/**
 * How many contacts a filter selects right now, and how the server reads it.
 *
 * `POST /contacts/query` is the same compiler the table, lists, and exports
 * run, so this count is the one the rest of the app would show. Asking for one
 * row keeps the answer cheap; only `total` and `describe` are used.
 */
export async function countFilter(tree: FilterTree, signal?: AbortSignal): Promise<FilterCount> {
  const { data, error, response } = await api.POST('/api/v1/contacts/query', {
    body: { filter: tree, limit: 1, offset: 0 },
    signal,
  })
  if (data === undefined) fail(error, response.status, 'could not count the filter')
  return { total: data.total, describe: data.describe }
}

// --- bulk actions -----------------------------------------------------------
//
// The count-confirmation flow lives in `features/contacts/api.ts`, which the
// Contacts table already drives: `BulkConfirmable`, `countBulk`, `applyBulk`,
// and `readBulkRefusal`, which sorts a refusal into the four kinds the server
// distinguishes. A second copy here would be a second dialect of the same
// conversation, and the two would drift on the day one of them learned
// something. This module re-exports them so the rest of this feature has one
// import site, and nothing is redefined.

export {
  applyBulk,
  countBulk,
  readBulkRefusal,
  refusalMessage,
  type BulkConfirmable,
  type BulkRefusal,
} from '@/features/contacts/api'

// --- exports ----------------------------------------------------------------

export interface ExportRequest {
  preset: ExportPreset
  format: ExportFormat
  headerless: boolean
  /** Quote CSV cells a spreadsheet would run as formulas; the file no longer re-imports (#76). */
  spreadsheetSafe?: boolean
  filter: FilterTree | null
  sort?: SortKey[]
}

/**
 * The URL that downloads an export.
 *
 * An export streams a file, so it is a plain browser navigation rather than a
 * client call: the response never becomes JSON and a download needs the
 * `Content-Disposition` filename the server sets.
 */
export function exportUrl(request: ExportRequest): string {
  const params = new URLSearchParams({ preset: request.preset, format: request.format })
  if (request.headerless) params.set('headerless', 'true')
  if (request.spreadsheetSafe === true) params.set('spreadsheet_safe', 'true')
  if (request.filter !== null) params.set('filter', JSON.stringify(request.filter))
  if (request.sort !== undefined && request.sort.length > 0) {
    params.set('sort', JSON.stringify(request.sort))
  }
  return `/api/v1/exports?${params.toString()}`
}
