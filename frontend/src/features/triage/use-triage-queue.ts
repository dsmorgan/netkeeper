/**
 * The triage queue: two cards in hand, one request per contact, exact undo,
 * and a trail of the contacts this run has already put on screen.
 *
 * The budget is twelve seconds a contact (P1-14), so the rule this hook exists
 * to keep is that a keypress never waits for the network. It holds a small
 * buffer of cards. A decision pops the head and renders the next one
 * immediately; the `POST` goes out behind it and its answer — which carries the
 * card after the one now on screen — refills the buffer. The steady state is one
 * request per contact and no round trip between two cards.
 *
 * Four details make that safe rather than merely fast:
 *
 * - **Requests are serialized.** One promise chain, in keypress order. Undo pops
 *   the newest decision, so a `u` racing a `POST` that has not landed would undo
 *   the wrong thing; and `prefetch_after_id` has to be the furthest contact the
 *   server has already handed over, which is only known once the previous answer
 *   is in.
 * - **The frontier is remembered, not derived.** `frontierId` is the furthest
 *   contact ever pulled. Deriving it from the buffer hands back a contact the
 *   person has already passed over as soon as `m` and `→` interleave.
 * - **A failed write is loud.** The card is already gone from the screen when the
 *   `POST` answers, so a failure becomes a banner naming the contact, never a
 *   silent drop — and the contact stays in `passed`, marked, so `←` reaches them.
 * - **Nothing is ever dropped in silence.** A key that cannot act says so in
 *   `notice` rather than doing nothing (issue #92).
 *
 * **Going back is not undo (P1-23).** Every card that leaves the front of the
 * queue is kept in `passed` with the decision it carries, and `reviewIndex`
 * points into that trail. Stepping through it sends no request and writes
 * nothing: it is a cursor. `undo` is the other thing entirely — it asks the
 * server to take back the newest write. They can both be on the screen because
 * they are named for what they do, and each one says what just happened in
 * `notice`.
 *
 * Nothing here is optimistic about what the server stores: the progress counters
 * come from the answers. Only the *screen position* runs ahead, and `pending`
 * says how far, so the counters read true mid-run.
 */

import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react'

import {
  AHEAD_PAGE,
  TriageError,
  applySuggestion,
  decide as decideRequest,
  fetchQueue,
  decidedByFor,
  fetchQueueAhead,
  fetchTriageContact,
  reviewContact,
  setPreferredName,
  statesFor,
  tagContact,
  undo as undoRequest,
  untagContact,
  type ContactMet,
  type QueuedContact,
  type QueueFilter,
  type TriageCard,
  type TriageContact,
  type TriageProgress,
  type TriageTag,
} from './api'

/**
 * A contact this run has already put on screen, and what it left behind.
 *
 * The card is kept whole — evidence included — so stepping back costs no request
 * and shows the same screen it showed the first time.
 */
export interface PassedCard {
  card: TriageCard
  /** What was written for them, or `null` when they were only passed over. */
  decision: ContactMet | null
  /** A decision was sent for them and refused: they are still untriaged. */
  failed: boolean
}

/**
 * How far the screen is running ahead of the server's counters.
 *
 * Two numbers rather than one, because a decision does not always move both.
 * Re-deciding somebody already triaged leaves `triaged` alone; marking an
 * untriaged contact `skip` while the Skipped filter is on leaves `remaining`
 * alone, because they stay in the queue.
 */
export interface PendingCounts {
  triaged: number
  removed: number
}

const NO_PENDING: PendingCounts = { triaged: 0, removed: 0 }

export interface TriageQueueState {
  status: 'loading' | 'ready' | 'error'
  /** The buffer. `cards[0]` is the live card; the rest are prefetched. */
  cards: TriageCard[]
  /** Contacts already passed this run, oldest first. The `←` key walks this. */
  passed: PassedCard[]
  /**
   * How many contacts this run has moved past, which is **not** `passed.length`.
   *
   * The trail is capped, so its length stops growing at `MAX_PASSED` while the
   * run goes on. Deriving the position from it froze the counter at "contact
   * 101" for the remaining five hundred of a 616-person queue. This counts the
   * run; the trail only remembers as much of it as it can hold.
   */
  seen: number
  /** Where `←` has walked to, or `null` when the live card is on screen. */
  reviewIndex: number | null
  /**
   * The contacts still waiting, from `POST /contacts/query` in queue order.
   *
   * A page taken from the head of the queue, so the rows before the frontier
   * are cards already in hand and the caller shows only the ones past it.
   */
  ahead: QueuedContact[]
  /** How many the queue holds altogether, as the same answer counted it. */
  aheadTotal: number | null
  /** That page was full, so there is more behind it and another can be asked for. */
  aheadMore: boolean
  /** The look-ahead could not be read. The run carries on without it. */
  aheadError: string | null
  /** The last thing that happened, in the screen's own words. Never an error. */
  notice: string | null
  progress: TriageProgress | null
  /** Decisions sent but not yet answered, so the counters can read true. */
  pending: PendingCounts
  /** The server has said there is nothing past the frontier. */
  exhausted: boolean
  /** The queue could not be loaded at all: the screen shows a retry. */
  loadError: string | null
  /**
   * Actions that failed, oldest first. The screen keeps working around them.
   *
   * A list rather than one slot, and never cleared by the next keystroke. A
   * decision is recorded behind a card that has already left the screen, so the
   * only evidence that a write did not land is this banner; a run of fifty that
   * quietly loses one would end with the person believing fifty were triaged.
   * It goes away when they dismiss it and not before.
   */
  actionErrors: string[]
  /** A `409` from undo, as the backend worded it. The person chooses what happens. */
  undoConflict: string | null
  /**
   * What `u` would take back, newest first.
   *
   * The undo stack lives on the server; this is the part of it this session
   * caused, kept so the button can name what it will undo. Tagging never goes
   * on it: `t` writes through the contacts API (P1-07), not through triage, so
   * `u` after `t` reaches past the tag to the decision before it.
   */
  undoable: UndoableAction[]
  /** A tag was applied or removed after the newest undoable action. */
  taggedSinceUndoable: boolean
  /** An undo that put a contact back that this queue will not serve again. */
  restored: string | null
}

