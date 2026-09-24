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
 * Where it used to disagree with the service it now does not, and the three
 * places that mattered are marked below: `progress` counts only live contacts,
 * a decision row records exactly the fields the service records, and the undo
 * conflict check reads those recorded fields rather than a list of its own.
 * A fake that answers differently from the API is worse than no fake, because
 * the suite goes green on behaviour the product does not have.
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
  /** Invitations on file, which the service counts apart from the messages. */
  invitations: number
  /** The body the newest message carries, verbatim, however unpleasant. */
  messageBody: string | null
}

interface FakeDecision {
  id: number
  contact_id: number
  kind: 'decide' | 'preferred_name' | 'bulk_met' | 'bulk_not_met'
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
  seen: Array<{ method: string; path: string; search: URLSearchParams; body?: unknown }>
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
  /** Make a tag behind the screen's back, so creating that name answers 409. */
  addTagBehindTheScenes(name: string): Tag
}

/** The scalar columns `POST /contacts/query` returns when it is given none. */
const CONTACT_COLUMNS = [
  'first_name',
  'last_name',
  'preferred_name',
  'headline',
  'current_title',
  'current_company',
  'location',
  'connected_on',
  'met',
  'triaged_at',
  'do_not_contact',
  'notes',
] as const

/**
 * The met states a filter tree asks for, or a throw.
 *
 * The triage screen sends one shape: an `eq` on `met`, or an `or` of them.
 * Anything else is a bug in the caller and is reported as one.
 */
interface AheadFilter {
  states: ContactMet[]
  decidedBy: 'manual' | 'automatic' | null
}

/**
 * The look-ahead's filter, as far as this fake understands one.
 *
 * Two shapes and no others: the states alone, or the states `and` who decided
 * them, which is what a review pass asks for. Anything else throws, because a
 * fake that agrees with a filter the screen should not be sending is how a
 * green suite ships a wrong list.
 */
function aheadFilterOf(where: unknown): AheadFilter {
  if (where === null || where === undefined) {
    throw new Error('the look-ahead must filter on met')
  }
  const node = where as { op?: string; field?: string; value?: unknown; children?: unknown[] }
  if (node.op === 'and') {
    const children = node.children ?? []
    if (children.length !== 2) {
      throw new Error('this fake understands one `and`: the states, then who decided them')
    }
    return { states: metStatesOf(children[0]), decidedBy: metSourceOf(children[1]) }
  }
  return { states: metStatesOf(where), decidedBy: null }
}

function metSourceOf(where: unknown): 'manual' | 'automatic' {
  const node = where as { op?: string; field?: string; value?: unknown }
  if (node.op !== 'eq' || node.field !== 'met_source') {
    throw new Error(
      `this fake expects eq on met_source here, not ${String(node.op)} on ${String(node.field)}`,
    )
  }
  if (node.value !== 'manual' && node.value !== 'automatic') {
    throw new Error(`${String(node.value)} is not a met_source`)
  }
  return node.value
}

function metStatesOf(where: unknown): ContactMet[] {
  if (where === null || where === undefined) {
    throw new Error('the look-ahead must filter on met')
  }
  const node = where as { op?: string; field?: string; value?: unknown; children?: unknown[] }
  if (node.op === 'or') {
    return (node.children ?? []).flatMap((child) => metStatesOf(child))
  }
  if (node.op !== 'eq' || node.field !== 'met') {
    throw new Error(
      `this fake only understands eq on met, not ${String(node.op)} on ${String(node.field)}`,
    )
  }
  const value = node.value
  if (value !== 'unknown' && value !== 'met' && value !== 'not_met' && value !== 'skip') {
    throw new Error(`${String(value)} is not a met state`)
  }
  return [value]
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
    met_source: 'manual',
    triaged_at: null,
    do_not_contact: false,
    notes: null,
    tags: [],
    updated_at: '2026-01-02T03:04:05Z',
    messages,
    invitations: 0,
    messageBody: messages > 0 ? 'Good to meet you at the Example Corp meetup.' : null,
    archived_at: null,
    merged_into_id: null,
    needs_review_at: null,
  }
}

/**
 * The fields each kind of decision row records, as `netkeeper.crm.triage` does.
 *
 * `decide` and `bulk_met` snapshot `(met, triaged_at)`; `set_preferred_name`
 * snapshots `(preferred_name,)`. `_log` then derives `after_state` from the
 * same keys, and `_diverged` iterates *those* keys — so a decide row's undo is
 * refused when `triaged_at` moved and is **not** refused when something else
 * renamed the contact. Recording all three fields here made the fake stricter
 * than the API in one direction and blinder in the other.
 */
