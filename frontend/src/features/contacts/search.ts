/**
 * The Contacts table's state lives in the URL (spec 10.1, 14.3).
 *
 * Every knob that changes what the server returns — the filter, the sort, the
 * page — is a search parameter, so a view is a link somebody can paste, the
 * back button walks the filters, and a reload lands on the same page. Only the
 * column picker stays out: which columns you like is yours, not the view's, and
 * it is kept in local storage and in saved views instead.
 *
 * Parameters at their default are left out of the URL, so `/contacts` stays
 * `/contacts` until you do something to it.
 */

import type { ContactMet, FilterNode, FilterTree, FilterTreeOut, SortField, SortKey } from './types'
import { MET_VALUES } from './types'

export const DEFAULT_PAGE_SIZE = 50
/** The API's ceiling on `limit` (`ContactQuery.limit`); the picker offers no more. */
export const MAX_PAGE_SIZE = 200
export const PAGE_SIZES = [25, 50, 100, 200] as const
/**
 * The highest page a URL may ask for.
 *
 * Far past any real network, and low enough that `(page - 1) * MAX_PAGE_SIZE`
 * stays an exact integer and inside the API's own ceiling on `offset`
 * (`MAX_QUERY_OFFSET`, 1,000,000,000), so a hand-edited `?page=` can never ask
 * for an offset the server refuses. The table also clamps a page past the last
 * one to the last one, once it knows the total.
 */
export const MAX_PAGE = 1_000_000

export interface ContactsSearch {
  /** Free text across the name, company, title, headline, and location columns. */
  q?: string
  /** Substring of the current company. */
  company?: string
  met?: ContactMet
  /** Tag names; a contact carrying any of them matches. */
  tags?: string[]
  /** Only contacts marked do-not-contact. */
  dnc?: boolean
  /** Include archived contacts, which the table hides by default. */
  archived?: boolean
  /** `field:direction`, comma separated, in precedence order. */
  sort?: string
  /** One-based. */
  page?: number
  size?: number
  /** The saved view this search came from, by id (spec 10.1, P1-08). */
  view?: number
}

/** The string fields `contains` accepts, and what free text searches across. */
const SEARCH_FIELDS = [
  'first_name',
  'last_name',
  'preferred_name',
  'current_company',
  'current_title',
  'headline',
  'location',
] as const

const SORT_FIELDS = new Set<string>([
  'first_name',
  'last_name',
  'preferred_name',
  'headline',
  'current_title',
  'current_company',
  'location',
  'li_public_id',
  'met',
  'source',
  'degree',
  'connected_on',
  'last_contacted_at',
  'last_enriched_at',
  'triaged_at',
  'li_disconnected_at',
  'archived_at',
  'created_at',
  'updated_at',
  'do_not_contact',
])

function text(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() !== '' ? value : undefined
}

function flag(value: unknown): true | undefined {
  return value === true || value === 'true' ? true : undefined
}

function positive(value: unknown, fallback: number, max = Number.MAX_SAFE_INTEGER): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed) || parsed < 1) return fallback
  return Math.min(Math.floor(parsed), max)
}

function tagList(value: unknown): string[] | undefined {
  const raw = Array.isArray(value) ? value : typeof value === 'string' ? value.split(',') : []
  const names = raw.map((name) => String(name).trim()).filter((name) => name !== '')
  return names.length > 0 ? names : undefined
}

/**
 * Reads a URL's search parameters into {@link ContactsSearch}.
 *
 * Anything unreadable falls back to the default rather than throwing: a
 * hand-edited or truncated link should still show the table.
 */
