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
  TriageError,
  applySuggestion,
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
  /** One action failed; the screen keeps working. */
  actionError: string | null
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
  progress: null,
  pendingDecisions: 0,
  exhausted: false,
  loadError: null,
  actionError: null,
  undoConflict: null,
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
        actionError: null,
        undoConflict: null,
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
          commit((state) => ({
            ...state,
            pendingDecisions: state.pendingDecisions - 1,
            // Nothing was written, so `u` must not offer to take it back.
            undoable: state.undoable.filter((action) => action.seq !== seq),
            actionError: `${nameOf(card)} was not recorded: ${messageOf(error, 'the request failed')}`,
          }))
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
        commit((state) => ({
          ...state,
          actionError: messageOf(error, 'the next contact could not be read'),
        }))
      }
    })
  }, [absorb, commit, enqueue, states])

  const undo = useCallback(
    (options?: { force?: boolean }): Promise<void> =>
      enqueue(async () => {
        try {
          const result = await undoRequest({
            force: options?.force ?? false,
            states,
          })
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
          // in, so it is not necessarily one this queue would serve, and the
          // card itself shows neither way that happens:
          //
          // - Undoing a preferred-name edit on somebody already marked met
          //   hands back a `met` contact, which this queue does not hold.
          // - A forced undo may have gone through over an archive or a merge.
          //   The API says a contact it names in `forced` can be restored
          //   without being back in the queue, and `met` alone looks fine.
          //
          // Either one is reported rather than rendered as the next card,
          // because putting somebody in front of you that the queue will never
          // serve again is how a run loses its place.
          const forced = result.forced.includes(restored.contact.id)
          const inQueue = !forced && states.includes(restored.contact.met)
          commit((state) => {
            const head = state.cards[0]
            const replaces =
              !forced && head !== undefined && head.contact.id === restored.contact.id
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
              actionError: null,
              undoable: state.undoable.slice(1),
              taggedSinceUndoable: false,
              restored:
                replaces || inQueue
                  ? null
                  : `Put ${restored.contact.preferred_name} ${restored.contact.last_name} back the way they were. ${
                      forced
                        ? 'A forced undo can restore a contact that is no longer in this queue, so the card did not change.'
                        : 'They are not in this queue, so the card did not change.'
                    }`,
            }
          })
        } catch (error) {
          if (error instanceof TriageError && error.status === 409) {
            commit((state) => ({ ...state, undoConflict: error.detail }))
            return
          }
          if (error instanceof TriageError && error.status === 404) {
            commit((state) => ({ ...state, actionError: 'Nothing left to undo.' }))
            return
          }
          commit((state) => ({
            ...state,
            actionError: messageOf(error, 'the undo failed'),
          }))
        }
      }),
    [commit, enqueue, load, states],
  )

  const rename = useCallback(
    async (contactId: number, preferredName: string) => {
      await enqueue(async () => {
        try {
          const result = await setPreferredName({ contactId, preferredName })
          commit((state) => ({
            ...state,
            actionError: null,
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
          commit((state) => ({
            ...state,
            actionError: messageOf(error, 'the name was not saved'),
          }))
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
          commit((state) => ({
            ...state,
            actionError: messageOf(error, 'the tag was not applied'),
          }))
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
          commit((state) => ({
            ...state,
            actionError: messageOf(error, 'the tag was not removed'),
          }))
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
      () => commit((previous) => ({ ...previous, undoConflict: null })),
      [commit],
    ),
    dismissError: useCallback(
      () => commit((previous) => ({ ...previous, actionError: null, restored: null })),
      [commit],
    ),
    rename,
    addTag,
    removeTag,
    applyBulk,
    reload,
  }
}