export interface UndoableAction {
  /** This session's own id for the action, so a retraction cannot hit a twin. */
  seq: number
  kind: 'decision' | 'preferred-name'
  /** How the button names it: "met — Alex Example". */
  label: string
}

const INITIAL: TriageQueueState = {
  status: 'loading',
  cards: [],
  passed: [],
  seen: 0,
  reviewIndex: null,
  ahead: [],
  aheadTotal: null,
  aheadMore: false,
  aheadError: null,
  notice: null,
  progress: null,
  pending: NO_PENDING,
  exhausted: false,
  loadError: null,
  actionErrors: [],
  undoConflict: null,
  undoable: [],
  taggedSinceUndoable: false,
  restored: null,
}

/**
 * How far back `←` reaches.
 *
 * The trail holds whole cards, evidence included, so it is bounded rather than
 * left to grow with a run of thousands. A hundred contacts is far more than the
 * fifty the budget is written around, and the screen says when the trail has
 * been trimmed rather than pretending the run started there.
 */
const MAX_PASSED = 100

/** Ask for another page of the contacts ahead once the tail is this short. */
const AHEAD_MIN = 10

/**
 * How many failed decisions are remembered, so the map is bounded like the trail.
 *
 * An entry only matters to an undo pressed underneath the decision it names,
 * which is the same tick or the next few; keeping the newest fifty is far more
 * than that needs and stops a long run with a flaky backend from growing a map
 * nobody reads.
 */
const MAX_RETRACTED = 50

/** What a bulk apply did, so the banner can re-preview instead of reporting a failure. */
export type BulkOutcome =
  | { kind: 'applied'; applied: number }
  | { kind: 'count-changed'; detail: string }
  | { kind: 'failed'; detail: string }

export interface TriageQueue extends TriageQueueState {
  /** The contact on screen: the live card, or the one `←` has walked back to. */
  readonly current: TriageCard | null
  /** The trail entry on screen, or `null` when the live card is. */
  readonly reviewing: PassedCard | null
  /** The head of the buffer, whatever `←` is looking at. */
  readonly liveCard: TriageCard | null
  /** The cards already in hand behind the live one. */
  readonly inHand: readonly TriageCard[]
  /** The contacts past everything in hand, in the order the queue will serve them. */
  readonly waiting: readonly QueuedContact[]
  /** The run position of `passed[0]`, so a trail entry can name its place in the run. */
  readonly trailOffset: number
  /** True while the buffer is empty but more is coming. */
  readonly refilling: boolean
  /** The card on screen, read fresh — safe from a key pressed before a re-render. */
  peek: () => TriageCard | null
  /** Record a decision for the contact on screen. `false` when there was none. */
  decide: (met: ContactMet) => boolean
  /** The `←` key: walk back through contacts already passed. Writes nothing. */
  back: () => void
  /** The `→` key: forward through the trail, or on past the live card. */
  forward: () => boolean
  /** Open a contact from the trail by its place in it. */
  goTo: (index: number) => void
  /** Leave the trail and return to the live card. */
  resume: () => void
  /**
   * Triage this contact next, without deciding the ones before them (#322).
   *
   * They go to the front of the buffer; the cards that were in hand stay
   * behind them, so the normal order carries on once they are answered.
   */
  jumpTo: (contactId: number) => Promise<void>
  /** Say something happened that the screen would otherwise swallow. */
  notify: (text: string) => void
  /** Resolves once the undo has landed, so a caller can re-run what it invalidates. */
  undo: (options?: { force?: boolean }) => Promise<void>
  dismissConflict: () => void
  dismissError: () => void
  rename: (contactId: number, preferredName: string) => Promise<void>
  /** Confirm or reject a contact read off a connections-page card (#184). */
  review: (contactId: number, verdict: 'confirm' | 'reject') => Promise<void>
  addTag: (contactId: number, tag: TriageTag) => Promise<void>
  removeTag: (contactId: number, tagId: number) => Promise<void>
  applyBulk: (key: string, expectedCount: number) => Promise<BulkOutcome>
  reload: () => void
}

