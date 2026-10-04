/**
 * "Possible duplicate of <name>" on a review band (#363).
 *
 * A contact read off a connections-page card is often somebody already in the
 * address book under an old slug (#186 item 5). The backend names the likely
 * ones (`GET /contacts/{id}/duplicates`): conservative, read-only, and scoped to
 * the current user. This only says so and offers a merge; it never merges.
 *
 * Nothing renders while the hint loads, when it finds nobody, or when it fails:
 * a missing hint costs nothing that **Merge with…** cannot do by hand.
 */

import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'

import { Button } from '@/components/ui/button'

import { duplicatesQuery } from './api'
import { displayName } from './format'
import type { PossibleDuplicate } from './types'

const REASONS: Record<PossibleDuplicate['matched_by'][number], string> = {
  email: 'the same email address',
  phone: 'the same phone number',
  slug: 'the same name under a different LinkedIn id',
  name: 'the same name',
}

/** Why the backend thinks so, in words: its strongest reason first. */
function reasonText(match: PossibleDuplicate): string {
  // A slug match is a name match too; saying "the same name" twice says nothing.
  const reasons = match.matched_by.includes('slug')
    ? match.matched_by.filter((reason) => reason !== 'name')
    : match.matched_by
  return reasons.map((reason) => REASONS[reason]).join(', ')
}

export function PossibleDuplicates({
  contactId,
  onMerge,
  newTab = false,
}: {
  contactId: number
  onMerge: (match: PossibleDuplicate) => void
  /** Open the other contact in a new tab, so a triage run keeps its place. */
  newTab?: boolean
}) {
  const found = useQuery(duplicatesQuery(contactId))
  if (!found.isSuccess || found.data.length === 0) return null

  return (
    <ul aria-label="Possible duplicates" data-testid="possible-duplicates" className="grid gap-1">
      {found.data.map((match) => {
        const name = displayName(match)
        return (
          <li key={match.contact_id} className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span>
              Possible duplicate of{' '}
              {newTab ? (
                <a
                  href={`/contacts/${match.contact_id}`}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="font-medium underline underline-offset-4"
                >
                  {name}
                </a>
              ) : (
                <Link
                  to="/contacts/$contactId"
                  params={{ contactId: String(match.contact_id) }}
                  className="font-medium underline underline-offset-4"
                >
                  {name}
                </Link>
              )}{' '}
              <span className="text-muted-foreground">({reasonText(match)})</span>
            </span>
            <Button
              size="xs"
              variant="outline"
              aria-label={`Merge with ${name}`}
              onClick={() => onMerge(match)}
            >
              Merge…
            </Button>
          </li>
        )
      })}
    </ul>
  )
}
