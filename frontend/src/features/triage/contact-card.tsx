/**
 * The contact under triage: who they are, in the order you decide by.
 *
 * The first line is always the position — where this contact sits in the run
 * and how much of the queue is left — because the CP2 walkthrough could not
 * tell (issue #114). The second, when it is there, says the card is one you
 * stepped *back* to and what decision it already carries, so "back" can never
 * be read as "undone": going back writes nothing, and the line says so.
 *
 * The card is a polite, atomic live region. Moving to the next contact replaces
 * its contents without moving focus — focus stays wherever the person put it, so
 * the keyboard map keeps working — and a screen reader reads the new person out
 * because the region changed. The evidence panel is deliberately *outside* the
 * region: it is long, it is for reading rather than for hearing announced, and
 * it is its own landmark.
 *
 * That atomicity is why the "looking back" line carries `aria-hidden`. `←`
 * changes the card *and* writes the screen's other live line, and an atomic
 * region re-reads all of itself, so a reader heard the whole card and then a
 * paraphrase of one of its sentences. The sentence has one owner now — the
 * notice line, which is where "what just happened" belongs and is on screen as
 * text either way. The card is left saying who this is; the notice says what
 * pressing `←` did.
 *
 * Nothing in here is a link that steals a key, and nothing is focusable except
 * the profile link, which sits last.
 *
 * The bordered box around this is `triage-page.tsx`'s, shared with `CardSteps`
 * so the two read as one card — and so the decision row is not a second box's
 * worth of padding further down the page.
 *
 * **What is not here: the name, the tags, and the decision.** They are
 * `CardSteps`, drawn immediately under this, in the order the work is done in
 * (#142). Keeping them out of this region is what lets them hold editors: an
 * atomic live region re-reads all of itself whenever it changes, so a text
 * field inside one would announce the whole card on every keystroke. The
 * heading still carries the preferred name, because that is who the card is
 * about; what you call them, and whether that differs from their given name, is
 * step 1's to say.
 */

import { ExternalLink } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { NeedsReviewBadge } from '@/features/contacts/needs-review'

import { formatDay } from './format'
import { passedStateLabel, type PassedCard } from './use-triage-queue'
import type { TriageCard as Card } from './api'

const MET_LABELS: Record<string, string> = {
  unknown: 'Untriaged',
  met: 'Met',
  not_met: 'Not met',
  skip: 'Skipped',
}

export function ContactCard({
  card,
  position,
  review,
}: {
  card: Card
  position: string
  /** The trail entry this card came from, when `←` walked back to it. */
  review: PassedCard | null
}) {
  const { contact } = card
  const name = `${contact.preferred_name} ${contact.last_name}`.trim()
  const job = [contact.current_title, contact.current_company].filter(Boolean).join(' at ')

  return (
    <section
      aria-label="Contact under triage"
      aria-live="polite"
      aria-atomic="true"
      data-testid="triage-card"
      className="flex min-w-0 flex-col gap-3"
    >
      <p data-testid="card-position" className="text-xs text-muted-foreground">
        {position}
      </p>

      {review !== null && (
        <p
          data-testid="card-review"
          aria-hidden="true"
          className="rounded-md bg-muted px-2 py-1.5 text-xs ring-1 ring-foreground/10"
        >
          <span className="font-medium">Looking back.</span> {passedStateLabel(review)}. Coming here
          wrote nothing — deciding again replaces it, and{' '}
          <kbd className="rounded border px-1 font-mono">u</kbd> is the one that takes a write back.
        </p>
      )}
      <div className="min-w-0">
        <h2 className="font-heading text-xl leading-tight font-medium break-words">
          {/* The contact page, in a new tab so the run keeps its place: it has
              the Met control and everything else the card does not (#322). */}
          <a
            href={`/contacts/${contact.id}`}
            target="_blank"
            rel="noreferrer noopener"
            title="Open this contact's page in a new tab"
            className="hover:underline focus-visible:underline"
          >
            {name}
          </a>
        </h2>
        {/* Two lines, always, however long the headline is. This is the one
            thing on the card whose height varies with the contact, and the
            decision row sits under it: without a reserved height a wrapped
            headline pushed the buttons 40px down on that card alone, so the
            mouse had to re-aim on every contact and a long one could put them
            below the fold. Clamping is visual only — the whole headline is
            still in the DOM, so a screen reader reads all of it. */}
        <p className="mt-1 line-clamp-2 min-h-[2lh] break-words text-muted-foreground">
          {contact.headline}
        </p>
      </div>

      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-muted-foreground">Job</dt>
        <dd className="min-w-0 break-words">{job === '' ? '—' : job}</dd>
        <dt className="text-muted-foreground">Location</dt>
        <dd className="min-w-0 break-words">{contact.location ?? '—'}</dd>
        <dt className="text-muted-foreground">Connected</dt>
        <dd>{formatDay(contact.connected_on)}</dd>
        <dt className="text-muted-foreground">State</dt>
        {/* The badge sits on the State line rather than on a line of its own:
            it is on some cards and not others, and a line that comes and goes
            would move the decision row under it (spec 10.2). Confirming or
            rejecting leads the column beside the card, where it moves nothing. */}
        <dd className="flex flex-wrap items-center gap-2">
          {MET_LABELS[contact.met] ?? contact.met}
          {contact.needs_review_at !== null && <NeedsReviewBadge />}
        </dd>
      </dl>

      {contact.do_not_contact && (
        <div>
          <Badge variant="destructive">Do not contact</Badge>
        </div>
      )}

      {contact.li_url !== null && (
        <a
          className="inline-flex w-fit items-center gap-1 text-sm text-primary underline-offset-4 hover:underline focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
          href={contact.li_url}
          target="_blank"
          rel="noreferrer noopener"
        >
          Open the profile
          <ExternalLink aria-hidden="true" className="size-3.5" />
        </a>
      )}
    </section>
  )
}