function messageOf(error: unknown, fallback: string): string {
  if (error instanceof TriageError) return error.detail
  if (error instanceof Error) return error.message
  return fallback
}

const MET_LABELS: Record<ContactMet, string> = {
  met: 'met',
  not_met: 'not met',
  skip: 'skipped',
  unknown: 'untriaged',
}

/** Append a failure without disturbing the ones already on screen. */
function withFailure(state: TriageQueueState, failure: string): TriageQueueState {
  return { ...state, actionErrors: [...state.actionErrors, failure] }
}

function nameOf(card: TriageCard): string {
  const { preferred_name, last_name } = card.contact
  return `${preferred_name} ${last_name}`.trim()
}

/** The card on screen: the trail entry `←` walked to, or the head of the buffer. */
function onScreen(state: TriageQueueState): TriageCard | null {
  if (state.reviewIndex === null) return state.cards[0] ?? null
  return state.passed[state.reviewIndex]?.card ?? null
}

/**
 * What going back to this contact should say, which is never "undone".
 *
 * `decision` is what the server holds for them, and `failed` says the last
 * thing sent for them was refused — so the two together, not either alone, are
 * what the person needs to read.
 */
function reviewNotice(entry: PassedCard): string {
  const name = nameOf(entry.card)
  if (entry.failed) {
    const holds =
      entry.decision === null
        ? 'they are still untriaged'
        : `they are still ${MET_LABELS[entry.decision]}`
    return `Back at ${name}. The last decision sent for them was not recorded, so ${holds} — deciding now tries again.`
  }
  if (entry.decision === null) {
    return `Back at ${name}. You moved past them without deciding; nothing has been written for them.`
  }
  return `Back at ${name}, who you marked ${MET_LABELS[entry.decision]}. Going back wrote nothing; deciding again replaces it.`
}

/**
 * What the undo did to the contact's row in this run, when it has one.
 *
 * "The card did not change" is true of the card and false of the screen: the
 * trail row for that contact visibly moves from Not met back to Met. Saying
 * only the first half reads as "nothing happened", which is the one thing undo
 * must never look like.
 */
function trailNote(passed: readonly PassedCard[], contactId: number): string {
  const entry = passed.find((row) => row.card.contact.id === contactId)
  return entry === undefined ? '' : ` Their row in this run reads ${passedStateLabel(entry)} now.`
}

/** How the trail names a contact's state, for the queue list's chips. */
export function passedStateLabel(entry: PassedCard): string {
  if (entry.failed) return 'Not recorded'
  if (entry.decision === null) return 'Passed over'
  return { met: 'Met', not_met: 'Not met', skip: 'Skipped', unknown: 'Untriaged' }[entry.decision]
}

/**
 * Drop the oldest of the trail once it is longer than a run needs.
 *
 * Every caller also increments `seen`, which is the run's own counter and does
 * not stop at the cap.
 */
function remember(passed: PassedCard[], entry: PassedCard): PassedCard[] {
  const next = [...passed, entry]
  return next.length > MAX_PASSED ? next.slice(next.length - MAX_PASSED) : next
}

/** Rewrite one contact wherever the queue is holding it: in hand or in the trail. */
function patchContact(
  state: TriageQueueState,
  contactId: number,
  patch: (contact: TriageContact) => TriageContact,
): TriageQueueState {
  const one = (card: TriageCard): TriageCard =>
    card.contact.id === contactId ? { ...card, contact: patch(card.contact) } : card
  return {
    ...state,
    cards: state.cards.map(one),
    passed: state.passed.map((entry) =>
      entry.card.contact.id === contactId ? { ...entry, card: one(entry.card) } : entry,
    ),
  }
}

