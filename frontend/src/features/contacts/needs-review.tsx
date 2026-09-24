/**
 * A contact netkeeper read off a card on the LinkedIn connections page (#184).
 *
 * When LinkedIn's own data is unavailable, the connections sync reads the page
 * instead, and a card for somebody netkeeper has never seen becomes a contact
 * marked needs review. The card's name and headline are all it has, and nothing
 * confirms the card is who its link says, so the contact waits: never enriched,
 * never put in a campaign, never counted as a connection, until the person
 * confirms it here or a later sync matches it by LinkedIn's own id.
 *
 * The same notice serves the contact page and the triage screen, so the
 * explanation and the two answers read the same wherever the person meets it.
 * Reject archives the contact; contacts are never deleted.
 */

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'

/** The badge a list or a card shows beside a contact waiting for review. */
export function NeedsReviewBadge() {
  return (
    <Badge variant="secondary" data-testid="needs-review-badge">
      Needs review
    </Badge>
  )
}

export function NeedsReviewNotice({
  name,
  archived,
  pending,
  onConfirm,
  onReject,
}: {
  /** Who the card says this is, for the buttons' accessible names. */
  name: string
  /** Rejected already: the contact is archived and still unconfirmed. */
  archived: boolean
  pending: boolean
  onConfirm: () => void
  onReject: () => void
}) {
  return (
    <div
      role="region"
      aria-label="Needs review"
      data-testid="needs-review"
      className="flex flex-col gap-2 rounded-lg bg-muted/60 px-3 py-2 text-sm ring-1 ring-foreground/10"
    >
      <p>
        <span className="font-medium">Needs review.</span> netkeeper created this contact from a
        card on your LinkedIn connections page, read while LinkedIn&apos;s own data was unavailable.
        The name and headline are what the card showed. Until you confirm it, or a later sync
        matches it, netkeeper won&apos;t enrich it, add it to a campaign, or count it as a
        connection.
      </p>
      {archived && (
        <p className="text-muted-foreground">
          Rejected: it&apos;s archived, not deleted. Confirming it still leaves it archived.
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          disabled={pending}
          aria-label={`Confirm ${name}`}
          onClick={onConfirm}
          data-testid="needs-review-confirm"
        >
          Confirm
        </Button>
        {!archived && (
          <Button
            size="sm"
            variant="outline"
            disabled={pending}
            aria-label={`Reject ${name} and archive the contact`}
            onClick={onReject}
            data-testid="needs-review-reject"
          >
            Reject and archive
          </Button>
        )}
      </div>
    </div>
  )
}
