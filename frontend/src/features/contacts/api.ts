/**
 * Every call the Contacts table and detail page make, through the generated
 * client (`openapi-fetch` over `schema.d.ts`). No hand-written URL or response
 * type lives outside this module.
 */

import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import { detailMessage } from '@/api/errors'

import type { ColumnId } from './columns'
import { requestedColumns } from './columns'
import type {
  BulkAction,
  BulkCountOut,
  BulkSelection,
  ContactCreate,
  ContactDetail,
  ContactMet,
  ContactPage,
  ContactPatch,
  ContactTagOut,
  ContactList,
  DuplicateContact,
  FilterTree,
  ProvenanceField,
  SavedView,
  SortKey,
  TagOut,
  TimelineEntry,
} from './types'

/** A request the backend refused, carrying the status and the parsed body. */
export class ApiFailure extends Error {
  readonly status: number
  readonly body: unknown

  constructor(status: number, body: unknown, message: string) {
    super(message)
    this.name = 'ApiFailure'
    this.status = status
    this.body = body
  }
}

function fail(what: string, status: number, body: unknown): never {
  throw new ApiFailure(status, body, `${what}: ${detailMessage(body) ?? status}`)
}

// --- the table --------------------------------------------------------------

export const contactsKeys = {
  all: ['contacts'] as const,
  pages: () => [...contactsKeys.all, 'page'] as const,
  detail: (id: number) => [...contactsKeys.all, 'detail', id] as const,
  timeline: (id: number) => [...contactsKeys.all, 'timeline', id] as const,
  tags: (id: number) => [...contactsKeys.all, 'tags', id] as const,
}

export interface ContactsPageRequest {
  filter: FilterTree
  sort: readonly SortKey[]
  limit: number
  offset: number
}

/** One page of the table: a filter, a sort, a page, and the columns on screen. */
export function contactsPageQuery(request: ContactsPageRequest, columns: readonly ColumnId[]) {
  const body = {
    filter: request.filter,
    sort: [...request.sort],
    limit: request.limit,
    offset: request.offset,
    columns: requestedColumns(columns),
  }
  return queryOptions({
    queryKey: [...contactsKeys.pages(), body] as const,
    queryFn: async ({ signal }): Promise<ContactPage> => {
      const { data, error, response } = await api.POST('/api/v1/contacts/query', { body, signal })
      if (data === undefined) fail('contacts query', response.status, error)
      return data
    },
    // A page a filter keystroke just left is still the right answer for the
    // keystroke before it; keeping it stops the table flashing empty.
    placeholderData: (previous) => previous,
    retry: false,
  })
}

export function contactQuery(contactId: number) {
  return queryOptions({
    queryKey: contactsKeys.detail(contactId),
    queryFn: async ({ signal }): Promise<ContactDetail> => {
      const { data, error, response } = await api.GET('/api/v1/contacts/{contact_id}', {
        params: { path: { contact_id: contactId } },
        signal,
      })
      if (data === undefined) fail('contact', response.status, error)
      return data
    },
    retry: false,
  })
}

export interface TimelinePage {
  items: TimelineEntry[]
  next_before: string | null
}

/**
 * One page of the timeline, newest first. `before` is the cursor the previous
 * page handed back; `null` starts at the newest entry.
 */
export async function fetchTimelinePage(
  contactId: number,
  before: string | null,
  signal?: AbortSignal,
): Promise<TimelinePage> {
  const { data, error, response } = await api.GET('/api/v1/contacts/{contact_id}/timeline', {
    params: { path: { contact_id: contactId }, query: before ? { before } : {} },
    signal,
  })
  if (data === undefined) fail('timeline', response.status, error)
  return data
}

/** The tags on one contact. A table row carries none, so this is per contact. */
export function contactTagsQuery(contactId: number) {
  return queryOptions({
    queryKey: contactsKeys.tags(contactId),
    queryFn: async ({ signal }): Promise<ContactTagOut[]> => {
      const { data, error, response } = await api.GET('/api/v1/contacts/{contact_id}/tags', {
        params: { path: { contact_id: contactId } },
        signal,
      })
      if (data === undefined) fail('contact tags', response.status, error)
      return data
    },
    retry: false,
  })
}

/**
 * Lists (spec 10.4, P1-08). Both kinds come back; only a static list takes
 * members, because a smart list's membership is its filter.
 */
export const listsQuery = queryOptions({
  queryKey: ['lists'] as const,
  queryFn: async ({ signal }): Promise<ContactList[]> => {
    const { data, error, response } = await api.GET('/api/v1/lists', { signal })
    if (data === undefined) fail('lists', response.status, error)
    return data
  },
  retry: false,
})

