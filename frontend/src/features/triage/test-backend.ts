/**
 * A stand-in for the triage API, faithful enough to drive the screen.
 *
 * The screen's correctness is mostly about *sequences* — decide, prefetch,
 * undo, undo again, a bulk apply whose count moved — so a handler that answers
 * each request in isolation would not test much. This keeps the state the real
 * service keeps: the queue in id order, the decision log with before and after
 * states, batches, and the conflict check undo runs before it writes.
 *
 * It also counts requests and can add latency, which is how the fifty-contacts
 * budget is measured rather than asserted.
 *
 * Every name, address, and number here is invented. Addresses are `.example`
 * and phone numbers are in the reserved `555-01xx` range (CLAUDE.md: no real
 * personal data in a fixture, ever).
 */

import { jsonResponse } from '@/test/fetch'

import type { ContactMet, Tag, TriageContact, TriageTag } from './api'

const GIVEN = ['Ada', 'Bo', 'Cleo', 'Dev', 'Esme', 'Finn', 'Gita', 'Hal', 'Iris', 'Jai'] as const
const FAMILY = ['Example', 'Sample', 'Placeholder', 'Testerly', 'Fictional'] as const
const COMPANIES = ['Example Corp', 'Sample Industries', 'Placeholder Labs'] as const

export interface FakeContact extends TriageContact {
  /** How many message interactions the evidence panel reports. */
  messages: number
  /** The body the newest message carries, verbatim, however unpleasant. */
  messageBody: string | null
  /** Set by `archive`; undo then refuses the way the service does. */
  archivedAt: string | null
  /** Set by `mergeAway`; undo then refuses the way the service does. */
  mergedIntoId: number | null
}

interface FakeDecision {
  id: number
  contact_id: number
  kind: 'decide' | 'preferred_name' | 'bulk_met'
  before_state: Record<string, string | null>
  after_state: Record<string, string | null>
  batch_id: string | null
  decided_at: string
  undone_at: string | null
}

export interface FakeBackendOptions {
  /** How many contacts the queue starts with. */
  contacts?: number
  /** Milliseconds every answer waits, to stand in for a local backend. */
  latencyMs?: number
  /** How many of the contacts have message history (the bulk suggestion's set). */
  withMessages?: number
  tags?: Tag[]
}

export interface FakeBackend {
  handler: (request: Request) => Promise<Response>
  /** Every request seen, in order. */
  seen: Array<{ method: string; path: string; search: URLSearchParams }>
  countOf(path: string, method?: string): number
  contacts: FakeContact[]
  byId(id: number): FakeContact
  decisions: FakeDecision[]
  /** Change a contact behind the screen's back, so the next undo answers 409. */
  diverge(id: number, patch: Partial<FakeContact>): void
  /** Archive a contact behind the screen's back; undo then refuses. */
  archive(id: number): void
  /** Merge a contact away behind the screen's back; undo then refuses. */
  mergeAway(id: number, into: number): void
  /** Move the suggestion's set, so the next apply answers 409. */
  setMessageCount(id: number, messages: number): void
}

function contactName(index: number): { first: string; last: string } {
  return {
    first: GIVEN[index % GIVEN.length] ?? 'Ada',
    last: FAMILY[index % FAMILY.length] ?? 'Example',
  }
}

export function makeContact(index: number, options: { messages?: number } = {}): FakeContact {
  const id = index + 1
  const { first, last } = contactName(index)
  const messages = options.messages ?? 0
  return {
    id,
    li_public_id: `contact-${id}`,
    li_url: `https://www.example.com/in/contact-${id}`,
    first_name: first,
    last_name: `${last}-${id}`,
    preferred_name: first,
    headline: `Head of Nothing in Particular at ${COMPANIES[index % COMPANIES.length]}`,
    current_title: 'Head of Nothing in Particular',
    current_company: COMPANIES[index % COMPANIES.length] ?? 'Example Corp',
    location: 'Anytown, Example',
    connected_on: '2021-06-14',
    met: 'unknown',
    triaged_at: null,
    do_not_contact: false,
    notes: null,
    tags: [],
    updated_at: '2026-01-02T03:04:05Z',
    messages,
    messageBody: messages > 0 ? 'Good to meet you at the Example Corp meetup.' : null,
    archivedAt: null,
    mergedIntoId: null,
  }
}