export function validateContactsSearch(input: Record<string, unknown>): ContactsSearch {
  const met = MET_VALUES.find((value) => value === input.met)
  const page = input.page === undefined ? undefined : positive(input.page, 1, MAX_PAGE)
  const size =
    input.size === undefined ? undefined : positive(input.size, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
  const sort = parseSort(text(input.sort))
  return {
    ...(text(input.q) ? { q: text(input.q) } : {}),
    ...(text(input.company) ? { company: text(input.company) } : {}),
    ...(met ? { met } : {}),
    ...(tagList(input.tags) ? { tags: tagList(input.tags) } : {}),
    ...(flag(input.dnc) ? { dnc: true as const } : {}),
    ...(flag(input.archived) ? { archived: true as const } : {}),
    ...(sort.length > 0 ? { sort: formatSort(sort) } : {}),
    ...(page !== undefined && page !== 1 ? { page } : {}),
    ...(size !== undefined && size !== DEFAULT_PAGE_SIZE ? { size } : {}),
    ...(Number.isFinite(Number(input.view)) && Number(input.view) > 0
      ? { view: Number(input.view) }
      : {}),
  }
}

/** `"last_name:asc,connected_on:desc"` → sort keys, skipping any the API cannot sort on. */
export function parseSort(sort: string | undefined): SortKey[] {
  if (!sort) return []
  const keys: SortKey[] = []
  for (const term of sort.split(',')) {
    const [field, direction] = term.split(':')
    if (field === undefined || !SORT_FIELDS.has(field)) continue
    keys.push({ field: field as SortField, direction: direction === 'desc' ? 'desc' : 'asc' })
  }
  return keys
}

export function formatSort(keys: readonly SortKey[]): string {
  return keys.map((key) => `${key.field}:${key.direction}`).join(',')
}

/** The sort the table falls back to, so paging is stable when nothing is chosen. */
export const DEFAULT_SORT: SortKey[] = [
  { field: 'last_name', direction: 'asc' },
  { field: 'first_name', direction: 'asc' },
]

export function sortKeys(search: ContactsSearch): SortKey[] {
  const keys = parseSort(search.sort)
  return keys.length > 0 ? keys : DEFAULT_SORT
}

/** The filter tree the search parameters mean (spec 10.4). */
export function buildFilter(search: ContactsSearch): FilterTree {
  const children: FilterNode[] = []
  if (search.q) {
    children.push({
      op: 'or',
      children: SEARCH_FIELDS.map((field) => ({
        op: 'contains',
        field,
        value: search.q as string,
      })),
    })
  }
  if (search.company) {
    children.push({ op: 'contains', field: 'current_company', value: search.company })
  }
  if (search.met) {
    children.push({ op: 'eq', field: 'met', value: search.met })
  }
  if (search.dnc) {
    children.push({ op: 'eq', field: 'do_not_contact', value: true })
  }
  if (search.tags && search.tags.length > 0) {
    children.push({ op: 'tag_any', names: search.tags })
  }
  const where: FilterNode | null =
    children.length === 0
      ? null
      : children.length === 1
        ? (children[0] as FilterNode)
        : { op: 'and', children }
  return { include_archived: search.archived === true, where }
}

/**
 * The search parameters a stored filter and sort mean, as far as the filter bar
 * can say them.
 *
 * A view this UI saved reads back exactly, because {@link buildFilter} is what
 * wrote it. A filter from somewhere else (a future filter builder) may carry
 * predicates with no control here; those are left out of the bar, and the
 * table goes on using the view's own tree until a filter is touched. Compare
 * with {@link buildFilter} to know which case you are in.
 */
export function searchFromFilter(
  filter: FilterTree | FilterTreeOut | null | undefined,
  sort: readonly SortKey[],
): ContactsSearch {
  const search: ContactsSearch = {}
  if (sort.length > 0) search.sort = formatSort(sort)
  if (!filter) return search
  if (filter.include_archived) search.archived = true

  const where = filter.where as FilterNode | null | undefined
  const nodes: FilterNode[] = where == null ? [] : where.op === 'and' ? where.children : [where]
  for (const node of nodes) {
    if (node.op === 'or' && node.children.every((child: FilterNode) => child.op === 'contains')) {
      const values = new Set(
        node.children.map((child: FilterNode) => (child as { value: string }).value),
      )
      const only = [...values][0]
      if (values.size === 1 && only !== undefined) search.q = only
    } else if (node.op === 'contains' && node.field === 'current_company') {
      search.company = node.value
    } else if (node.op === 'eq' && node.field === 'met' && typeof node.value === 'string') {
      const met = MET_VALUES.find((value) => value === node.value)
      if (met) search.met = met
    } else if (node.op === 'eq' && node.field === 'do_not_contact' && node.value === true) {
      search.dnc = true
    } else if (node.op === 'tag_any' && node.names.length > 0) {
      search.tags = [...node.names]
    }
  }
  return search
}

/** True when the search carries nothing but paging: the table is showing everybody. */
export function isUnfiltered(search: ContactsSearch): boolean {
  return (
    !search.q &&
    !search.company &&
    !search.met &&
    !search.dnc &&
    !search.archived &&
    !search.tags?.length &&
    search.view === undefined
  )
}

export function pageSize(search: ContactsSearch): number {
  return search.size ?? DEFAULT_PAGE_SIZE
}

export function pageOffset(search: ContactsSearch): number {
  return ((search.page ?? 1) - 1) * pageSize(search)
}

/** The last page `total` rows fill at `size` a page; page one when there are none. */
export function lastPage(total: number, size: number): number {
  return Math.max(1, Math.ceil(total / size))
}

/** A JSON reading of a value with object keys sorted, so key order never makes two trees differ. */
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value !== null && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, entry]) => entry !== undefined)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    return `{${entries.map(([key, entry]) => `${JSON.stringify(key)}:${canonical(entry)}`).join(',')}}`
  }
  return JSON.stringify(value)
}

/**
 * A filter tree with its incidental differences taken out: a missing `where` is
 * `null`, and the children of an `and` or `or` are put in one order, since
 * neither cares which comes first.
 */
function normalized(filter: FilterTree | FilterTreeOut | null | undefined): string {
  const node = (value: unknown): unknown => {
    if (value === null || typeof value !== 'object') return value
    const record = value as Record<string, unknown>
    if ((record.op === 'and' || record.op === 'or') && Array.isArray(record.children)) {
      const children = record.children.map(node).sort((a, b) => {
        const left = canonical(a)
        const right = canonical(b)
        return left < right ? -1 : left > right ? 1 : 0
      })
      return { ...record, children }
    }
    return record
  }
  return canonical({
    include_archived: filter?.include_archived ?? false,
    where: node(filter?.where ?? null),
  })
}

/**
 * Whether the filter bar can say everything a stored filter says.
 *
 * When it can, touching a filter control after applying the view only changes
 * what the control changes. When it cannot, the table is running the view's
 * own tree, and the first touch replaces that tree with the bar's reading of it
 * — a wider selection than the one on screen (#88), which the page warns about.
 */
export function filterBarCanShow(filter: FilterTree | FilterTreeOut | null | undefined): boolean {
  return normalized(buildFilter(searchFromFilter(filter, []))) === normalized(filter)
}