/** Puts contacts on a static list. Ids already on it are not counted again. */
export async function addListMembers(listId: number, contactIds: number[]): Promise<number> {
  const { data, error, response } = await api.POST('/api/v1/lists/{list_id}/members', {
    params: { path: { list_id: listId } },
    body: { contact_ids: contactIds },
  })
  if (data === undefined) fail('add to list', response.status, error)
  return data.added
}

/** Saved views: a column set, a sort, and a filter under a name (spec 10.1, P1-08). */
export const viewsQuery = queryOptions({
  queryKey: ['views'] as const,
  queryFn: async ({ signal }): Promise<SavedView[]> => {
    const { data, error, response } = await api.GET('/api/v1/views', { signal })
    if (data === undefined) fail('views', response.status, error)
    return data
  },
  retry: false,
})

export async function createView(view: {
  name: string
  columns: string[]
  sort: SortKey[]
  filter: FilterTree | null
}): Promise<SavedView> {
  const { data, error, response } = await api.POST('/api/v1/views', { body: view })
  if (data === undefined) fail('save view', response.status, error)
  return data
}

export async function deleteView(viewId: number): Promise<void> {
  const { error, response } = await api.DELETE('/api/v1/views/{view_id}', {
    params: { path: { view_id: viewId } },
  })
  if (!response.ok) fail('delete view', response.status, error)
}

export const tagsQuery = queryOptions({
  queryKey: ['tags'] as const,
  queryFn: async ({ signal }): Promise<TagOut[]> => {
    const { data, error, response } = await api.GET('/api/v1/tags', { signal })
    if (data === undefined) fail('tags', response.status, error)
    return data
  },
  staleTime: 60_000,
  retry: false,
})

// --- adding a contact by hand (#303) ----------------------------------------

/** What adding a contact came to: the new contact, or the one already there. */
export type CreateContactResult =
  { kind: 'created'; contact: ContactDetail } | { kind: 'duplicate'; duplicate: DuplicateContact }

function isDuplicate(body: unknown): body is DuplicateContact {
  return (
    body !== null &&
    typeof body === 'object' &&
    'detail' in body &&
    body.detail === 'duplicate' &&
    'contact_id' in body &&
    typeof body.contact_id === 'number'
  )
}

/**
 * Adds one contact, with the dedup and checks an import runs. Someone the email
 * or the LinkedIn URL already finds (or the name and company, unless
 * `allow_name_match`) comes back as `duplicate`, naming who is already there;
 * any other refusal throws an {@link ApiFailure} whose body {@link fieldErrors} reads.
 */
export async function createContact(body: ContactCreate): Promise<CreateContactResult> {
  const { data, error, response } = await api.POST('/api/v1/contacts', { body })
  if (data !== undefined) return { kind: 'created', contact: data }
  if (response.status === 409 && isDuplicate(error)) return { kind: 'duplicate', duplicate: error }
  fail('add contact', response.status, error)
}

/**
 * A `422`'s problems by the body field they name (`loc: ["body", field]`), the
 * shape both a schema refusal and the service's own checks answer in. Problems
 * that name no field are left out; the failure's message still carries them.
 */
export function fieldErrors(error: unknown): Record<string, string> {
  if (!(error instanceof ApiFailure) || error.status !== 422) return {}
  const { body } = error
  if (body === null || typeof body !== 'object' || !('detail' in body)) return {}
  const { detail } = body
  if (!Array.isArray(detail)) return {}
  const found: Record<string, string> = {}
  for (const item of detail as unknown[]) {
    if (item === null || typeof item !== 'object') continue
    const loc = 'loc' in item ? item.loc : undefined
    const msg = 'msg' in item ? item.msg : undefined
    if (!Array.isArray(loc) || typeof msg !== 'string') continue
    const field: unknown = loc[1]
    if (loc[0] === 'body' && typeof field === 'string' && !(field in found)) {
      found[field] = msg.replace(/^Value error, /, '')
    }
  }
  return found
}

// --- writes on one contact --------------------------------------------------

export async function patchContact(contactId: number, patch: ContactPatch): Promise<ContactDetail> {
  const { data, error, response } = await api.PATCH('/api/v1/contacts/{contact_id}', {
    params: { path: { contact_id: contactId } },
    body: patch,
  })
  if (data === undefined) fail('save', response.status, error)
  return data
}

export async function revertContactField(
  contactId: number,
  field: ProvenanceField,
): Promise<ContactDetail> {
  const { data, error, response } = await api.POST('/api/v1/contacts/{contact_id}/revert-field', {
    params: { path: { contact_id: contactId } },
    body: { field },
  })
  if (data === undefined) fail('revert', response.status, error)
  return data
}

