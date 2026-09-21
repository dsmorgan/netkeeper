/**
 * The queue, visible: what you have passed, where you are, and what is next.
 *
 * The CP2 walkthrough stopped on "it's not totally clear how you navigate — is
 * the list of un-triaged contacts?" (issue #114). It is, and this says so: the
 * contacts this run has already put on screen, each with the decision it
 * carries, then the contact on screen, then the ones already in hand.
 *
 * The list claims only what the screen actually knows. The trail is local, and
 * the contacts ahead are the ones the prefetch has handed over — there is no
 * endpoint that lists the queue, so the tail is a count rather than a row per
 * person. Making one up out of `remaining` would be a guess at an order the
 * server never told us.
 *
 * Opening a contact from the trail is navigation: it moves the cursor and
 * writes nothing. Nothing here takes focus on its own and nothing is modal.
 */

import { Badge } from '@/components/ui/badge'

import { passedStateLabel, type PassedCard } from './use-triage-queue'
import type { TriageCard } from './api'

function nameOf(card: TriageCard): string {
  const { preferred_name, last_name } = card.contact
  return `${preferred_name} ${last_name}`.trim()
}

/** A decision that stands, a contact skipped over, or a write that did not land. */
function toneOf(entry: PassedCard): 'secondary' | 'outline' | 'destructive' {
  if (entry.failed) return 'destructive'
  return entry.decision === null ? 'outline' : 'secondary'
}

export function QueueList({
  passed,
  reviewIndex,
  liveCard,
  ahead,
  remaining,
  exhausted,
  filterLabel,
  onOpen,
  onResume,
  onNext,
}: {
  passed: readonly PassedCard[]
  reviewIndex: number | null
  liveCard: TriageCard | null
  ahead: readonly TriageCard[]
  remaining: number
  exhausted: boolean
  filterLabel: string
  onOpen: (index: number) => void
  onResume: () => void
  onNext: () => void
}) {
  const reviewing = reviewIndex !== null
  return (
    <section
      aria-label="Queue"
      data-testid="triage-queue-list"
      className="flex min-w-0 flex-col gap-2 rounded-xl bg-card p-3 ring-1 ring-foreground/10"
    >
      <div>
        <h3 className="font-medium">Queue · {filterLabel}</h3>
        <p className="text-xs text-muted-foreground">
          {passed.length === 0
            ? `${remaining} in this queue. It is worked through in order.`
            : `${passed.length} seen so far in this run · ${remaining} left in this queue.`}
        </p>
      </div>

      <ol className="flex max-h-96 flex-col gap-0.5 overflow-y-auto text-sm">
        {passed.map((entry, index) => (
          <li key={`${entry.card.contact.id}-${index}`}>
            <button
              type="button"
              // Spelled out rather than left to the name computation, so a
              // reader hears "Ada Example-1 — Met" instead of the two runs of
              // text the row happens to be built from.
              aria-label={`${nameOf(entry.card)} — ${passedStateLabel(entry)}`}
              aria-current={reviewIndex === index ? 'true' : undefined}
              onClick={() => onOpen(index)}
              className="flex w-full min-w-0 items-center justify-between gap-2 rounded-md px-2 py-1 text-left hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none aria-[current]:bg-muted aria-[current]:ring-1 aria-[current]:ring-foreground/15"
            >
              <span className="min-w-0 truncate">{nameOf(entry.card)}</span>
              <Badge variant={toneOf(entry)}>{passedStateLabel(entry)}</Badge>
            </button>
          </li>
        ))}

        {liveCard !== null &&
          (reviewing ? (
            <li>
              <button
                type="button"
                aria-label={`${nameOf(liveCard)} — Where you were`}
                onClick={onResume}
                className="flex w-full min-w-0 items-center justify-between gap-2 rounded-md px-2 py-1 text-left hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
              >
                <span className="min-w-0 truncate">{nameOf(liveCard)}</span>
                <Badge variant="outline">Where you were</Badge>
              </button>
            </li>
          ) : (
            <li
              aria-current="true"
              className="flex min-w-0 items-center justify-between gap-2 rounded-md bg-muted px-2 py-1 ring-1 ring-foreground/15"
            >
              <span className="min-w-0 truncate font-medium">{nameOf(liveCard)}</span>
              <Badge variant="default">On screen</Badge>
            </li>
          ))}

        {ahead.map((card, index) => (
          <li key={card.contact.id}>
            {/* Only from the live card: while the cursor is back in the trail,
                "next" means stepping forward through it, and a row labelled
                Next that did something else would be a small lie. */}
            {index === 0 && !reviewing ? (
              <button
                type="button"
                aria-label={`${nameOf(card)} — Next`}
                onClick={onNext}
                className="flex w-full min-w-0 items-center justify-between gap-2 rounded-md px-2 py-1 text-left hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
              >
                <span className="min-w-0 truncate">{nameOf(card)}</span>
                <Badge variant="outline">Next</Badge>
              </button>
            ) : (
              <span className="flex min-w-0 items-center justify-between gap-2 px-2 py-1">
                <span className="min-w-0 truncate">{nameOf(card)}</span>
                <Badge variant="outline">In hand</Badge>
              </span>
            )}
          </li>
        ))}
      </ol>

      <p className="text-xs text-muted-foreground">
        {exhausted
          ? 'That is the end of this queue.'
          : `More after these — ${remaining} left in this queue altogether.`}
      </p>
    </section>
  )
}
