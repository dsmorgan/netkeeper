/**
 * The queue, visible: what you have passed, where you are, and who is next.
 *
 * The CP2 walkthrough stopped on "it's not totally clear how you navigate — is
 * the list of un-triaged contacts?" (issue #114). It is, and this says so: the
 * contacts this run has already put on screen, each with the decision it
 * carries, then the contact on screen, then the ones in hand, then the rest of
 * the queue in the order it will be served.
 *
 * The tail is real rows, not a guess. `POST /contacts/query` compiles to the
 * same `WHERE` and the same `ORDER BY` as the triage queue's own statement, so
 * the list *is* the queue rather than an approximation of it; `api.ts`
 * documents the match line by line.
 *
 * **Focus.** Every row that can be pressed stays a button whatever the cursor
 * is doing, so using one never destroys the element the click was on and drops
 * focus to the document. The rows that cannot be pressed are never buttons, so
 * they are never focused to begin with: a contact further down the queue cannot
 * be opened, because reaching them means passing everybody in between, and that
 * is a decision about thirty people rather than a navigation.
 *
 * That leaves the one case the rows cannot hold on their own, which is issue
 * #137: `u` takes back the newest decision, the contact goes back to the front
 * of the queue, and their row leaves the trail — under the cursor, if that is
 * where it was. The browser then drops focus to `document.body` and a keyboard
 * or screen-reader user has lost their place entirely (WCAG 2.4.3).
 *
 * So focus is handed to **the row that took the same place in the list**, of
 * the three candidates the issue weighed. It keeps the person where they were
 * reading: the list has not gone anywhere, and one row out of it is not a
 * reason to move them to the card or to a control they were not using. Where
 * the row that went was the last one, the same index is the live card's row,
 * which is the nearest thing to "where you were" the list can offer.
 *
 * The handover is deliberately narrow. It fires only when the remembered
 * element has actually left the document *and* nothing else has taken focus,
 * so a person who moved focus away themselves is never pulled back: moving to
 * another element clears the memory through `focusout`, and clicking the page
 * background leaves the row connected, which fails the other half.
 */

import { useLayoutEffect, useRef } from 'react'

import { Badge } from '@/components/ui/badge'

import { passedStateLabel, type PassedCard } from './use-triage-queue'
import type { QueuedContact, TriageCard } from './api'

function nameOf(card: TriageCard): string {
  const { preferred_name, last_name } = card.contact
  return `${preferred_name} ${last_name}`.trim()
}

/** A decision that stands, a contact skipped over, or a write that did not land. */
function toneOf(entry: PassedCard): 'secondary' | 'outline' | 'destructive' {
  if (entry.failed) return 'destructive'
  return entry.decision === null ? 'outline' : 'secondary'
}

const ROW = 'flex w-full min-w-0 items-center justify-between gap-2 rounded-md px-2 py-1 text-left'
const PRESSABLE =
  `${ROW} hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none ` +
  'aria-[current]:bg-muted aria-[current]:font-medium aria-[current]:ring-1 aria-[current]:ring-foreground/15'