type RecordedField = 'met' | 'met_source' | 'triaged_at' | 'preferred_name'

// `_MET_FIELDS`: a met decision writes the three together, so it records the
// three and undo puts the three back — `met_source` among them, which is how
// undoing a hand-made decision over a batch's leaves the contact in the review
// queue exactly as the batch left it.
const RECORDED_FIELDS: Record<FakeDecision['kind'], readonly RecordedField[]> = {
  decide: ['met', 'met_source', 'triaged_at'],
  bulk_met: ['met', 'met_source', 'triaged_at'],
  bulk_not_met: ['met', 'met_source', 'triaged_at'],
  preferred_name: ['preferred_name'],
}

/** One recorded column as the log stores it. Throws on a field the log never holds. */
function readField(contact: FakeContact, name: string): string | null {
  if (name === 'met') return contact.met
  if (name === 'met_source') return contact.met_source
  if (name === 'triaged_at') return contact.triaged_at
  if (name === 'preferred_name') return contact.preferred_name
  throw new Error(`the triage log does not record ${name}`)
}

function snapshot(contact: FakeContact, fields: Iterable<string>): Record<string, string | null> {
  const out: Record<string, string | null> = {}
  for (const field of fields) out[field] = readField(contact, field)
  return out
}

/**
 * The batches this fake offers, in the order the service's catalogue runs.
 *
 * Message history first, then one batch per tag the user has given a meaning
 * (`_tag_batches`), met before not met. Two kinds, because one of them decides
 * `not_met` and the screen says so: a catalogue with only `met` batches cannot
 * tell a button that reads the batch from one that assumes the answer.
 *
 * There is no batch here that argues from an *absence*, because there is none
 * in the service any more (#142). `not_met_no_evidence` and the clauses that
 * held people back from it are gone: triage is an affirmative pass, and a quiet
 * message history is not evidence that two people have never met. The only
 * `not_met` left is a tag whose meaning the user declared, which is why the
 * shape of a tag batch is what this fake has to get right.
 */
interface FakeBatch {
  key: string
  title: string
  met: ContactMet
  /** The tag a tag batch is built on; `null` for the message batch. */
  tagId: number | null
  describe: (count: number) => string
  /** ``others`` is every live contact, as the service's clauses read the table. */
  covers: (contact: FakeContact, others: readonly FakeContact[]) => boolean
}

const MESSAGE_BATCH: FakeBatch = {
  key: 'met_with_messages',
  title: 'Mark everyone with message history as met',
  met: 'met',
  tagId: null,
  describe: (count) =>
    `You have message threads with ${count} untriaged ${count === 1 ? 'person' : 'people'}.`,
  covers: (contact) => contact.messages > 0,
}

/** `_tag_batch`: one offer per tag carrying a meaning, however the tag got on. */
function tagBatch(tag: Tag): FakeBatch {
  const decidesMet = tag.met_signal === 'met'
  return {
    key: `tag:${tag.id}`,
    title: `Mark everyone tagged ${tag.name} as ${decidesMet ? 'met' : 'not met'}`,
    met: decidesMet ? 'met' : 'not_met',
    tagId: tag.id,
    // Word for word what `_tag_batch` renders, including the construction that
    // keeps the verb out of the count's way. The fake used to write "person
    // carries", which quietly repaired a sentence the service got wrong — so
    // no test here could ever have shown it.
    describe: (count) =>
      `The tag ${tag.name} is on ${count} untriaged ${count === 1 ? 'person' : 'people'}, ` +
      `which you have said means you ${decidesMet ? 'have met' : 'have not met'} them.`,
    // `_carries_tag`: a rule's tag counts the same as one placed by hand.
    covers: (contact) => contact.tags.some((applied) => applied.id === tag.id),
  }
}

/**
 * The catalogue as it stands, which moves when a tag gains a meaning.
 *
 * `_tag_batches` reads the tags on every call and orders them by signal then
 * name, so `met` batches come before `not_met` ones; a fake that froze the list
 * at startup would not notice a tag given a meaning mid-run.
 */
