/**
 * `/triage` — spec 10.2, the Step 6 workflow.
 *
 * One contact at a time with its evidence, the keyboard map, the button row
 * that teaches it, the position counters, the visible queue, the progress
 * counters, the queue filter, and the bulk suggestion banner.
 *
 * The screen is built to a budget: fifty contacts in ten minutes with the
 * keyboard alone, which is twelve seconds each. That rules out a few designs
 * that would otherwise be reasonable — nothing here traps focus, nothing is
 * modal, no keystroke waits for a round trip (`useTriageQueue` keeps the next
 * card in hand), and every key in the map works from a cold page with focus
 * nowhere in particular.
 *
 * It is keyboard-*first*, not keyboard-only (P1-23). Every action in the map has
 * a button that shows its key and goes through the same handler, synchronously,
 * so the mouse path costs what the keyboard path costs and neither one waits.
 *
 * **Back and undo are two different things and the screen keeps them apart.**
 * `←` walks the contacts this run has already seen: it is a cursor, it sends no
 * request, and the card says what decision the contact already carries. `u` asks
 * the server to take back the newest *write*. They are named for that, they sit
 * in different groups in the button row, and every move of either kind says what
 * just happened in the line under the buttons.
 */

import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'

import { ActionBar } from './action-bar'
import { ContactCard } from './contact-card'
import { EvidencePanel } from './evidence-panel'
import { KeyboardHelp } from './keyboard-help'
import { PreferredNameEditor } from './preferred-name-editor'
import { QueueList } from './queue-list'
import { SuggestionBanner } from './suggestion-banner'
import { TagPicker } from './tag-picker'
import { bindingFor } from './keymap'
import { useTriageKeys } from './use-triage-keys'
import { useTriageQueue } from './use-triage-queue'
import { SUGGESTIONS_KEY, type QueueFilter } from './api'
import type { TriageAction } from './keymap'

type Overlay = 'none' | 'help' | 'name' | 'tags'

/**
 * Which overlay is open, and for whom.
 *
 * `contactId` is the whole point. The `p` editor and the `t` picker are about
 * one person: they hold that person's name in their own state and send it to
 * that person's id. Leaving one open across a card change — which is easy,
 * because they do not hold focus and `m` still works while they are up — would
 * let a name typed for one contact be submitted against the next. Tying the
 * overlay to the contact it was opened for closes it by derivation when the
 * card moves, with no effect and nothing to forget. That covers stepping back
 * as well as moving on, because both change which contact is on screen. The
 * help overlay is about the screen rather than a contact, so it carries `null`
 * and stays up.
 */
interface OpenOverlay {
  kind: Overlay
  contactId: number | null
}

const CLOSED: OpenOverlay = { kind: 'none', contactId: null }

/** What an empty queue is called, in the words of the queue that emptied. */
const EMPTY_QUEUE: Record<QueueFilter, string> = {
  unknown: 'Nothing left to triage.',
  skip: 'Nothing skipped is left.',
  both: 'Nothing left to triage.',
  automatic: 'Nothing left to review.',
}

const FILTERS: ReadonlyArray<{ value: QueueFilter; label: string }> = [
  { value: 'unknown', label: 'Untriaged' },
  { value: 'skip', label: 'Skipped' },
  { value: 'both', label: 'Both' },
  // The review pass (P1-22): the contacts a batch decided and nobody has
  // looked at since. Answering one by hand is what takes it out of here.
  { value: 'automatic', label: 'Reviewing' },
]

/**
 * What netkeeper decided, and the way into checking it.
 *
 * The first thing after an import should be the work already done rather than
 * the first stranger (P1-22), so this says how much there is and offers the
 * queue that walks it. It draws nothing when no batch has been accepted, which
 * is every session until one is.
 */
function AutomaticPass({
  waiting,
  reviewing,
  onReview,
}: {
  waiting: number
  reviewing: boolean
  onReview: () => void
}) {
  if (reviewing) {
    return (
      <p
        data-testid="automatic-pass"
        className="rounded-lg bg-muted/60 px-3 py-2 text-sm ring-1 ring-foreground/10"
      >
        {waiting === 0 ? (
          <>
            Nothing left to review — every decision netkeeper made has been checked. The other
            queues are where the untriaged are.
          </>
        ) : (
          <>
            These {waiting === 1 ? 'is the one contact' : `are the ${waiting} contacts`} netkeeper
            decided for you, waiting to be checked. Answering one yourself takes it out of this
            queue; <kbd>u</kbd> puts it back.
          </>
        )}
      </p>
    )
  }
  if (waiting === 0) return null
  return (
    <div
      data-testid="automatic-pass"
      className="flex flex-wrap items-center justify-between gap-3 rounded-lg bg-muted/60 px-3 py-2 ring-1 ring-foreground/10"
    >
      <p className="min-w-0 text-sm">
        netkeeper decided {waiting} {waiting === 1 ? 'contact' : 'contacts'} from a batch you
        accepted. None of them has been checked yet.
      </p>
      <Button size="sm" variant="secondary" onClick={onReview}>
        Review {waiting === 1 ? 'it' : 'them'}
      </Button>
    </div>
  )
}

