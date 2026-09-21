/**
 * The triage endpoints, through the generated client (spec 10.2, P1-09).
 *
 * Nothing here hand-writes a URL shape or a response type: the paths and the
 * bodies come from `@/api/schema`, so a schema change surfaces as a type error
 * rather than as a runtime surprise. The wrappers exist for one reason each:
 * to turn a non-2xx answer into a `TriageError` that carries the status, because
 * the screen has to tell a `409` ("this changed since you decided") apart from a
 * `404` ("nothing left to undo") and offer the person a different choice for
 * each.
 */

import { api } from '@/api/client'
import type { components } from '@/api/schema'

export type ContactMet = components['schemas']['ContactMet']
export type TriageCard = components['schemas']['TriageCardOut']
export type TriageContact = components['schemas']['TriageContactOut']
export type TriageEvidence = components['schemas']['TriageEvidenceOut']
export type TriageMessages = components['schemas']['TriageMessagesOut']
export type TriageProgress = components['schemas']['TriageProgressOut']
export type TriageQueue = components['schemas']['TriageQueueOut']
export type TriageDecisionResult = components['schemas']['TriageDecisionResult']
export type TriageUndoResult = components['schemas']['TriageUndoOut']
export type TriageSuggestion = components['schemas']['TriageSuggestionOut']
export type TriageSuggestionApplied = components['schemas']['TriageSuggestionApplyOut']
export type PreferredNameResult = components['schemas']['PreferredNameOut']
export type SharedCompany = components['schemas']['SharedCompanyOut']
export type Interaction = components['schemas']['InteractionOut']
export type TimelineEntry = TriageEvidence['timeline'][number]
export type TriageTag = components['schemas']['TriageTagOut']
export type Tag = components['schemas']['TagOut']

/**
 * The query key prefix for the bulk suggestion's preview. The page invalidates
 * it whenever something may have moved the count the banner is showing.
 */
export const SUGGESTIONS_KEY = ['triage', 'suggestions'] as const

/** The queue the screen is working through: the untriaged, the skipped, or both. */
export type QueueFilter = 'unknown' | 'skip' | 'both'

/** The `states` query the API takes for a filter. Always explicit, never defaulted. */
export function statesFor(filter: QueueFilter): ContactMet[] {
  if (filter === 'both') return ['unknown', 'skip']
  return [filter]
}

/** A non-2xx answer, with the status the screen branches on and the backend's wording. */
export class TriageError extends Error {
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'TriageError'
    this.status = status
    this.detail = detail
  }
}

/** FastAPI puts its wording in `detail`; anything else falls back to `whenUnknown`. */
function detailOf(error: unknown, whenUnknown: string): string {
  if (typeof error === 'object' && error !== null && 'detail' in error) {
    const { detail } = error as { detail: unknown }
    if (typeof detail === 'string') return detail
  }
  return whenUnknown
}

function fail(status: number, error: unknown, whenUnknown: string): never {
  throw new TriageError(status, detailOf(error, whenUnknown))
}

/**
 * The next card, its evidence, and (with `prefetch`) the one after it.
 *
 * `afterId` is the `→` key and the refill cursor both: "the first contact past
 * this id". It writes nothing.
 */
export async function fetchQueue(options: {
  states: ContactMet[]
  afterId?: number | null
  prefetch?: boolean
  signal?: AbortSignal
}): Promise<TriageQueue> {
  const { data, error, response } = await api.GET('/api/v1/triage/next', {
    params: {
      query: {
        states: options.states,
        after_id: options.afterId ?? null,
        prefetch: options.prefetch ?? true,
      },
    },
    signal: options.signal,
  })
  if (data === undefined) fail(response.status, error, 'the triage queue could not be read')
  return data
}

/**
 * `m`, `n`, or `s` on one contact. The answer carries the next card, so a run
 * of fifty costs fifty requests and no waiting between them.
 */
export async function decide(options: {
  contactId: number
  met: ContactMet
  prefetchAfterId: number | null
  states: ContactMet[]
}): Promise<TriageDecisionResult> {
  const { data, error, response } = await api.POST('/api/v1/triage/decisions', {
    params: { query: { states: options.states } },
    body: {
      contact_id: options.contactId,
      met: options.met,
      prefetch_after_id: options.prefetchAfterId,
    },
  })
  if (data === undefined) fail(response.status, error, 'the decision was not recorded')
  return data
}

/**
 * Undo the newest action. `409` when the contact moved on since the decision:
 * the caller offers that as a choice rather than forcing it.
 */
export async function undo(options: {
  force?: boolean
  states: ContactMet[]
}): Promise<TriageUndoResult> {
  const { data, error, response } = await api.POST('/api/v1/triage/undo', {
    params: { query: { states: options.states } },
    body: { force: options.force ?? false },
  })
  if (data === undefined) fail(response.status, error, 'nothing was undone')
  return data
}

/** The `p` key: a manual override that is itself undoable. */
export async function setPreferredName(options: {
  contactId: number
  preferredName: string
}): Promise<PreferredNameResult> {
  const { data, error, response } = await api.PUT(
    '/api/v1/triage/contacts/{contact_id}/preferred-name',
    {
      params: { path: { contact_id: options.contactId } },
      body: { preferred_name: options.preferredName },
    },
  )
  if (data === undefined) fail(response.status, error, 'the name was not saved')
  return data
}

/** The bulk suggestions worth offering. An empty list means: draw no banner. */
export async function fetchSuggestions(options: {
  states: ContactMet[]
  signal?: AbortSignal
}): Promise<TriageSuggestion[]> {
  const { data, error, response } = await api.GET('/api/v1/triage/suggestions', {
    params: { query: { states: options.states } },
    signal: options.signal,
  })
  if (data === undefined) fail(response.status, error, 'the suggestions could not be read')
  return data
}

/**
 * Apply a suggestion, sending back the count the banner drew. A `409` means the
 * set moved; the caller re-previews instead of reporting a failure.
 */
export async function applySuggestion(options: {
  key: string
  expectedCount: number
  states: ContactMet[]
}): Promise<TriageSuggestionApplied> {
  const { data, error, response } = await api.POST('/api/v1/triage/suggestions/{key}/apply', {
    params: { path: { key: options.key }, query: { states: options.states } },
    body: { expected_count: options.expectedCount },
  })
  if (data === undefined) fail(response.status, error, 'the suggestion was not applied')
  return data
}

/** Every tag, for the `t` key's picker. */
export async function fetchTags(signal?: AbortSignal): Promise<Tag[]> {
  const { data, error, response } = await api.GET('/api/v1/tags', { signal })
  if (data === undefined) fail(response.status, error, 'the tags could not be read')
  return data
}

/** Put a manual tag on the contact under triage. */
export async function tagContact(contactId: number, tagId: number): Promise<void> {
  const { error, response } = await api.POST('/api/v1/contacts/{contact_id}/tags', {
    params: { path: { contact_id: contactId } },
    body: { tag_id: tagId },
  })
  if (!response.ok) fail(response.status, error, 'the tag was not applied')
}

/** Take a tag off the contact under triage. */
export async function untagContact(contactId: number, tagId: number): Promise<void> {
  const { error, response } = await api.DELETE('/api/v1/contacts/{contact_id}/tags/{tag_id}', {
    params: { path: { contact_id: contactId, tag_id: tagId } },
  })
  if (!response.ok) fail(response.status, error, 'the tag was not removed')
}