export function QueueList({
  passed,
  reviewIndex,
  trailOffset,
  seen,
  liveCard,
  inHand,
  waiting,
  notShown,
  error,
  filterLabel,
  onOpen,
  onResume,
}: {
  passed: readonly PassedCard[]
  reviewIndex: number | null
  /** The run position of `passed[0]`, so a row can name its place past the cap. */
  trailOffset: number
  seen: number
  liveCard: TriageCard | null
  inHand: readonly TriageCard[]
  waiting: readonly QueuedContact[]
  /**
   * Contacts in the queue that no row below reaches: the page the look-ahead
   * took, subtracted from the count that answer carried. `null` until it lands.
   */
  notShown: number | null
  error: string | null
  filterLabel: string
  onOpen: (index: number) => void
  onResume: () => void
}) {
  const reviewing = reviewIndex !== null
  const rows = useRef<HTMLOListElement>(null)
  const section = useRef<HTMLElement>(null)
  /** The row focus is on, so a row removed under it can hand focus on (#137). */
  const standingOn = useRef<{ node: HTMLElement; index: number } | null>(null)

  function pressables(): HTMLButtonElement[] {
    return [...(rows.current?.querySelectorAll<HTMLButtonElement>('button') ?? [])]
  }

  useLayoutEffect(() => {
    const was = standingOn.current
    // Still there, or focus was never in here: nothing to hand on.
    if (was === null || was.node.isConnected) return
    standingOn.current = null
    // Something else took focus in the meantime, and it is not this component's
    // to take back.
    if (document.activeElement !== document.body) return
    const now = pressables()
    const heir = now[Math.min(was.index, now.length - 1)]
    // No row left at all — the whole trail went. The section itself is the
    // nearest place that still says where the person is.
    if (heir === undefined) section.current?.focus()
    else heir.focus()
  })

  return (
    <section
      ref={section}
      tabIndex={-1}
      aria-label="Queue"
      data-testid="triage-queue-list"
      // Focusable only as the last resort in the effect above, when the whole
      // trail went and there is no row left to hand focus to — so it needs a
      // visible ring of its own, or a sighted keyboard user is told nothing
      // about where they just landed.
      className="flex min-w-0 flex-col gap-2 rounded-xl bg-card p-3 ring-1 ring-foreground/10 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
    >
      <div>
        <h3 className="font-medium">Queue · {filterLabel}</h3>
        <p className="text-xs text-muted-foreground">
          {seen === 0
            ? 'Worked through in order, oldest connection first.'
            : `${seen} seen so far in this run.`}
        </p>
      </div>

      <ol
        ref={rows}
        className="flex max-h-96 flex-col gap-0.5 overflow-y-auto text-sm"
        onFocus={(event) => {
          const node = event.target as HTMLElement
          const index = pressables().indexOf(node as HTMLButtonElement)
          standingOn.current = index === -1 ? null : { node, index }
        }}
        onBlur={(event) => {
          // A `relatedTarget` means the person moved focus themselves, so there
          // is nothing to restore. A removed element leaves it null, which is
          // the case worth remembering.
          if (event.relatedTarget !== null) standingOn.current = null
        }}
      >
        {passed.map((entry, index) => (
          <li key={`${entry.card.contact.id}-${trailOffset + index}`}>
            <button
              type="button"
              // Spelled out rather than left to the name computation, so a
              // reader hears "Ada Example-1 — Met" instead of the two runs of
              // text the row happens to be built from.
              aria-label={`${nameOf(entry.card)} — ${passedStateLabel(entry)}`}
              aria-current={reviewIndex === index ? 'true' : undefined}
              onClick={() => onOpen(index)}
              className={PRESSABLE}
            >
              <span className="min-w-0 truncate">{nameOf(entry.card)}</span>
              <Badge variant={toneOf(entry)}>{passedStateLabel(entry)}</Badge>
            </button>
          </li>
        ))}

        {liveCard !== null && (
          <li>
            <button
              type="button"
              aria-label={`${nameOf(liveCard)} — ${reviewing ? 'Where you were' : 'On screen'}`}
              aria-current={reviewing ? undefined : 'true'}
              onClick={onResume}
              className={PRESSABLE}
            >
              <span className="min-w-0 truncate">{nameOf(liveCard)}</span>
              <Badge variant={reviewing ? 'outline' : 'default'}>
                {reviewing ? 'Where you were' : 'On screen'}
              </Badge>
            </button>
          </li>
        )}

        {inHand.map((card) => (
          <li key={card.contact.id} className={`${ROW} text-muted-foreground`}>
            <span className="min-w-0 truncate">{nameOf(card)}</span>
            <Badge variant="outline">Next</Badge>
          </li>
        ))}

        {waiting.map((row) => (
          <li key={row.id} className={`${ROW} text-muted-foreground`}>
            <span className="min-w-0 truncate">{row.name}</span>
          </li>
        ))}
      </ol>

      {error !== null && (
        <p className="text-xs text-destructive">
          The contacts ahead could not be read ({error}), so this shows only what is in hand.
        </p>
      )}
      {error === null && notShown !== null && (
        <p className="text-xs text-muted-foreground">
          {notShown === 0 ? 'That is the end of this queue.' : `…and ${notShown} more after these.`}
        </p>
      )}
    </section>
  )
}