export async function setArchived(contactId: number, archived: boolean): Promise<ContactDetail> {
  const path = archived
    ? ('/api/v1/contacts/{contact_id}/archive' as const)
    : ('/api/v1/contacts/{contact_id}/unarchive' as const)
  const { data, error, response } = await api.POST(path, {
    params: { path: { contact_id: contactId } },
  })
  if (data === undefined) fail(archived ? 'archive' : 'unarchive', response.status, error)
  return data
}

/**
 * Confirm a contact read off a connections-page card, or reject it (#184).
 *
 * Confirm clears the needs-review mark; reject archives the contact and keeps
 * the mark, so unarchiving brings back an unconfirmed contact.
 */
export async function reviewContact(
  contactId: number,
  verdict: 'confirm' | 'reject',
): Promise<ContactDetail> {
  const path =
    verdict === 'confirm'
      ? ('/api/v1/contacts/{contact_id}/confirm' as const)
      : ('/api/v1/contacts/{contact_id}/reject' as const)
  const { data, error, response } = await api.POST(path, {
    params: { path: { contact_id: contactId } },
  })
  if (data === undefined) fail(verdict, response.status, error)
  return data
}

export async function tagContact(contactId: number, tagId: number): Promise<void> {
  const { error, response } = await api.POST('/api/v1/contacts/{contact_id}/tags', {
    params: { path: { contact_id: contactId } },
    body: { tag_id: tagId },
  })
  // The contact already carrying the tag is the state asked for, not a failure.
  if (!response.ok && response.status !== 409) fail('tag', response.status, error)
}

export async function untagContact(contactId: number, tagId: number): Promise<void> {
  const { error, response } = await api.DELETE('/api/v1/contacts/{contact_id}/tags/{tag_id}', {
    params: { path: { contact_id: contactId, tag_id: tagId } },
  })
  if (!response.ok && response.status !== 404) fail('untag', response.status, error)
}

export async function setNotes(contactId: number, notes: string | null): Promise<void> {
  const { error, response } = await api.PUT('/api/v1/contacts/{contact_id}/notes', {
    params: { path: { contact_id: contactId } },
    body: { notes },
  })
  if (!response.ok) fail('notes', response.status, error)
}

export async function addEmail(contactId: number, email: string): Promise<void> {
  const { error, response } = await api.POST('/api/v1/contacts/{contact_id}/emails', {
    params: { path: { contact_id: contactId } },
    body: { email, kind: 'other', is_primary: false, status: 'ok' },
  })
  if (!response.ok) fail('add address', response.status, error)
}

export async function makeEmailPrimary(contactId: number, emailId: number): Promise<void> {
  const { error, response } = await api.PATCH('/api/v1/contacts/{contact_id}/emails/{email_id}', {
    params: { path: { contact_id: contactId, email_id: emailId } },
    body: { is_primary: true },
  })
  if (!response.ok) fail('set primary address', response.status, error)
}

export async function deleteEmail(contactId: number, emailId: number): Promise<void> {
  const { error, response } = await api.DELETE('/api/v1/contacts/{contact_id}/emails/{email_id}', {
    params: { path: { contact_id: contactId, email_id: emailId } },
  })
  if (!response.ok) fail('remove address', response.status, error)
}

export async function addPhone(contactId: number, raw: string): Promise<void> {
  const { error, response } = await api.POST('/api/v1/contacts/{contact_id}/phones', {
    params: { path: { contact_id: contactId } },
    body: { raw, kind: 'other', is_primary: false },
  })
  if (!response.ok) fail('add number', response.status, error)
}

export async function deletePhone(contactId: number, phoneId: number): Promise<void> {
  const { error, response } = await api.DELETE('/api/v1/contacts/{contact_id}/phones/{phone_id}', {
    params: { path: { contact_id: contactId, phone_id: phoneId } },
  })
  if (!response.ok) fail('remove number', response.status, error)
}

export async function addLink(contactId: number, url: string): Promise<void> {
  const { error, response } = await api.POST('/api/v1/contacts/{contact_id}/links', {
    params: { path: { contact_id: contactId } },
    body: { url, kind: 'other' },
  })
  if (!response.ok) fail('add link', response.status, error)
}

export async function deleteLink(contactId: number, linkId: number): Promise<void> {
  const { error, response } = await api.DELETE('/api/v1/contacts/{contact_id}/links/{link_id}', {
    params: { path: { contact_id: contactId, link_id: linkId } },
  })
  if (!response.ok) fail('remove link', response.status, error)
}

