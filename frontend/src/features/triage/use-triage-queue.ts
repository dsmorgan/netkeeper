/**
 * The triage queue: two cards in hand, one request per contact, exact undo.
 *
 * The budget is twelve seconds a contact (P1-14), so the rule this hook exists
 * to keep is that a keypress never waits for the network. It holds a small
 * buffer of cards. A decision pops the head and renders the next one
 * immediately; the `POST` goes out behind it and its answer — which carries the
 * card after the one now on screen — refills the buffer. The steady state is one
 * request per contact and no round trip between two cards.
 *
 * Three details make that safe rather than merely fast:
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
 *   silent drop.
 *
 * Nothing here is optimistic about what the server stores: the progress counters
 * come from the answers. Only the *screen position* runs ahead, and
 * `pendingDecisions` says how far, so the counter reads true mid-run.
 */

import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react'

import {
  LIVENESS_CONFLICTS,
  TriageError,
  applySuggestion,
  conflictFieldOf,
  decide as decideRequest,
  fetchQueue,
  setPreferredName,
  statesFor,
  tagContact,
  undo as undoRequest,
  untagContact,
  type ContactMet,
  type QueueFilter,
  type TriageCard,
  type TriageProgress,
  type TriageTag,
} from './api'

export interface TriageQueueState {
  status: 'loading' | 'ready' | 'error'
  /** The buffer. `cards[0]` is on screen; the rest are prefetched. */
  cards: TriageCard[]
  progress: TriageProgress | null
  /** Decisions sent but not yet answered, so the progress counter can read true. */
  pendingDecisions: number
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
   * The contact field that `409` named, which says what kind of refusal it was.
   *
   * `archived_at` or `merged_into_id` mean the contact has left the queue, and
   * forcing restores it without putting it back; anything else is an ordinary
   * edit in between, and forcing leaves the contact exactly where it was.
   */
  undoConflictField: string | null
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
  progress: null,
  pendingDecisions: 0,
  exhausted: false,
  loadError: null,
  actionErrors: [],
  undoConflict: null,
  undoConflictField: null,
  undoable: [],
  taggedSinceUndoable: false,
  restored: null,
}

/** What a bulk apply did, so the banner can re-preview instead of reporting a failure. */
export type BulkOutcome =
  | { kind: 'applied'; applied: number }
  | { kind: 'count-changed'; detail: string }
  | { kind: 'failed'; detail: string }

export interface TriageQueue extends TriageQueueState {
  /** The contact on screen, or `null` when the queue is empty or refilling. */
  readonly current: TriageCard | null
  /** True while the buffer is empty but more is coming. */
  readonly refilling: boolean
  decide: (met: ContactMet) => void
  /** The `→` key: move on without deciding. Writes nothing. */
  skipAhead: () => void
  /** Resolves once the undo has landed, so a caller can re-run what it invalidates. */
  undo: (options?: { force?: boolean }) => Promise<void>
  dismissConflict: () => void
  dismissError: () => void
  rename: (contactId: number, preferredName: string) => Promise<void>
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
  const states = useMemo(() => statesFor(filter), [filter])

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

  const load = useCallback(async () => {
    commit((state) => ({
      ...INITIAL,
      progress: state.progress,
      undoable: state.undoable,
      taggedSinceUndoable: state.taggedSinceUndoable,
    }))
    frontierRef.current = null
    try {
      const queue = await fetchQueue({ states, prefetch: true })
      const cards = [queue.card, queue.next].filter((card): card is TriageCard => card !== null)
      frontierRef.current = cards.at(-1)?.contact.id ?? null
      commit((state) => ({
        ...state,
        status: 'ready',
        cards,
        progress: queue.progress,
        exhausted: queue.next === null,
      }))
    } catch (error) {
      commit((state) => ({
        ...state,
        status: 'error',
        loadError: messageOf(error, 'the triage queue could not be read'),
      }))
    }
  }, [commit, states])

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  // Re-runs when the filter changes, because `load` closes over it.
  useEffect(() => {
    void enqueue(load)
  }, [enqueue, load])

  /** Fold a refill answer into the buffer and move the frontier with it. */
  const absorb = useCallback(
    (card: TriageCard | null, progress: TriageProgress, decided: boolean) => {
      if (card !== null) frontierRef.current = card.contact.id
      commit((state) => ({
        ...state,
        progress,
        pendingDecisions: decided ? state.pendingDecisions - 1 : state.pendingDecisions,
        cards: card === null ? state.cards : [...state.cards, card],
        exhausted: card === null,
      }))
    },
    [commit],
  )

  const decide = useCallback(
    (met: ContactMet) => {
      const card = stateRef.current.cards[0]
      if (card === undefined) return
      const decisionLabel = `${MET_LABELS[met]} — ${nameOf(card)}`
      const seq = (sequence.current += 1)
      commit((state) => ({
        ...state,
        cards: state.cards.slice(1),
        pendingDecisions: state.pendingDecisions + 1,
        // `actionErrors` is deliberately untouched: a write that did not land
        // must not be erased by the next keystroke.
        undoConflict: null,
        undoConflictField: null,
        restored: null,
        taggedSinceUndoable: false,
        undoable: [{ seq, kind: 'decision', label: decisionLabel }, ...state.undoable],
      }))
      void enqueue(async () => {
        try {
          const result = await decideRequest({
            contactId: card.contact.id,
            met,
            prefetchAfterId: frontierRef.current,
            states,
          })
          absorb(result.next, result.progress, true)
        } catch (error) {
          // Nothing was written. Retracting the entry keeps `u` from offering to
          // take it back, and the note keeps an undo that was already pressed
          // underneath this decision from reaching past it to the one before.
          retracted.current.set(seq, nameOf(card))
          commit((state) =>
            withFailure(
              {
                ...state,
                pendingDecisions: state.pendingDecisions - 1,
                undoable: state.undoable.filter((action) => action.seq !== seq),
              },
              `${nameOf(card)} was not recorded: ${messageOf(error, 'the request failed')}`,
            ),
          )
        }
      })
    },
    [absorb, commit, enqueue, states],
  )