function batchesFor(tags: readonly Tag[]): FakeBatch[] {
  const signalled = tags
    .filter((tag) => tag.met_signal !== null)
    .sort(
      (a, b) =>
        (a.met_signal ?? '').localeCompare(b.met_signal ?? '') ||
        a.name.toLowerCase().localeCompare(b.name.toLowerCase()),
    )
  return [MESSAGE_BATCH, ...signalled.map(tagBatch)]
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
      met_signal: null,
      contact_count: 3,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    {
      id: 2,
      name: 'investor',
      color: null,
      kind: 'auto',
      met_signal: null,
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
    if (states.length > 0) return states
    // `_states`: with `decided_by` and no states named, the queue defaults to
    // everything a batch can have decided rather than to the untriaged.
    return decidedByOf(search) === null ? ['unknown'] : ['met', 'not_met']
  }

  /** `decided_by`: the review pass narrows the queue by who decided (#128). */
  function decidedByOf(search: URLSearchParams): 'manual' | 'automatic' | null {
    const value = search.get('decided_by')
    return value === 'manual' || value === 'automatic' ? value : null
  }

  function queue(
    states: ContactMet[],
    decidedBy: 'manual' | 'automatic' | null = null,
  ): FakeContact[] {
    return contacts.filter(
      (contact) =>
        states.includes(contact.met) &&
        (decidedBy === null || contact.met_source === decidedBy) &&
        contact.archived_at === null &&
        contact.merged_into_id === null,
    )
  }

  function nextContact(
    states: ContactMet[],
    afterId: number | null,
    decidedBy: 'manual' | 'automatic' | null = null,
  ): FakeContact | null {
    return (
      queue(states, decidedBy).find((contact) => afterId === null || contact.id > afterId) ?? null
    )
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
    // `archived_at` and `merged_into_id` stay on the card: `TriageContactOut`
    // carries them (#91), and a client reads liveness off them instead of
    // inferring it from `TriageUndoOut.forced`, which never meant that.
    const { messages, messageBody: _body, ...out } = contact
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
          // Counted, never among the messages: `_messages` asks the same
          // question the batches ask. The rows themselves are not modelled
          // here -- what this fake is for is the counting.
          invitations: contact.invitations,
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

  /**
   * `netkeeper.crm.triage.progress`: one grouped count over the **live**
   * contacts. Archived and merged-away rows are excluded before grouping, so
   * archiving a contact you had marked met drops both `total` and `triaged`.
   */
  function progress(states: ContactMet[], decidedBy: 'manual' | 'automatic' | null = null) {
    const live = contacts.filter(
      (contact) => contact.archived_at === null && contact.merged_into_id === null,
    )
    const by_state: Record<string, number> = { unknown: 0, met: 0, not_met: 0, skip: 0 }
    for (const contact of live) by_state[contact.met] = (by_state[contact.met] ?? 0) + 1
    const total = live.length
    return {
      total,
      triaged: total - (by_state.unknown ?? 0),
      // `remaining` follows the queue being served, source and all, so the
      // counter and the cards never disagree about what is left.
      remaining: queue(states, decidedBy).length,
      by_state,
      // The size of the review pass: every live contact a batch decided and
      // nobody has answered since, whatever queue is being served.
      automatic: live.filter((contact) => contact.met_source === 'automatic').length,
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
      // `_log`: `after_state = _snapshot(contact, before)` — the same keys.
      after_state: snapshot(contact, Object.keys(before)),
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

  /**
   * The service refuses a batch pointed at an answer somebody gave (#128).
   *
   * `unknown` and `skip` are the only states a batch may reach: `skip` is "I
   * passed on this one", not an answer. The count, the preview, and the apply
   * all refuse alike, so a client can never be shown a batch it would then be
   * refused for.
   */
  function batchStates(states: ContactMet[]): Response | null {
    const answered = states.filter((state) => state !== 'unknown' && state !== 'skip')
    if (answered.length === 0) return null
    return jsonResponse(
      {
        detail: `a batch decides for people nobody has answered for, so it cannot be applied to ${answered.join(', ')}; the states it takes are skip, unknown`,
      },
      422,
    )
  }

  /** The contacts one batch covers, against the queue as it stands. */
  function covered(batch: FakeBatch, states: ContactMet[]): FakeContact[] {
    const live = contacts.filter(
      (contact) => contact.archived_at === null && contact.merged_into_id === null,
    )
    return queue(states).filter((contact) => batch.covers(contact, live))
  }

  async function handler(request: Request): Promise<Response> {
    const url = new URL(request.url)
    if (latency > 0) await new Promise((resolve) => setTimeout(resolve, latency))
    const states = statesOf(url.searchParams)
    const decidedBy = decidedByOf(url.searchParams)
    const body: unknown =
      request.method === 'GET' || request.method === 'DELETE' ? null : await readJson(request)
    // Recorded with the body, so a test can pin what went over the wire and not
    // only what this fake made of it: a field the fake defaults the same way
    // the server does is a field a client can stop sending unnoticed.
    seen.push({ method: request.method, path: url.pathname, search: url.searchParams, body })

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
    if (url.pathname === '/api/v1/tags') {
      if (request.method !== 'POST') return jsonResponse(tags)
      // `create_tag`: a name is unique per user under `tag_name_key`, which
      // folds case and surrounding space, and a repeat is a 409 rather than a
      // second tag.
      const input = (body ?? {}) as { name?: string; kind?: string }
      const name = (input.name ?? '').trim()
      if (name === '') return jsonResponse({ detail: 'a tag needs a name' }, 422)
      if (tags.some((tag) => tag.name.trim().toLowerCase() === name.toLowerCase())) {
        return jsonResponse({ detail: `a tag called ${name} already exists` }, 409)
      }
      const made: Tag = {
        id: Math.max(0, ...tags.map((tag) => tag.id)) + 1,
        name,
        color: null,
        kind: (input.kind ?? 'manual') as Tag['kind'],
        met_signal: null,
        contact_count: 0,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
      }
      tags.push(made)
      return jsonResponse(made, 201)
    }

    if (url.pathname === '/api/v1/triage/next') {
      const afterParam = url.searchParams.get('after_id')
      const afterId = afterParam === null || afterParam === '' ? null : Number(afterParam)
      const first = nextContact(states, afterId, decidedBy)
      const prefetch = url.searchParams.get('prefetch') !== 'false'
      const second = prefetch && first !== null ? nextContact(states, first.id, decidedBy) : null
      return jsonResponse({
        card: card(first),
        next: card(second),
        progress: progress(states, decidedBy),
      })
    }

    if (url.pathname === '/api/v1/triage/decisions') {
      const input = body as {
        contact_id: number
        met: ContactMet
        prefetch_after_id: number | null
      }
      const contact = contacts.find((candidate) => candidate.id === input.contact_id)
      if (contact === undefined) return jsonResponse({ detail: 'no such contact' }, 404)
      const before = snapshot(contact, RECORDED_FIELDS.decide)
      contact.met = input.met
      // Answering here is the person answering, whoever answered before.
      contact.met_source = 'manual'
      contact.triaged_at = new Date().toISOString()
      const decision = record(contact, 'decide', before)
      const after = input.prefetch_after_id ?? contact.id
      return jsonResponse(
        {
          decision,
          next: card(nextContact(states, after, decidedBy)),
          progress: progress(states, decidedBy),
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
          if (contact.merged_into_id !== null) {
            return conflict(
              contact.id,
              'merged_into_id',
              String(contact.merged_into_id),
              'it was merged away after the decision, and the survivor carries that decision now',
            )
          }
          if (contact.archived_at !== null) {
            return conflict(
              contact.id,
              'archived_at',
              contact.archived_at,
              'it was archived after the decision, so undo cannot put it back in the queue',
            )
          }
          // `_diverged` iterates `row.after_state`, so which fields are
          // checked is a property of the row, not a list kept here: a decide
          // row is refused when `triaged_at` moved and never for a rename.
          const field = Object.keys(decision.after_state).find(
            (name) => readField(contact, name) !== decision.after_state[name],
          )
          if (field !== undefined) {
            return conflict(
              contact.id,
              field,
              readField(contact, field),
              'something changed it after the decision, so undo would overwrite that change',
              decision.after_state[field] ?? null,
            )
          }
        }
      }
      const undoneAt = new Date().toISOString()
      for (const decision of batch) {
        const contact = byId(decision.contact_id)
        // `_restore`: every field of `before_state`, and only those. A
        // preferred-name row does not put `met` back.
        for (const [name, value] of Object.entries(decision.before_state)) {
          if (name === 'met') contact.met = (value ?? 'unknown') as ContactMet
          else if (name === 'met_source')
            contact.met_source = (value ?? 'manual') as 'manual' | 'automatic'
          else if (name === 'triaged_at') contact.triaged_at = value
          else if (name === 'preferred_name') contact.preferred_name = value ?? contact.first_name
          else throw new Error(`the triage log does not record ${name}`)
        }
        decision.undone_at = undoneAt
      }
      const single = batch.length === 1 ? byId(batch[0]!.contact_id) : null
      return jsonResponse({
        kind: newest.kind,
        decisions: batch.length,
        batch_id: newest.batch_id,
        forced: input.force === true ? batch.map((decision) => decision.contact_id) : [],
        card: card(single),
        progress: progress(states, decidedBy),
      })
    }

    const nameMatch = /^\/api\/v1\/triage\/contacts\/(\d+)\/preferred-name$/.exec(url.pathname)
    if (nameMatch !== null) {
      const contact = byId(Number(nameMatch[1]))
      const input = body as { preferred_name: string }
      const before = snapshot(contact, RECORDED_FIELDS.preferred_name)
      contact.preferred_name = input.preferred_name.trim() || contact.first_name
      const decision = record(contact, 'preferred_name', before)
      return jsonResponse({
        contact_id: contact.id,
        preferred_name: contact.preferred_name,
        decision,
      })
    }

    // `POST /contacts/query`, as far as the triage screen uses it: the
    // look-ahead list. Deliberately strict — anything the screen might send
    // that this does not understand answers 422 rather than quietly returning
    // the wrong set, because a fake that agrees with a wrong filter is how a
    // green suite ships a broken list.
    if (url.pathname === '/api/v1/contacts/query') {
      const input = (body ?? {}) as {
        filter?: { include_archived?: boolean; where?: unknown } | null
        sort?: unknown[]
        limit?: number
        offset?: number
        columns?: string[] | null
      }
      let ahead: AheadFilter
      try {
        ahead = aheadFilterOf(input.filter?.where)
      } catch (failure) {
        return jsonResponse({ detail: String(failure) }, 422)
      }
      const wanted = ahead.states
      if (input.filter?.include_archived === true) {
        return jsonResponse({ detail: 'the queue never includes archived contacts' }, 422)
      }
      if ((input.sort ?? []).length > 0) {
        return jsonResponse({ detail: 'the queue is id order; this fake takes no sort' }, 422)
      }
      // `compile_where` excludes merged-away rows unconditionally and archived
      // ones unless `include_archived`, which is exactly `_queue_where`.
      const matching = contacts
        .filter(
          (contact) =>
            wanted.includes(contact.met) &&
            (ahead.decidedBy === null || contact.met_source === ahead.decidedBy) &&
            contact.archived_at === null &&
            contact.merged_into_id === null,
        )
        // `apply_sort` ends every ordering with `Contact.id.asc()`, and with no
        // sort keys that is the whole ordering.
        .sort((a, b) => a.id - b.id)
      const offset = input.offset ?? 0
      const limit = input.limit ?? 50
      const columns = input.columns ?? CONTACT_COLUMNS
      return jsonResponse({
        items: matching.slice(offset, offset + limit).map((contact) => ({
          id: contact.id,
          primary_email: null,
          primary_phone: null,
          // Only the columns asked for are set, as `_row` does it.
          ...Object.fromEntries(
            columns.map((name) => [name, contact[name as keyof FakeContact] ?? null]),
          ),
        })),
        total: matching.length,
        describe: wanted.join(' or '),
      })
    }

    if (url.pathname === '/api/v1/triage/suggestions') {
      const refusal = batchStates(states)
      if (refusal !== null) return refusal
      // A batch matching nobody is not offered, and the counts are taken
      // against the queue as it stands, so accepting one shrinks the other.
      return jsonResponse(
        batchesFor(tags)
          .map((batch) => ({ batch, count: covered(batch, states).length }))
          .filter(({ count }) => count > 0)
          .map(({ batch, count }) => ({
            key: batch.key,
            title: batch.title,
            description: batch.describe(count),
            count,
            // The service puts both on every offer: `met` is what the batch
            // decides, which is the word the button says, and `tag_id` names
            // the tag when the batch came from one.
            met: batch.met,
            tag_id: batch.tagId,
          })),
      )
    }

    const contactsMatch = /^\/api\/v1\/triage\/suggestions\/([^/]+)\/contacts$/.exec(url.pathname)
    if (contactsMatch !== null) {
      // A tag batch's key carries a colon, which the client percent-encodes
      // into the path; FastAPI decodes a path parameter before the handler
      // sees it, so this does too. Matching the raw segment made every tag
      // batch answer 404 here while the real API served it.
      const batch = batchesFor(tags).find(
        (candidate) => candidate.key === decodeURIComponent(contactsMatch[1] ?? ''),
      )
      if (batch === undefined) {
        return jsonResponse({ detail: 'no suggestion by that key' }, 404)
      }
      const refusal = batchStates(states)
      if (refusal !== null) return refusal
      const matching = covered(batch, states)
      const limit = Number(url.searchParams.get('limit') ?? '50')
      const offset = Number(url.searchParams.get('offset') ?? '0')
      return jsonResponse({
        items: matching
          .slice(offset, offset + limit)
          .map((contact) => card(contact)?.contact)
          .filter((contact) => contact !== undefined),
        total: matching.length,
        limit,
        offset,
      })
    }

    const applyMatch = /^\/api\/v1\/triage\/suggestions\/([^/]+)\/apply$/.exec(url.pathname)
    if (applyMatch !== null) {
      const batch = batchesFor(tags).find(
        (candidate) => candidate.key === decodeURIComponent(applyMatch[1] ?? ''),
      )
      if (batch === undefined) {
        return jsonResponse({ detail: 'no suggestion by that key' }, 404)
      }
      const refusal = batchStates(states)
      if (refusal !== null) return refusal
      const input = (body ?? {}) as { expected_count?: number | null }
      if (input.expected_count == null) {
        // The body is required and so is the count in it: "apply whatever
        // matches right now" is not a request the API takes, so a client that
        // sends neither gets FastAPI's own 422 rather than a silent apply.
        return jsonResponse(
          { detail: [{ loc: ['body', 'expected_count'], msg: 'Field required' }] },
          422,
        )
      }
      const matching = covered(batch, states)
      if (input.expected_count !== matching.length) {
        return jsonResponse(
          {
            detail: `the suggestion now matches ${matching.length} contacts, not the ${input.expected_count} you were shown; take the preview again`,
          },
          409,
        )
      }
      const batchId = `batch-${nextDecisionId}`
      // `apply_suggestion` writes `batch.kind`, which is `BULK_NOT_MET` for a
      // batch that decides `not_met`. Recording `bulk_met` for all of them was
      // inert while nothing read `kind` back — and tag batches are the only
      // `not_met` batches there are now, so the first test worded off `kind`
      // would have passed here and been wrong in production.
      const kind = batch.met === 'met' ? ('bulk_met' as const) : ('bulk_not_met' as const)
      for (const contact of matching) {
        const before = snapshot(contact, RECORDED_FIELDS[kind])
        contact.met = batch.met
        // What the review pass serves, and what keeps a batch's decision from
        // passing as one the person made.
        contact.met_source = 'automatic'
        contact.triaged_at = new Date().toISOString()
        record(contact, kind, before, batchId)
      }
      return jsonResponse({
        key: batch.key,
        met: batch.met,
        applied: matching.length,
        batch_id: batchId,
        progress: progress(states),
      })
    }

    // Confirm and reject (#184), as `netkeeper.crm.contacts` does them: confirm
    // clears the mark; reject archives and keeps it, and refuses a contact that
    // is not waiting. The answer is a contact detail; the screen reads two fields.
    const reviewMatch = /^\/api\/v1\/contacts\/(\d+)\/(confirm|reject)$/.exec(url.pathname)
    if (reviewMatch !== null && request.method === 'POST') {
      const contact = byId(Number(reviewMatch[1]))
      if (reviewMatch[2] === 'confirm') {
        contact.needs_review_at = null
      } else {
        if (contact.needs_review_at === null) {
          return jsonResponse({ detail: 'this contact is not waiting for review' }, 409)
        }
        contact.archived_at ??= '2026-09-24T12:00:00Z'
      }
      return jsonResponse({ ...contact })
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
    addTagBehindTheScenes(name) {
      const made: Tag = {
        id: Math.max(0, ...tags.map((tag) => tag.id)) + 1,
        name,
        color: null,
        kind: 'manual',
        met_signal: null,
        contact_count: 0,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
      }
      tags.push(made)
      return made
    },
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
      byId(id).archived_at = '2026-09-21T10:00:00Z'
    },
    mergeAway(id, into) {
      byId(id).merged_into_id = into
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