export function useTriageQueue(filter: QueueFilter): TriageQueue {
  const stateRef = useRef<TriageQueueState>(INITIAL)
  const [, render] = useReducer((count: number) => count + 1, 0)
  const alive = useRef(true)
  /** The furthest contact the server has handed over; the refill cursor. */
  const frontierRef = useRef<number | null>(null)
  const chain = useRef<Promise<unknown>>(Promise.resolve())
  /** Bumped per undoable action, so a retracted one is found by identity. */
  const sequence = useRef(0)
  /**
   * Decisions whose write failed, by `seq`, with the contact each one named.
   *
   * Serializing the requests puts them in keypress order, which is enough while
   * they all land. It is not enough when one does not: `u` pressed underneath a
   * decision that is still in flight means "take back *that* decision", and if
   * that decision never reached the server's stack, the undo would reach past
   * it and silently revert the contact before it — one the queue will not serve
   * again either, because both sit behind the frontier. So the intent is
   * captured when `u` is pressed and checked against this map when its turn
   * comes.
   */
  const retracted = useRef(new Map<number, string>())
  /** The look-ahead read in flight, kept off the decision chain and abortable. */
  const aheadRequest = useRef<AbortController | null>(null)
  const states = useMemo(() => statesFor(filter), [filter])
  const decidedBy = useMemo(() => decidedByFor(filter), [filter])

  const commit = useCallback((update: (state: TriageQueueState) => TriageQueueState) => {
    stateRef.current = update(stateRef.current)
    if (alive.current) render()
  }, [])

  /** Run `task` after everything already queued, whether or not that failed. */
  const enqueue = useCallback(<T>(task: () => Promise<T>): Promise<T> => {
    const run = chain.current.then(task, task)
    chain.current = run.then(
      () => undefined,
      () => undefined,
    )
    return run
  }, [])

  /**
   * Take a page of the contacts ahead.
   *
   * Deliberately **not** on the serialized chain: it is a read that touches
   * neither the frontier nor the undo stack, and putting it there would make
   * the next decision's `POST` queue behind it. A failure leaves the panel
   * showing what is in hand and says so; the run does not depend on it.
   */
  const refreshAhead = useCallback(() => {
    aheadRequest.current?.abort()
    const controller = new AbortController()
    aheadRequest.current = controller
    void fetchQueueAhead({ states, decidedBy, signal: controller.signal }).then(
      (page) => {
        if (controller.signal.aborted) return
        commit((state) => ({
          ...state,
          ahead: page.contacts,
          aheadTotal: page.total,
          aheadMore: page.contacts.length >= AHEAD_PAGE,
          aheadError: null,
        }))
      },
      (error: unknown) => {
        if (controller.signal.aborted) return
        commit((state) => ({
          ...state,
          aheadError: messageOf(error, 'the contacts ahead could not be read'),
        }))
      },
    )
  }, [commit, decidedBy, states])

  const load = useCallback(async () => {
    commit((state) => ({
      ...INITIAL,
      progress: state.progress,
      undoable: state.undoable,
      taggedSinceUndoable: state.taggedSinceUndoable,
    }))
    frontierRef.current = null
    try {
      const queue = await fetchQueue({ states, decidedBy, prefetch: true })
      const cards = [queue.card, queue.next].filter((card): card is TriageCard => card !== null)
      frontierRef.current = cards.at(-1)?.contact.id ?? null
      commit((state) => ({
        ...state,
        status: 'ready',
        cards,
        progress: queue.progress,
        exhausted: queue.next === null,
      }))
      refreshAhead()
    } catch (error) {
      commit((state) => ({
        ...state,
        status: 'error',
        loadError: messageOf(error, 'the triage queue could not be read'),
      }))
    }
  }, [commit, decidedBy, refreshAhead, states])

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      aheadRequest.current?.abort()
    }
  }, [])

  // Re-runs when the filter changes, because `load` closes over it.
  useEffect(() => {
    void enqueue(load)
  }, [enqueue, load])

  /**
   * Fold a refill answer into the buffer and move the frontier with it.
   *
   * Returns `true` when the card was one this run already holds or has passed
   * (a contact jumped to from past the frontier, then skipped, comes round
   * again in the Skipped and Both queues). It is dropped rather than shown
   * twice, and the caller asks for the one after it (#322).
   */
  const absorb = useCallback(
    (card: TriageCard | null, progress: TriageProgress, settle: PendingCounts | null): boolean => {
      if (card !== null) frontierRef.current = card.contact.id
      const known = stateRef.current
      const duplicate =
        card !== null &&
        (known.cards.some((held) => held.contact.id === card.contact.id) ||
          known.passed.some((entry) => entry.card.contact.id === card.contact.id))
      commit((state) => ({
        ...state,
        progress,
        pending:
          settle === null
            ? state.pending
            : {
                triaged: state.pending.triaged - settle.triaged,
                removed: state.pending.removed - settle.removed,
              },
        cards: card === null || duplicate ? state.cards : [...state.cards, card],
        exhausted: card === null,
      }))
      return duplicate
    },
    [commit],
  )

  /** After a dropped duplicate, keep asking for the next contact until one is new. */
  const pastDuplicates = useCallback(
    async (duplicate: boolean): Promise<void> => {
      let again = duplicate
      while (again) {
        const queue = await fetchQueue({
          states,
          decidedBy,
          afterId: frontierRef.current,
          prefetch: false,
        })
        again = absorb(queue.card, queue.progress, null)
      }
    },
    [absorb, decidedBy, states],
  )

  const decide = useCallback(
    (met: ContactMet): boolean => {
      const before = stateRef.current
      const reviewIndex = before.reviewIndex
      const entry = reviewIndex === null ? null : before.passed[reviewIndex]
      const card = reviewIndex === null ? before.cards[0] : entry?.card
      if (card === undefined) return false

      const was = card.contact.met
      // Both counters move only when this decision actually changes the thing
      // they count: re-deciding somebody already triaged adds no triage, and a
      // decision that keeps the contact inside the filter takes nothing off it.
      const settle: PendingCounts = {
        triaged: was === 'unknown' && met !== 'unknown' ? 1 : 0,
        removed: states.includes(was) && !states.includes(met) ? 1 : 0,
      }
      const decisionLabel = `${MET_LABELS[met]} — ${nameOf(card)}`
      const seq = (sequence.current += 1)
      const decided: TriageCard = { ...card, contact: { ...card.contact, met } }

      commit((state) => {
        const shared = {
          ...state,
          pending: {
            triaged: state.pending.triaged + settle.triaged,
            removed: state.pending.removed + settle.removed,
          },
          // `actionErrors` is deliberately untouched: a write that did not land
          // must not be erased by the next keystroke.
          undoConflict: null,
          restored: null,
          taggedSinceUndoable: false,
          undoable: [{ seq, kind: 'decision' as const, label: decisionLabel }, ...state.undoable],
        }
        if (reviewIndex === null) {
          return {
            ...shared,
            cards: state.cards.slice(1),
            seen: state.seen + 1,
            passed: remember(state.passed, { card: decided, decision: met, failed: false }),
            notice: null,
          }
        }
        // Changing a decision from the trail. Nothing leaves the buffer — the
        // contact is behind the frontier and was never going to be served
        // again — though the answer's prefetch still lands in it through
        // `absorb`, so the cards in hand run one further ahead than before.
        // The cursor steps forward, so `← m` puts the person back where they
        // were with one more key.
        const passed = state.passed.map((old, index) =>
          index === reviewIndex ? { card: decided, decision: met, failed: false } : old,
        )
        const moved = reviewIndex + 1
        const stillReviewing = moved < passed.length
        return {
          ...shared,
          passed,
          reviewIndex: stillReviewing ? moved : null,
          notice:
            `${nameOf(card)} is ${MET_LABELS[met]} now` +
            (was === 'unknown' ? '.' : ` instead of ${MET_LABELS[was]}.`),
        }
      })

      void enqueue(async () => {
        try {
          const result = await decideRequest({
            contactId: card.contact.id,
            met,
            prefetchAfterId: frontierRef.current,
            states,
            decidedBy,
          })
          const duplicate = absorb(result.next, result.progress, settle)
          try {
            await pastDuplicates(duplicate)
          } catch (refill) {
            commit((state) =>
              withFailure(state, messageOf(refill, 'the next contact could not be read')),
            )
          }
        } catch (error) {
          // Nothing was written, so the trail goes back to what the contact
          // actually holds — which is not always "untriaged": this may have
          // been a change to a decision that did land. Retracting the undoable
          // entry keeps `u` from offering to take back a write that never
          // happened, the trail marks the contact so `←` reaches them, and the
          // note keeps an undo already pressed underneath this decision from
          // reaching past it to the one before.
          retracted.current.set(seq, nameOf(card))
          // Bounded like the trail: an entry is only ever read by an undo
          // pressed underneath the decision it names.
          while (retracted.current.size > MAX_RETRACTED) {
            const oldest = retracted.current.keys().next()
            if (oldest.done === true) break
            retracted.current.delete(oldest.value)
          }
          const unchanged: PassedCard = {
            card,
            decision: entry?.decision ?? (was === 'unknown' ? null : was),
            failed: true,
          }
          const stays =
            was === 'unknown'
              ? 'They stay untriaged and come back the next time this queue is loaded'
              : `They stay ${MET_LABELS[was]}`
          commit((state) =>
            withFailure(
              {
                ...state,
                pending: {
                  triaged: state.pending.triaged - settle.triaged,
                  removed: state.pending.removed - settle.removed,
                },
                passed: state.passed.map((old) =>
                  old.card.contact.id === card.contact.id ? unchanged : old,
                ),
                undoable: state.undoable.filter((action) => action.seq !== seq),
              },
              `${nameOf(card)} was not recorded: ${messageOf(error, 'the request failed')}. ${stays}; ← goes back to them in this run.`,
            ),
          )
        }
      })
      return true
    },
    [absorb, commit, decidedBy, enqueue, pastDuplicates, states],
  )

  const skipAhead = useCallback((): boolean => {
    const card = stateRef.current.cards[0]
    if (card === undefined) return false
    commit((state) => ({
      ...state,
      cards: state.cards.slice(1),
      seen: state.seen + 1,
      passed: remember(state.passed, { card, decision: null, failed: false }),
      undoConflict: null,
      notice: null,
    }))
    // The frontier is the end of the queue, so there is nothing to ask for.
    if (stateRef.current.exhausted) return true
    void enqueue(async () => {
      try {
        const queue = await fetchQueue({
          states,
          decidedBy,
          afterId: frontierRef.current,
          prefetch: false,
        })
        await pastDuplicates(absorb(queue.card, queue.progress, null))
      } catch (error) {
        commit((state) =>
          withFailure(state, messageOf(error, 'the next contact could not be read')),
        )
      }
    })
    return true
  }, [absorb, commit, decidedBy, enqueue, pastDuplicates, states])

  const back = useCallback(() => {
    commit((state) => {
      if (state.passed.length === 0) {
        return {
          ...state,
          notice: 'Nothing behind you: this is the first contact of the run.',
        }
      }
      const at = state.reviewIndex ?? state.passed.length
      if (at === 0) {
        return {
          ...state,
          notice: `That is as far back as this run goes — the last ${state.passed.length} contacts you saw.`,
        }
      }
      const index = at - 1
      const entry = state.passed[index]
      if (entry === undefined) return state
      return { ...state, reviewIndex: index, notice: reviewNotice(entry) }
    })
  }, [commit])

  const resumeNotice = useCallback((state: TriageQueueState): string => {
    const live = state.cards[0]
    return live === undefined
      ? 'Back at the end of the queue; there is nothing left to triage here.'
      : `Back at the queue, on ${nameOf(live)}.`
  }, [])

  const forward = useCallback((): boolean => {
    if (stateRef.current.reviewIndex === null) return skipAhead()
    commit((state) => {
      if (state.reviewIndex === null) return state
      const index = state.reviewIndex + 1
      const entry = state.passed[index]
      if (entry === undefined) {
        return { ...state, reviewIndex: null, notice: resumeNotice(state) }
      }
      return { ...state, reviewIndex: index, notice: reviewNotice(entry) }
    })
    return true
  }, [commit, resumeNotice, skipAhead])

  const goTo = useCallback(
    (index: number) => {
      commit((state) => {
        const entry = state.passed[index]
        if (entry === undefined) return state
        return { ...state, reviewIndex: index, notice: reviewNotice(entry) }
      })
    },
    [commit],
  )

  const resume = useCallback(() => {
    commit((state) => {
      if (state.reviewIndex !== null) {
        return { ...state, reviewIndex: null, notice: resumeNotice(state) }
      }
      // The row for the live card is a button whether or not the cursor is in
      // the trail, so that using the list never destroys the element the click
      // was on and drops focus to the document. Pressed here it has nothing to
      // do, so it says so rather than looking broken.
      const live = state.cards[0]
      return live === undefined
        ? state
        : { ...state, notice: `You are on ${nameOf(live)} already.` }
    })
  }, [commit, resumeNotice])

  const jumpTo = useCallback(
    async (contactId: number): Promise<void> => {
      if (stateRef.current.cards[0]?.contact.id === contactId) {
        commit((state) => ({
          ...state,
          reviewIndex: null,
          notice: `You are on ${nameOf(state.cards[0] as TriageCard)} already.`,
        }))
        return
      }
      try {
        const card = await fetchTriageContact({ contactId, states, decidedBy })
        commit((state) => ({
          ...state,
          reviewIndex: null,
          // A contact already in hand moves up rather than appearing twice.
          cards: [card, ...state.cards.filter((held) => held.contact.id !== contactId)],
          // Off the look-ahead too, so the list does not show them as waiting.
          ahead: state.ahead.filter((row) => row.id !== contactId),
          undoConflict: null,
          notice: `Jumped to ${nameOf(card)}. Once you answer, the queue carries on where it was.`,
        }))
      } catch (error) {
        commit((state) =>
          withFailure(
            state,
            `Could not jump to that contact: ${messageOf(error, 'the request failed')}.`,
          ),
        )
      }
    },
    [commit, decidedBy, states],
  )

  const notify = useCallback(
    (text: string) => {
      commit((state) => ({ ...state, notice: text }))
    },
    [commit],
  )

  const peek = useCallback(() => onScreen(stateRef.current), [])

  const undo = useCallback(
    (options?: { force?: boolean }): Promise<void> => {
      // All of these are read when the key is pressed, not when the request
      // goes out, because they are what the person meant by pressing it.
      const taking = stateRef.current.undoable[0]
      const meant = taking?.seq
      const force = options?.force ?? false
      return enqueue(async () => {
        const retraction = meant === undefined ? undefined : retracted.current.get(meant)
        if (retraction !== undefined) {
          // The decision this `u` was aimed at never reached the server. Sending
          // the request now would take back the decision *before* it.
          commit((state) =>
            withFailure(
              state,
              `Nothing was taken back: the decision on ${retraction} never reached the server, so there was nothing to undo.`,
            ),
          )
          return
        }
        try {
          const result = await undoRequest({ force, states, decidedBy })
          const undone = taking === undefined ? 'the newest triage decision' : `“${taking.label}”`
          if (result.card === null) {
            // A bulk batch put many contacts back: the buffer no longer
            // describes the queue, so take it again.
            await load()
            commit((state) => ({
              ...state,
              undoable: state.undoable.slice(1),
              taggedSinceUndoable: false,
              notice: `Undo took back ${undone}.`,
              restored: `Put ${result.decisions} contacts back the way they were.`,
            }))
            return
          }
          const restored = result.card
          // The card comes off the restored contact whatever state that left it
          // in, so it is not always one this queue would serve — and there are
          // two different ways that happens. Undoing a preferred-name edit on
          // somebody already marked met hands back a `met` contact, who is
          // simply in another state; a contact archived or merged away is
          // restored without returning to the queue at all, whatever `met` says.
          //
          // The card says which (#91). `result.forced` cannot: the service
          // appends to it for *any* divergence a force overrode, an ordinary
          // edited field included, which is the common case — an archive or a
          // merge needs another actor. Reading liveness off the fields is the
          // difference between "they are not in this queue" and a notice about
          // an archive that never happened.
          const leftQueue =
            restored.contact.archived_at !== null || restored.contact.merged_into_id !== null
          const inQueue = !leftQueue && states.includes(restored.contact.met)
          commit((state) => {
            const head = state.cards[0]
            const replaces =
              !leftQueue && head !== undefined && head.contact.id === restored.contact.id
            const cards = replaces
              ? [restored, ...state.cards.slice(1)]
              : inQueue
                ? [restored, ...state.cards]
                : state.cards
            // A contact put back at the front of the queue is ahead again, not
            // behind, so the trail lets go of them rather than listing them in
            // both places. One left where they are keeps their place in the
            // trail, with whatever the undo made of their decision.
            const returning = !replaces && inQueue
            const passed = returning
              ? state.passed.filter((entry) => entry.card.contact.id !== restored.contact.id)
              : state.passed.map((entry) =>
                  entry.card.contact.id === restored.contact.id
                    ? {
                        card: restored,
                        decision: restored.contact.met === 'unknown' ? null : restored.contact.met,
                        failed: false,
                      }
                    : entry,
                )
            const reviewIndex =
              state.reviewIndex === null || state.reviewIndex < passed.length
                ? state.reviewIndex
                : null
            // A contact the trail let go of is one the run has *un*-passed, so
            // the run's own counter walks back with it. Counted from the trail
            // rather than from `returning`, because that is the branch where a
            // row is actually dropped and this can only ever be 0 or 1: leaving
            // it out froze the position and, through `trailOffset`, renumbered
            // every row behind it as well.
            const unpassed = state.passed.length - passed.length
            return {
              ...state,
              status: 'ready',
              cards,
              passed,
              seen: state.seen - unpassed,
              reviewIndex: returning ? null : reviewIndex,
              progress: result.progress,
              undoConflict: null,
              undoable: state.undoable.slice(1),
              taggedSinceUndoable: false,
              notice: `Undo took back ${undone}.`,
              restored:
                replaces || inQueue
                  ? null
                  : `Put ${restored.contact.preferred_name} ${restored.contact.last_name} back the way they were. ${
                      leftQueue
                        ? 'They were archived or merged away since, so they are not in this queue and the card did not change.'
                        : 'They are not in this queue, so the card did not change.'
                    }${trailNote(passed, restored.contact.id)}`,
            }
          })
        } catch (error) {
          if (error instanceof TriageError && error.status === 409 && error.reason === 'raced') {
            // Another undo, from another tab or window, took this decision back
            // first. Not a conflict: offering "Undo anyway" would send a forced
            // undo that reaches the decision *before* this one, unchecked
            // (#222). The decision is spent either way, so it leaves the stack,
            // and the buffer describes a queue that undo has since changed.
            await load()
            commit((state) => ({
              ...state,
              undoable: state.undoable.slice(1),
              taggedSinceUndoable: false,
              notice: `Another undo took back ${
                taking === undefined ? 'the newest triage decision' : `“${taking.label}”`
              } first, so this one changed nothing. The queue has been read again.`,
            }))
            return
          }
          if (error instanceof TriageError && error.status === 409) {
            commit((state) => ({
              ...state,
              undoConflict: error.detail,
            }))
            return
          }
          if (error instanceof TriageError && error.status === 404) {
            commit((state) => withFailure(state, 'Nothing left to undo.'))
            return
          }
          commit((state) => withFailure(state, messageOf(error, 'the undo failed')))
        }
      })
    },
    [commit, decidedBy, enqueue, load, states],
  )

  const rename = useCallback(
    async (contactId: number, preferredName: string) => {
      await enqueue(async () => {
        try {
          const result = await setPreferredName({ contactId, preferredName })
          commit((state) =>
            patchContact(
              {
                ...state,
                restored: null,
                taggedSinceUndoable: false,
                undoable: [
                  {
                    seq: (sequence.current += 1),
                    kind: 'preferred-name' as const,
                    label: `name — ${result.preferred_name}`,
                  },
                  ...state.undoable,
                ],
              },
              contactId,
              (contact) => ({ ...contact, preferred_name: result.preferred_name }),
            ),
          )
        } catch (error) {
          commit((state) => withFailure(state, messageOf(error, 'the name was not saved')))
        }
      })
    },
    [commit, enqueue],
  )

  const review = useCallback(
    async (contactId: number, verdict: 'confirm' | 'reject') => {
      await enqueue(async () => {
        try {
          const outcome = await reviewContact(contactId, verdict)
          commit((state) => {
            const patched = patchContact(state, contactId, (contact) => ({
              ...contact,
              needs_review_at: outcome.needs_review_at,
              archived_at: outcome.archived_at,
            }))
            // Neither is a triage decision, so neither goes on the undo stack:
            // a rejected contact comes back from its own page, with Unarchive.
            return {
              ...patched,
              notice:
                verdict === 'confirm'
                  ? 'Confirmed. netkeeper treats this contact like any other now.'
                  : 'Rejected and archived, so this contact leaves the queue. → moves on; Unarchive on its page brings it back.',
            }
          })
        } catch (error) {
          commit((state) =>
            withFailure(
              state,
              messageOf(
                error,
                verdict === 'confirm'
                  ? 'the contact was not confirmed'
                  : 'the contact was not rejected',
              ),
            ),
          )
        }
      })
    },
    [commit, enqueue],
  )

  const patchTags = useCallback(
    (contactId: number, update: (tags: TriageTag[]) => TriageTag[]) => {
      commit((state) =>
        patchContact(state, contactId, (contact) => ({ ...contact, tags: update(contact.tags) })),
      )
    },
    [commit],
  )

  const addTag = useCallback(
    async (contactId: number, tag: TriageTag) => {
      await enqueue(async () => {
        try {
          await tagContact(contactId, tag.id)
          patchTags(contactId, (tags) =>
            tags.some((existing) => existing.id === tag.id) ? tags : [...tags, tag],
          )
          commit((state) => ({ ...state, taggedSinceUndoable: true }))
        } catch (error) {
          commit((state) => withFailure(state, messageOf(error, 'the tag was not applied')))
        }
      })
    },
    [commit, enqueue, patchTags],
  )

  const removeTag = useCallback(
    async (contactId: number, tagId: number) => {
      await enqueue(async () => {
        try {
          await untagContact(contactId, tagId)
          patchTags(contactId, (tags) => tags.filter((tag) => tag.id !== tagId))
          commit((state) => ({ ...state, taggedSinceUndoable: true }))
        } catch (error) {
          commit((state) => withFailure(state, messageOf(error, 'the tag was not removed')))
        }
      })
    },
    [commit, enqueue, patchTags],
  )

  const applyBulk = useCallback(
    async (key: string, expectedCount: number): Promise<BulkOutcome> =>
      enqueue(async (): Promise<BulkOutcome> => {
        try {
          const applied = await applySuggestion({
            key,
            expectedCount,
            states,
          })
          // The batch moved a lot of contacts out of the queue; the buffer, the
          // trail, and the cursor all describe a queue that no longer exists.
          await load()
          commit((state) => ({
            ...state,
            taggedSinceUndoable: false,
            undoable: [
              {
                seq: (sequence.current += 1),
                kind: 'decision' as const,
                label: `met — ${applied.applied} contacts in one batch`,
              },
              ...state.undoable,
            ],
          }))
          return { kind: 'applied', applied: applied.applied }
        } catch (error) {
          if (error instanceof TriageError && error.status === 409) {
            return { kind: 'count-changed', detail: error.detail }
          }
          return { kind: 'failed', detail: messageOf(error, 'the suggestion was not applied') }
        }
      }),
    [commit, enqueue, load, states],
  )

  const reload = useCallback(() => {
    void enqueue(load)
  }, [enqueue, load])

  const state = stateRef.current
  // The rows past everything already in hand. `frontierRef` is the furthest
  // contact the server has handed over, so anything at or below it is either a
  // card in the buffer or one already passed, and both are drawn from state
  // that is more current than this page.
  const frontier = frontierRef.current
  const held = new Set(state.cards.map((card) => card.contact.id))
  const waiting = state.ahead.filter(
    (row) => (frontier === null || row.id > frontier) && !held.has(row.id),
  )
  // A page is only asked for again when the tail it left has nearly run out
  // *and* that page was full, so a queue whose whole tail is in hand never
  // asks twice and a long run asks about once every ninety contacts.
  const wantsMore = state.status === 'ready' && state.aheadMore && waiting.length < AHEAD_MIN
  useEffect(() => {
    if (wantsMore) refreshAhead()
  }, [wantsMore, refreshAhead])

  return {
    ...state,
    current: onScreen(state),
    reviewing: state.reviewIndex === null ? null : (state.passed[state.reviewIndex] ?? null),
    liveCard: state.cards[0] ?? null,
    inHand: state.cards.slice(1),
    waiting,
    trailOffset: state.seen - state.passed.length,
    refilling: state.status === 'ready' && state.cards.length === 0 && !state.exhausted,
    peek,
    decide,
    back,
    forward,
    goTo,
    resume,
    jumpTo,
    notify,
    undo,
    dismissConflict: useCallback(
      () => commit((previous) => ({ ...previous, undoConflict: null })),
      [commit],
    ),
    dismissError: useCallback(
      () => commit((previous) => ({ ...previous, actionErrors: [], restored: null })),
      [commit],
    ),
    rename,
    review,
    addTag,
    removeTag,
    applyBulk,
    reload,
  }
}