  const skipAhead = useCallback(() => {
    if (stateRef.current.cards[0] === undefined) return
    commit((state) => ({ ...state, cards: state.cards.slice(1), undoConflict: null }))
    // The frontier is the end of the queue, so there is nothing to ask for.
    if (stateRef.current.exhausted) return
    void enqueue(async () => {
      try {
        const queue = await fetchQueue({
          states,
          afterId: frontierRef.current,
          prefetch: false,
        })
        absorb(queue.card, queue.progress, false)
      } catch (error) {
        commit((state) =>
          withFailure(state, messageOf(error, 'the next contact could not be read')),
        )
      }
    })
  }, [absorb, commit, enqueue, states])

  const undo = useCallback(
    (options?: { force?: boolean }): Promise<void> => {
      // Both of these are read when the key is pressed, not when the request
      // goes out, because they are what the person meant by pressing it.
      const meant = stateRef.current.undoable[0]?.seq
      const force = options?.force ?? false
      // A force answers a refusal the screen is still showing, and the field it
      // named says whether the contact is merely edited or gone from the queue.
      const overLiveness =
        force &&
        stateRef.current.undoConflictField !== null &&
        LIVENESS_CONFLICTS.has(stateRef.current.undoConflictField)
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
          const result = await undoRequest({ force, states })
          if (result.card === null) {
            // A bulk batch put many contacts back: the buffer no longer
            // describes the queue, so take it again.
            await load()
            commit((state) => ({
              ...state,
              undoable: state.undoable.slice(1),
              taggedSinceUndoable: false,
              restored: `Put ${result.decisions} contacts back the way they were.`,
            }))
            return
          }
          const restored = result.card
          // The card comes off the restored contact whatever state that left it
          // in, so it is not always one this queue would serve — and the card
          // shows neither way that happens. Undoing a preferred-name edit on
          // somebody already marked met hands back a `met` contact; a contact
          // archived or merged away is restored without returning to the queue.
          //
          // `result.forced` alone does not tell them apart. The service appends
          // to it for *any* divergence it overrode, an edited field included,
          // and that is the common case: an archive or a merge needs another
          // actor. So liveness is read from the refusal this force answered,
          // and `forced` only narrows it to the contacts actually overridden.
          const leftQueue = overLiveness && result.forced.includes(restored.contact.id)
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
            return {
              ...state,
              status: 'ready',
              cards,
              progress: result.progress,
              undoConflict: null,
              undoConflictField: null,
              undoable: state.undoable.slice(1),
              taggedSinceUndoable: false,
              restored:
                replaces || inQueue
                  ? null
                  : `Put ${restored.contact.preferred_name} ${restored.contact.last_name} back the way they were. ${
                      leftQueue
                        ? 'They were archived or merged away since, so they are not in this queue and the card did not change.'
                        : 'They are not in this queue, so the card did not change.'
                    }`,
            }
          })
        } catch (error) {
          if (error instanceof TriageError && error.status === 409) {
            commit((state) => ({
              ...state,
              undoConflict: error.detail,
              undoConflictField: conflictFieldOf(error.detail),
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
    [commit, enqueue, load, states],
  )

  const rename = useCallback(
    async (contactId: number, preferredName: string) => {
      await enqueue(async () => {
        try {
          const result = await setPreferredName({ contactId, preferredName })
          commit((state) => ({
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
            cards: state.cards.map((card) =>
              card.contact.id === contactId
                ? { ...card, contact: { ...card.contact, preferred_name: result.preferred_name } }
                : card,
            ),
          }))
        } catch (error) {
          commit((state) => withFailure(state, messageOf(error, 'the name was not saved')))
        }
      })
    },
    [commit, enqueue],
  )

  const patchTags = useCallback(
    (contactId: number, update: (tags: TriageTag[]) => TriageTag[]) => {
      commit((state) => ({
        ...state,
        cards: state.cards.map((card) =>
          card.contact.id === contactId
            ? { ...card, contact: { ...card.contact, tags: update(card.contact.tags) } }
            : card,
        ),
      }))
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
          // The batch moved a lot of contacts out of the queue; the buffer and
          // the cursor both describe a queue that no longer exists.
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
  return {
    ...state,
    current: state.cards[0] ?? null,
    refilling: state.status === 'ready' && state.cards.length === 0 && !state.exhausted,
    decide,
    skipAhead,
    undo,
    dismissConflict: useCallback(
      () => commit((previous) => ({ ...previous, undoConflict: null, undoConflictField: null })),
      [commit],
    ),
    dismissError: useCallback(
      () => commit((previous) => ({ ...previous, actionErrors: [], restored: null })),
      [commit],
    ),
    rename,
    addTag,
    removeTag,
    applyBulk,
    reload,
  }
}
