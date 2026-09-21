import { useInfiniteQuery } from '@tanstack/react-query'
import { CalendarClock, MessageSquare } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'

import { contactsKeys, fetchTimelinePage } from './api'
import { formatDateTime } from './format'
import type { ContactDetail, TimelineEntry } from './types'

/**
 * Interactions and snapshots interleaved, newest first (spec 8.1, 10.1).
 *
 * The detail response already carries the newest page, so the screen renders in
 * one request; older entries are paged from the timeline endpoint on request.
 */
export function ContactTimeline({ contact }: { contact: ContactDetail }) {
  const [expanded, setExpanded] = useState(false)
  const older = useInfiniteQuery({
    queryKey: contactsKeys.timeline(contact.id),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => fetchTimelinePage(contact.id, pageParam, signal),
    getNextPageParam: (last) => last.next_before,
    enabled: expanded,
    retry: false,
  })

  const entries: TimelineEntry[] = expanded
    ? (older.data?.pages.flatMap((page) => page.items) ?? [])
    : contact.timeline

  if (!expanded && entries.length === 0) {
    return <p className="text-muted-foreground">Nothing has happened with this contact yet.</p>
  }

  return (
    <div className="grid gap-2">
      <ol className="grid gap-2">
        {entries.map((entry) => (
          <li
            key={`${entry.kind}-${entry.kind === 'interaction' ? entry.interaction.id : entry.snapshot.id}`}
            className="flex gap-2"
          >
            <span className="mt-0.5 text-muted-foreground">
              {entry.kind === 'interaction' ? (
                <MessageSquare className="size-4" />
              ) : (
                <CalendarClock className="size-4" />
              )}
            </span>
            <div className="min-w-0">
              <div className="text-xs text-muted-foreground">{formatDateTime(entry.at)}</div>
              {entry.kind === 'interaction' ? (
                <div>
                  <span className="font-medium">{entry.interaction.kind.replace(/_/g, ' ')}</span>
                  {entry.interaction.summary ? ` · ${entry.interaction.summary}` : ''}
                </div>
              ) : (
                <div>
                  <span className="font-medium">Snapshot</span>
                  {' · '}
                  {[
                    entry.snapshot.headline,
                    entry.snapshot.current_title,
                    entry.snapshot.current_company,
                  ]
                    .filter(Boolean)
                    .join(' — ') || 'no headline recorded'}
                </div>
              )}
            </div>
          </li>
        ))}
      </ol>

      {expanded && older.isPending && <p className="text-muted-foreground">Loading timeline…</p>}
      {expanded && older.isError && (
        <p role="alert" className="text-destructive">
          The timeline could not be loaded: {older.error.message}
        </p>
      )}

      {!expanded ? (
        <div>
          <Button variant="outline" size="sm" onClick={() => setExpanded(true)}>
            Show the full timeline
          </Button>
        </div>
      ) : (
        older.hasNextPage && (
          <div>
            <Button
              variant="outline"
              size="sm"
              disabled={older.isFetchingNextPage}
              onClick={() => void older.fetchNextPage()}
            >
              {older.isFetchingNextPage ? 'Loading…' : 'Load older entries'}
            </Button>
          </div>
        )
      )}
    </div>
  )
}