export function TriagePage() {
  const [filter, setFilter] = useState<QueueFilter>('unknown')
  const [overlay, setOverlay] = useState<OpenOverlay>(CLOSED)
  const queue = useTriageQueue(filter)
  const client = useQueryClient()
  /** Anything that may have moved the suggestion's count re-runs its preview. */
  const previewAgain = useCallback(() => {
    void client.invalidateQueries({ queryKey: SUGGESTIONS_KEY })
  }, [client])

  // The key handler reads the queue through a ref so its identity is stable and
  // the window listener is bound once rather than on every render. The ref is
  // written after the commit, which is soon enough: a keystroke can only arrive
  // once the render it belongs to is on screen — and the actions it calls read
  // the queue's own state rather than this snapshot, so even a key pressed
  // inside the same tick as the last one acts on what is really there.
  const queueRef = useRef(queue)
  useEffect(() => {
    queueRef.current = queue
  })

  const onAction = useCallback(
    (action: TriageAction) => {
      const current = queueRef.current
      // A key that cannot act says so rather than vanishing (issue #92): six
      // `m` presses against a slow backend used to decide two and drop four.
      const nothing = () => {
        const key = bindingFor(action)?.label ?? 'that key'
        current.notify(
          `No contact is on screen yet, so ${key} did nothing. Nothing was recorded — try again once the next contact is here.`,
        )
      }
      switch (action) {
        case 'met':
          if (!current.decide('met')) nothing()
          return
        case 'not-met':
          if (!current.decide('not_met')) nothing()
          return
        case 'skip':
          if (!current.decide('skip')) nothing()
          return
        case 'undo':
          void current.undo().then(previewAgain)
          return
        case 'back':
          current.back()
          return
        case 'next':
          if (!current.forward()) nothing()
          return
        case 'tag': {
          const open = current.peek()
          if (open === null) nothing()
          else setOverlay({ kind: 'tags', contactId: open.contact.id })
          return
        }
        case 'preferred-name': {
          const open = current.peek()
          if (open === null) nothing()
          else setOverlay({ kind: 'name', contactId: open.contact.id })
          return
        }
        case 'help':
          setOverlay((open) => (open.kind === 'help' ? CLOSED : { kind: 'help', contactId: null }))
          return
        case 'dismiss':
          setOverlay(CLOSED)
          current.dismissConflict()
          current.dismissError()
      }
    },
    [previewAgain],
  )

  useTriageKeys({ enabled: true, onAction })

  const card = queue.current
  // Derived rather than stored: an overlay opened for a contact closes itself
  // the moment the card moves on, so a name typed for one person can never be
  // submitted against the next.
  const open: Overlay =
    overlay.contactId === null || overlay.contactId === card?.contact.id ? overlay.kind : 'none'
  const progress = queue.progress
  const triaged = (progress?.triaged ?? 0) + queue.pending.triaged
  const total = progress?.total ?? 0
  const remaining = Math.max((progress?.remaining ?? 0) - queue.pending.removed, 0)
  const skipped = progress?.by_state.skip ?? 0
  // How many decisions a batch made that nobody has looked at since. The
  // number the review pass exists for, and the one that says whether to
  // mention it at all.
  const automatic = progress?.automatic ?? 0
  const nextUndo = queue.undoable[0]
  const filterLabel = FILTERS.find((option) => option.value === filter)?.label ?? 'Untriaged'
  // Where this card sits, in the two terms a person asked for: which contact of
  // the run this is, and how much of the queue is still in front of them.
  //
  // The run position comes from `queue.seen`, not from the length of the trail.
  // The trail is capped at a hundred cards; the run is not, and deriving one
  // from the other froze this line at "contact 101" for the last five hundred
  // of a six-hundred-person queue.
  const runPosition =
    queue.reviewIndex === null ? queue.seen + 1 : queue.trailOffset + queue.reviewIndex + 1
  const position =
    queue.reviewIndex === null
      ? `Contact ${runPosition} of this run · ${remaining} left in this queue`
      : `Looking back: contact ${runPosition} of this run · ${remaining} left in this queue`
  const notShown =
    queue.aheadTotal === null ? null : Math.max(queue.aheadTotal - queue.ahead.length, 0)

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div
          role="group"
          aria-label="Queue filter"
          className="flex items-center gap-1 rounded-lg bg-muted/50 p-1"
        >
          {FILTERS.map((option) => (
            <Button
              key={option.value}
              size="sm"
              variant={filter === option.value ? 'secondary' : 'ghost'}
              aria-pressed={filter === option.value}
              onClick={() => setFilter(option.value)}
            >
              {option.label}
            </Button>
          ))}
        </div>

        <p data-testid="triage-progress" className="text-sm">
          <span className="font-medium">
            {triaged} / {total}
          </span>{' '}
          <span className="text-muted-foreground">
            triaged · {remaining} left in this queue
            {skipped > 0 && ` · ${skipped} skipped`}
          </span>
        </p>
      </div>

      <AutomaticPass
        waiting={automatic}
        reviewing={filter === 'automatic'}
        onReview={() => setFilter('automatic')}
      />

      <ActionBar onAction={onAction} />

      <p
        data-testid="triage-notice"
        aria-live="polite"
        className="min-h-5 text-sm text-muted-foreground"
      >
        {queue.notice}
      </p>

      {/* A batch only ever reaches contacts nobody has answered for, so the
          review pass has none to offer: asking for them there is a 422. */}
      {filter !== 'automatic' && (
        <SuggestionBanner
          filter={filter}
          onApply={(key, expectedCount) => queue.applyBulk(key, expectedCount)}
        />
      )}

      {queue.undoConflict !== null && (
        <UndoConflict
          detail={queue.undoConflict}
          onForce={() => void queue.undo({ force: true }).then(previewAgain)}
          onCancel={queue.dismissConflict}
        />
      )}

      {queue.actionErrors.length > 0 && (
        <div
          role="alert"
          className="flex flex-col gap-1 rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {queue.actionErrors.map((failure, index) => (
            <p key={`${index}-${failure}`}>{failure}</p>
          ))}
          <Button size="xs" variant="ghost" className="w-fit" onClick={queue.dismissError}>
            {queue.actionErrors.length === 1 ? 'Dismiss' : 'Dismiss all'}
          </Button>
        </div>
      )}

      {queue.restored !== null && (
        <p role="status" className="rounded-lg bg-muted/60 px-3 py-2 text-sm">
          {queue.restored}{' '}
          <Button size="xs" variant="ghost" onClick={queue.dismissError}>
            Dismiss
          </Button>
        </p>
      )}

      {queue.status === 'loading' && <p role="status">Loading the queue…</p>}

      {queue.status === 'error' && (
        <div role="alert" className="flex flex-wrap items-center gap-3">
          <p>The triage queue could not be read: {queue.loadError}</p>
          <Button size="sm" onClick={queue.reload}>
            Try again
          </Button>
        </div>
      )}

      {queue.status === 'ready' && (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,18rem)]">
          <div className="flex min-w-0 flex-col gap-4">
            {card === null && queue.refilling && <p role="status">Loading the next contact…</p>}

            {card === null && !queue.refilling && (
              <EmptyQueue
                filter={filter}
                skipped={skipped}
                seen={queue.seen}
                onRevisitSkipped={() => setFilter('skip')}
                canUndo={queue.undoable.length > 0}
                onUndo={() => void queue.undo().then(previewAgain)}
                onBack={queue.back}
              />
            )}

            {card !== null && (
              <div className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
                <ContactCard card={card} position={position} review={queue.reviewing} />
                <EvidencePanel card={card} />
              </div>
            )}

            {card !== null && open === 'name' && (
              <PreferredNameEditor
                key={card.contact.id}
                contactId={card.contact.id}
                initial={card.contact.preferred_name}
                firstName={card.contact.first_name}
                onSave={queue.rename}
                onClose={() => setOverlay(CLOSED)}
              />
            )}

            {card !== null && open === 'tags' && (
              <TagPicker
                key={card.contact.id}
                applied={card.contact.tags}
                onAdd={(tag) => void queue.addTag(card.contact.id, tag)}
                onRemove={(tagId) => void queue.removeTag(card.contact.id, tagId)}
                onClose={() => setOverlay(CLOSED)}
              />
            )}
          </div>

          {(queue.seen > 0 || queue.liveCard !== null) && (
            <QueueList
              passed={queue.passed}
              reviewIndex={queue.reviewIndex}
              trailOffset={queue.trailOffset}
              seen={queue.seen}
              liveCard={queue.liveCard}
              inHand={queue.inHand}
              waiting={queue.waiting}
              notShown={notShown}
              error={queue.aheadError}
              filterLabel={filterLabel}
              onOpen={queue.goTo}
              onResume={queue.resume}
            />
          )}
        </div>
      )}

      <div className="flex flex-col gap-1 text-sm text-muted-foreground">
        <p data-testid="undo-affordance">
          {nextUndo === undefined
            ? 'u takes back the newest triage decision. That stack lives on the server, so on a fresh page it can reach one from an earlier session.'
            : `u takes back: ${nextUndo.label}.`}
          {queue.taggedSinceUndoable &&
            ' Tagging is not on the triage undo stack, so u reaches past the tag you just set.'}
        </p>
        <p data-testid="back-affordance">
          ← is not undo. It walks back through the contacts you have already seen, shows what each
          one carries, and writes nothing until you decide.
        </p>
      </div>

      {open === 'help' && <KeyboardHelp onClose={() => setOverlay(CLOSED)} />}
    </div>
  )
}