const QUEUE_FIELDS = ['met', 'triaged_at', 'preferred_name'] as const

function snapshot(contact: FakeContact): Record<string, string | null> {
  return {
    met: contact.met,
    triaged_at: contact.triaged_at,
    preferred_name: contact.preferred_name,
  }
}

export function createFakeBackend(options: FakeBackendOptions = {}): FakeBackend {
  const total = options.contacts ?? 6
  const withMessages = options.withMessages ?? 0
  const latency = options.latencyMs ?? 0
  const tags: Tag[] = options.tags ?? [
    {
      id: 1,
      name: 'founder',
      color: null,
      kind: 'manual',
      contact_count: 3,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    {
      id: 2,
      name: 'investor',
      color: null,
      kind: 'auto',
      contact_count: 1,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
  ]

  const contacts = Array.from({ length: total }, (_, index) =>
    makeContact(index, { messages: index < withMessages ? 2 : 0 }),
  )
  const decisions: FakeDecision[] = []
  const seen: FakeBackend['seen'] = []
  let nextDecisionId = 1

  function byId(id: number): FakeContact {
    const found = contacts.find((contact) => contact.id === id)
    if (found === undefined) throw new Error(`no fake contact ${id}`)
    return found
  }

  function statesOf(search: URLSearchParams): ContactMet[] {
    const states = search.getAll('states') as ContactMet[]
    return states.length === 0 ? ['unknown'] : states
  }

  function queue(states: ContactMet[]): FakeContact[] {
    return contacts.filter(
      (contact) =>
        states.includes(contact.met) &&
        contact.archivedAt === null &&
        contact.mergedIntoId === null,
    )
  }

  function nextContact(states: ContactMet[], afterId: number | null): FakeContact | null {
    return queue(states).find((contact) => afterId === null || contact.id > afterId) ?? null
  }

  function card(contact: FakeContact | null) {
    if (contact === null) return null
    const recent =
      contact.messages === 0
        ? []
        : [
            {
              id: contact.id * 100,
              contact_id: contact.id,
              kind: 'li_in' as const,
              at: '2024-03-04T10:11:00Z',
              summary: contact.messageBody,
              message_id: null,
              source: 'archive' as const,
              created_at: '2024-03-04T10:11:00Z',
              updated_at: '2024-03-04T10:11:00Z',
            },
          ]
    const {
      messages,
      messageBody: _body,
      archivedAt: _archived,
      mergedIntoId: _merged,
      ...out
    } = contact
    void _archived
    void _merged
    void _body
    return {
      contact: out,
      evidence: {
        messages: {
          total: messages,
          inbound: messages,
          outbound: 0,
          first_at: messages > 0 ? '2024-03-04T10:11:00Z' : null,
          last_at: messages > 0 ? '2024-03-04T10:11:00Z' : null,
          recent,
        },
        timeline: recent.map((interaction) => ({
          kind: 'interaction' as const,
          at: interaction.at,
          interaction,
        })),
        shared_companies: [
          { company: contact.current_company ?? 'Example Corp', contact_count: 2, met_count: 1 },
          { company: 'Nobody Else Here Ltd', contact_count: 0, met_count: 0 },
        ],
      },
    }
  }

  function progress(states: ContactMet[]) {
    const by_state: Record<string, number> = { unknown: 0, met: 0, not_met: 0, skip: 0 }
    for (const contact of contacts) by_state[contact.met] = (by_state[contact.met] ?? 0) + 1
    return {
      total: contacts.length,
      triaged: contacts.filter((contact) => contact.met !== 'unknown').length,
      remaining: queue(states).length,
      by_state,
    }
  }

  function record(
    contact: FakeContact,
    kind: FakeDecision['kind'],
    before: Record<string, string | null>,
    batchId: string | null = null,
  ): FakeDecision {
    const decision: FakeDecision = {
      id: nextDecisionId++,
      contact_id: contact.id,
      kind,
      before_state: before,
      after_state: snapshot(contact),
      batch_id: batchId,
      decided_at: new Date().toISOString(),
      undone_at: null,
    }
    decisions.push(decision)
    return decision
  }

  /** The service's `UndoConflict` message, to the letter. */
  function conflict(
    contactId: number,
    field: string,
    found: string | null,
    reason: string,
    expected: string | null = null,
  ): Response {
    const quote = (value: string | null) => (value === null ? 'None' : `'${value}'`)
    return jsonResponse(
      {
        detail: `contact ${contactId} has ${field}=${quote(found)} where the decision left ${quote(expected)}; ${reason}`,
      },
      409,
    )
  }

  function suggestionCount(states: ContactMet[]): number {
    return queue(states).filter((contact) => contact.messages > 0).length
  }

  async function handler(request: Request): Promise<Response> {
    const url = new URL(request.url)
    seen.push({ method: request.method, path: url.pathname, search: url.searchParams })
    if (latency > 0) await new Promise((resolve) => setTimeout(resolve, latency))
    const states = statesOf(url.searchParams)
    const body: unknown =
      request.method === 'GET' || request.method === 'DELETE' ? null : await readJson(request)

    // The app shell polls these; they are not what this fake is about.
    if (url.pathname === '/api/v1/health') return jsonResponse({ status: 'ok', version: 'test' })
    if (url.pathname === '/api/v1/me') {
      return jsonResponse({
        id: 1,
        kind: 'local',
        display_name: 'Test User',
        email: null,
        timezone: 'UTC',
      })
    }
    if (url.pathname === '/api/v1/tags') return jsonResponse(tags)

    if (url.pathname === '/api/v1/triage/next') {
      const afterParam = url.searchParams.get('after_id')
      const afterId = afterParam === null || afterParam === '' ? null : Number(afterParam)
      const first = nextContact(states, afterId)
      const prefetch = url.searchParams.get('prefetch') !== 'false'
      const second = prefetch && first !== null ? nextContact(states, first.id) : null
      return jsonResponse({ card: card(first), next: card(second), progress: progress(states) })
    }

    if (url.pathname === '/api/v1/triage/decisions') {
      const input = body as {
        contact_id: number
        met: ContactMet
        prefetch_after_id: number | null
      }
      const contact = contacts.find((candidate) => candidate.id === input.contact_id)
      if (contact === undefined) return jsonResponse({ detail: 'no such contact' }, 404)
      const before = snapshot(contact)
      contact.met = input.met
      contact.triaged_at = new Date().toISOString()
      const decision = record(contact, 'decide', before)
      const after = input.prefetch_after_id ?? contact.id
      return jsonResponse(
        {
          decision,
          next: card(nextContact(states, after)),
          progress: progress(states),
        },
        201,
      )
    }

    if (url.pathname === '/api/v1/triage/undo') {
      const input = (body ?? {}) as { force?: boolean }
      const newest = [...decisions].reverse().find((decision) => decision.undone_at === null)
      if (newest === undefined) return jsonResponse({ detail: 'nothing to undo' }, 404)
      const batch =
        newest.batch_id === null
          ? [newest]
          : decisions.filter(
              (decision) => decision.batch_id === newest.batch_id && decision.undone_at === null,
            )
      if (input.force !== true) {
        for (const decision of batch) {
          const contact = byId(decision.contact_id)
          // Liveness first, as the service checks it: neither archiving nor
          // merging moves a recorded field, so the fields alone never show it.
          if (contact.mergedIntoId !== null) {
            return conflict(
              contact.id,
              'merged_into_id',
              String(contact.mergedIntoId),
              'it was merged away after the decision, and the survivor carries that decision now',
            )
          }
          if (contact.archivedAt !== null) {
            return conflict(
              contact.id,
              'archived_at',
              contact.archivedAt,
              'it was archived after the decision, so undo cannot put it back in the queue',
            )
          }
          const now = snapshot(contact)
          const field = QUEUE_FIELDS.find(
            (name) => name !== 'triaged_at' && now[name] !== decision.after_state[name],
          )
          if (field !== undefined) {
            return conflict(
              contact.id,
              field,
              now[field] ?? null,
              'something changed it after the decision, so undo would overwrite that change',
              decision.after_state[field] ?? null,
            )
          }
        }
      }
      const undoneAt = new Date().toISOString()
      for (const decision of batch) {
        const contact = byId(decision.contact_id)
        contact.met = (decision.before_state.met ?? 'unknown') as ContactMet
        contact.triaged_at = decision.before_state.triaged_at ?? null
        contact.preferred_name = decision.before_state.preferred_name ?? contact.first_name
        decision.undone_at = undoneAt
      }
      const single = batch.length === 1 ? byId(batch[0]!.contact_id) : null
      return jsonResponse({
        kind: newest.kind,
        decisions: batch.length,
        batch_id: newest.batch_id,
        forced: input.force === true ? batch.map((decision) => decision.contact_id) : [],
        card: card(single),
        progress: progress(states),
      })
    }

    const nameMatch = /^\/api\/v1\/triage\/contacts\/(\d+)\/preferred-name$/.exec(url.pathname)
    if (nameMatch !== null) {
      const contact = byId(Number(nameMatch[1]))
      const input = body as { preferred_name: string }
      const before = snapshot(contact)
      contact.preferred_name = input.preferred_name.trim() || contact.first_name
      const decision = record(contact, 'preferred_name', before)
      return jsonResponse({
        contact_id: contact.id,
        preferred_name: contact.preferred_name,
        decision,
      })
    }

    if (url.pathname === '/api/v1/triage/suggestions') {
      const count = suggestionCount(states)
      if (count === 0) return jsonResponse([])
      return jsonResponse([
        {
          key: 'met_with_messages',
          title: 'Mark everyone with message history as met',
          description: `You have message threads with ${count} untriaged ${count === 1 ? 'person' : 'people'}.`,
          count,
        },
      ])
    }

    const applyMatch = /^\/api\/v1\/triage\/suggestions\/([^/]+)\/apply$/.exec(url.pathname)
    if (applyMatch !== null) {
      if (applyMatch[1] !== 'met_with_messages') {
        return jsonResponse({ detail: 'no suggestion by that key' }, 404)
      }
      const input = (body ?? {}) as { expected_count?: number | null }
      const matching = queue(states).filter((contact) => contact.messages > 0)
      if (input.expected_count != null && input.expected_count !== matching.length) {
        return jsonResponse(
          {
            detail: `the suggestion now matches ${matching.length} contacts, not the ${input.expected_count} you were shown; take the preview again`,
          },
          409,
        )
      }
      const batchId = `batch-${nextDecisionId}`
      for (const contact of matching) {
        const before = snapshot(contact)
        contact.met = 'met'
        contact.triaged_at = new Date().toISOString()
        record(contact, 'bulk_met', before, batchId)
      }
      return jsonResponse({
        key: 'met_with_messages',
        applied: matching.length,
        batch_id: batchId,
        progress: progress(states),
      })
    }

    const tagMatch = /^\/api\/v1\/contacts\/(\d+)\/tags(?:\/(\d+))?$/.exec(url.pathname)
    if (tagMatch !== null) {
      const contact = byId(Number(tagMatch[1]))
      if (request.method === 'DELETE') {
        contact.tags = contact.tags.filter((tag) => tag.id !== Number(tagMatch[2]))
        return new Response(null, { status: 204 })
      }
      const input = body as { tag_id: number }
      const tag = tags.find((candidate) => candidate.id === input.tag_id)
      if (tag === undefined) return jsonResponse({ detail: 'no such tag' }, 404)
      const assigned: TriageTag = { id: tag.id, name: tag.name, color: tag.color, kind: tag.kind }
      if (!contact.tags.some((existing) => existing.id === tag.id)) contact.tags.push(assigned)
      return jsonResponse(
        {
          id: 1,
          contact_id: contact.id,
          tag_id: tag.id,
          source: 'manual',
          rule_id: null,
          created_at: '2026-01-01T00:00:00Z',
        },
        201,
      )
    }

    return jsonResponse({ detail: `unhandled ${request.method} ${url.pathname}` }, 404)
  }

  return {
    handler,
    seen,
    countOf(path, method) {
      return seen.filter(
        (entry) => entry.path === path && (method === undefined || entry.method === method),
      ).length
    },
    contacts,
    byId,
    decisions,
    diverge(id, patch) {
      Object.assign(byId(id), patch)
    },
    archive(id) {
      byId(id).archivedAt = '2026-09-21T10:00:00Z'
    },
    mergeAway(id, into) {
      byId(id).mergedIntoId = into
    },
    setMessageCount(id, messages) {
      byId(id).messages = messages
    },
  }
}

async function readJson(request: Request): Promise<unknown> {
  const text = await request.text()
  if (text === '') return null
  try {
    return JSON.parse(text) as unknown
  } catch {
    return null
  }
}