// --- bulk -------------------------------------------------------------------

/**
 * The most ids one bulk selection may name (`BulkSelection.ids`' `maxLength`).
 *
 * Picked rows survive paging, so a person could click past this; the table
 * stops them here instead of letting the server refuse the whole action. More
 * than this is what "select all matching this filter" is for.
 */
export const MAX_BULK_IDS = 1000

/**
 * Everything a confirmation token binds: the selection, the action, and exactly
 * what would be written.
 *
 * The count and the action send the same object, because the token is issued
 * against all of it: a token for "mark 214 as met" is refused for "mark 214 as
 * not met". So the dialog settles the whole sentence, reason included, before
 * it asks for a count.
 */
export interface BulkConfirmable {
  action: BulkAction
  selection: BulkSelection
  value?: ContactMet | boolean | null
  reason?: string | null
}

function bulkBody(confirmable: BulkConfirmable) {
  return {
    action: confirmable.action,
    selection: confirmable.selection,
    ...(confirmable.value === undefined ? {} : { value: confirmable.value }),
    ...(confirmable.reason === undefined ? {} : { reason: confirmable.reason }),
  }
}

/** Asks what an action would touch. The token that comes back binds that count. */
export async function countBulk(confirmable: BulkConfirmable): Promise<BulkCountOut> {
  const { data, error, response } = await api.POST('/api/v1/contacts/bulk/count', {
    body: bulkBody(confirmable),
  })
  if (data === undefined) fail('count', response.status, error)
  return data
}

export async function applyBulk(confirmable: BulkConfirmable, token: string): Promise<number> {
  const { data, error, response } = await api.POST('/api/v1/contacts/bulk', {
    body: { ...bulkBody(confirmable), token },
  })
  if (data === undefined) fail('bulk action', response.status, error)
  return data.affected
}

/**
 * The survivor a merged-away contact points at, or null when this is some other
 * failure.
 *
 * Every write to a contact that was merged away answers `409 {"detail":
 * "merged", "merged_into_id": N}` (spec 8.2), so the client can say where the
 * person went instead of showing a conflict nobody can act on.
 */
export function mergedInto(error: unknown): number | null {
  if (!(error instanceof ApiFailure) || error.status !== 409) return null
  const body = (error.body ?? {}) as Record<string, unknown>
  return body.detail === 'merged' && typeof body.merged_into_id === 'number'
    ? body.merged_into_id
    : null
}

/**
 * Why a bulk action was refused, in the terms the dialog explains it in.
 *
 * A moved count and an expired token are both the confirmation doing its job
 * (spec 14.1): nothing was changed, and asking for the count again is the way
 * forward. They read differently, so they are separate kinds.
 */
export type BulkRefusal =
  | { kind: 'count_mismatch'; expected: number; actual: number }
  | { kind: 'expired' }
  | { kind: 'rejected'; detail: string }
  | { kind: 'failed'; detail: string }

export function readBulkRefusal(error: unknown): BulkRefusal {
  if (!(error instanceof ApiFailure)) {
    return { kind: 'failed', detail: error instanceof Error ? error.message : 'Unknown error' }
  }
  const body = (error.body ?? {}) as Record<string, unknown>
  if (
    error.status === 409 &&
    body.detail === 'count mismatch' &&
    typeof body.expected_count === 'number' &&
    typeof body.actual_count === 'number'
  ) {
    return { kind: 'count_mismatch', expected: body.expected_count, actual: body.actual_count }
  }
  if (error.status === 409 && body.reason === 'expired') {
    return { kind: 'expired' }
  }
  if (error.status === 422 && typeof body.reason === 'string') {
    return { kind: 'rejected', detail: String(body.detail ?? body.reason) }
  }
  return { kind: 'failed', detail: error.message }
}

/**
 * A refusal in words, shared by every screen that confirms a bulk count.
 *
 * It sits beside `readBulkRefusal` rather than in one dialog, because the
 * Contacts table and the Lists page both run this flow and a refusal that read
 * differently in the two places would be two explanations of one mechanism.
 */
export function refusalMessage(refusal: BulkRefusal): string {
  switch (refusal.kind) {
    case 'count_mismatch':
      return (
        `The selection changed while the confirmation was open: it now matches ` +
        `${refusal.actual.toLocaleString()} contacts, not the ${refusal.expected.toLocaleString()} you confirmed.`
      )
    case 'expired':
      return 'This confirmation expired; a count is good for five minutes.'
    case 'rejected':
      return `The confirmation was refused: ${refusal.detail}.`
    case 'failed':
      return `The action could not be applied: ${refusal.detail}.`
  }
}