/**
 * The `409` from undo, as a choice rather than a failure.
 *
 * The backend refuses for three different reasons and words each one, so the
 * screen does too rather than flattening them into "something went wrong". A
 * field edited in between is not this decision's to overwrite; a contact
 * archived or merged away cannot be put back in the queue at all, and forcing
 * it through means something different in each case. Nothing was written
 * either way, and forcing is offered, never taken.
 */
const CONFLICT_WORDING: Record<string, { heading: string; consequence: string }> = {
  merged_into_id: {
    heading: 'This contact was merged into another one. Undo anyway?',
    consequence:
      'The surviving contact carries this decision now. Undoing here changes the merged-away row, not the one you will see again.',
  },
  archived_at: {
    heading: 'This contact was archived since you decided. Undo anyway?',
    consequence:
      'Undoing puts the recorded state back, but an archived contact does not come back into the queue.',
  },
}

const CHANGED_FIELD = {
  heading: 'This contact changed since you decided. Undo anyway?',
  consequence:
    'Undoing anyway puts back what the decision recorded and overwrites whatever arrived since.',
}

/** The backend names the field in its detail: "contact 4 has archived_at='…'". */
function wordingFor(detail: string) {
  const field = /\bhas ([a-z_]+)=/.exec(detail)?.[1]
  return (field === undefined ? undefined : CONFLICT_WORDING[field]) ?? CHANGED_FIELD
}

