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

const ALSO: Partial<Record<PossibleDuplicate['matched_by'][number], string>> = {
  email: 'email address',
  phone: 'phone number',
}

/**
 * Why the backend thinks so, in words. The names always agree; a shared address
 * or number adds to that, and differing LinkedIn ids count against it.
 */
function reasonText(match: PossibleDuplicate): string {
  const also = match.matched_by.flatMap((reason) => ALSO[reason] ?? [])
  const same = also.length === 0 ? 'the same name' : `the same name and ${also.join(' and ')}`
  return match.linkedin_ids_differ ? `${same}; LinkedIn ids differ` : same
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
