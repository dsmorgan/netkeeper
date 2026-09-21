/**
 * `/triage` — spec 10.2, the Step 6 workflow.
 *
 * One contact at a time with its evidence, the keyboard map, the progress
 * counters, the queue filter, and the bulk suggestion banner.
 *
 * The screen is built to a budget: fifty contacts in ten minutes with the
 * keyboard alone, which is twelve seconds each. That rules out a few designs
 * that would otherwise be reasonable — nothing here traps focus, nothing is
 * modal, no keystroke waits for a round trip (`useTriageQueue` keeps the next
 * card in hand), and every key in the map works from a cold page with focus
 * nowhere in particular.
 */

import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'

import { ContactCard } from './contact-card'
import { EvidencePanel } from './evidence-panel'
import { KeyboardHelp } from './keyboard-help'
import { PreferredNameEditor } from './preferred-name-editor'
import { SPEC_BINDINGS } from './keymap'
import { SuggestionBanner } from './suggestion-banner'
import { TagPicker } from './tag-picker'
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
 * card moves, with no effect and nothing to forget. The help overlay is about
 * the screen rather than a contact, so it carries `null` and stays up.
 */
interface OpenOverlay {
  kind: Overlay
  contactId: number | null
}

const CLOSED: OpenOverlay = { kind: 'none', contactId: null }

const FILTERS: ReadonlyArray<{ value: QueueFilter; label: string }> = [
  { value: 'unknown', label: 'Untriaged' },
  { value: 'skip', label: 'Skipped' },
  { value: 'both', label: 'Both' },
]

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
  // once the render it belongs to is on screen.
  const queueRef = useRef(queue)
  useEffect(() => {
    queueRef.current = queue
  })

  const onAction = useCallback(
    (action: TriageAction) => {
      const current = queueRef.current
      switch (action) {
        case 'met':
          current.decide('met')
          return
        case 'not-met':
          current.decide('not_met')
          return
        case 'skip':
          current.decide('skip')
          return
        case 'undo':
          void current.undo().then(previewAgain)
          return
        case 'next':
          current.skipAhead()
          return
        case 'tag': {
          const open = current.current
          if (open !== null) setOverlay({ kind: 'tags', contactId: open.contact.id })
          return
        }
        case 'preferred-name': {
          const open = current.current
          if (open !== null) setOverlay({ kind: 'name', contactId: open.contact.id })
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
  const triaged = (progress?.triaged ?? 0) + queue.pendingDecisions
  const total = progress?.total ?? 0
  const remaining = Math.max((progress?.remaining ?? 0) - queue.pendingDecisions, 0)
  const skipped = progress?.by_state.skip ?? 0
  const nextUndo = queue.undoable[0]

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

        <Button size="sm" variant="ghost" onClick={() => onAction('help')}>
          Keyboard (?)
        </Button>
      </div>

      <SuggestionBanner
        filter={filter}
        onApply={(key, expectedCount) => queue.applyBulk(key, expectedCount)}
      />

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

      {queue.status === 'ready' && card === null && queue.refilling && (
        <p role="status">Loading the next contact…</p>
      )}

      {queue.status === 'ready' && card === null && !queue.refilling && (
        <EmptyQueue
          filter={filter}
          skipped={skipped}
          onRevisitSkipped={() => setFilter('skip')}
          canUndo={queue.undoable.length > 0}
          onUndo={() => void queue.undo().then(previewAgain)}
        />
      )}

      {card !== null && (
        <>
          <div className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
            <ContactCard
              card={card}
              position={`${Math.min(triaged + 1, Math.max(total, 1))} of ${total} · ${remaining} left`}
            />
            <EvidencePanel card={card} />
          </div>

          {open === 'name' && (
            <PreferredNameEditor
              key={card.contact.id}
              contactId={card.contact.id}
              initial={card.contact.preferred_name}
              firstName={card.contact.first_name}
              onSave={queue.rename}
              onClose={() => setOverlay(CLOSED)}
            />
          )}

          {open === 'tags' && (
            <TagPicker
              key={card.contact.id}
              applied={card.contact.tags}
              onAdd={(tag) => void queue.addTag(card.contact.id, tag)}
              onRemove={(tagId) => void queue.removeTag(card.contact.id, tagId)}
              onClose={() => setOverlay(CLOSED)}
            />
          )}
        </>
      )}

      <div className="flex flex-col gap-1 text-sm text-muted-foreground">
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1">
          {SPEC_BINDINGS.map((binding) => (
            <span key={binding.key} className="inline-flex items-center gap-1">
              <kbd className="rounded border px-1.5 py-0.5 font-mono text-xs">{binding.label}</kbd>
              {binding.description}
            </span>
          ))}
          <span className="inline-flex items-center gap-1">
            <kbd className="rounded border px-1.5 py-0.5 font-mono text-xs">?</kbd>
            all keys
          </span>
        </p>
        <p data-testid="undo-affordance">
          {nextUndo === undefined
            ? 'u takes back the newest triage decision.'
            : `u takes back: ${nextUndo.label}.`}
          {queue.taggedSinceUndoable &&
            ' Tagging is not on the triage undo stack, so u reaches past the tag you just set.'}
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
  onRevisitSkipped,
  canUndo,
  onUndo,
}: {
  filter: QueueFilter
  skipped: number
  onRevisitSkipped: () => void
  canUndo: boolean
  onUndo: () => void
}) {
  return (
    <div
      role="status"
      className="flex flex-col items-start gap-2 rounded-xl bg-card p-6 ring-1 ring-foreground/10"
    >
      <p className="font-heading text-lg font-medium">
        {filter === 'skip' ? 'Nothing skipped is left.' : 'Nothing left to triage.'}
      </p>
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
        {canUndo && (
          <Button size="sm" variant="outline" onClick={onUndo}>
            Undo the last decision
          </Button>
        )}
      </div>
    </div>
  )
}