function UndoConflict({
  detail,
  onForce,
  onCancel,
}: {
  detail: string
  onForce: () => void
  onCancel: () => void
}) {
  const confirm = useRef<HTMLButtonElement>(null)
  const wording = wordingFor(detail)
  return (
    <div
      role="alertdialog"
      aria-modal="false"
      aria-label={wording.heading}
      className="flex flex-col gap-2 rounded-lg bg-muted/60 p-3 ring-1 ring-foreground/15"
    >
      <p className="font-medium">{wording.heading}</p>
      <p className="text-sm text-muted-foreground">
        {detail} Nothing has been written. {wording.consequence}
      </p>
      <div className="flex flex-wrap gap-2">
        <Button ref={confirm} size="sm" autoFocus onClick={onForce} data-testid="undo-force">
          Undo anyway
        </Button>
        <Button size="sm" variant="ghost" onClick={onCancel}>
          Leave it as it is
        </Button>
      </div>
    </div>
  )
}

function EmptyQueue({
  filter,
  skipped,
  seen,
  onRevisitSkipped,
  canUndo,
  onUndo,
  onBack,
}: {
  filter: QueueFilter
  skipped: number
  seen: number
  onRevisitSkipped: () => void
  canUndo: boolean
  onUndo: () => void
  onBack: () => void
}) {
  return (
    <div
      role="status"
      className="flex flex-col items-start gap-2 rounded-xl bg-card p-6 ring-1 ring-foreground/10"
    >
      <p className="font-heading text-lg font-medium">{EMPTY_QUEUE[filter]}</p>
      <p className="text-muted-foreground">
        {filter === 'unknown' && skipped > 0
          ? `${skipped} contacts are waiting under Skipped.`
          : 'Every contact in this queue has an answer.'}
      </p>
      <div className="flex flex-wrap gap-2">
        {filter === 'unknown' && skipped > 0 && (
          <Button size="sm" onClick={onRevisitSkipped}>
            Revisit the skipped
          </Button>
        )}
        {seen > 0 && (
          <Button size="sm" variant="outline" onClick={onBack}>
            Go back over this run
          </Button>
        )}
        {canUndo && (
          <Button size="sm" variant="outline" onClick={onUndo}>
            Undo the last write
          </Button>
        )}
      </div>
    </div>
  )
}
