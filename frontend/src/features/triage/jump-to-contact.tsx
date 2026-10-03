/**
 * Jump to a contact in the queue without deciding the ones before them (#322).
 *
 * A search box over the queue being served: it offers only contacts the queue
 * would serve (the same `met` states, live, and `decided_by` for the review
 * pass), so a contact waiting in another queue or one already answered never
 * shows up. Picking one puts them on screen next; the cards that were in hand
 * stay behind them, so the normal order carries on once they are answered.
 *
 * The key handler ignores keys typed into a text field, so typing a name here
 * never decides anybody.
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { Input } from '@/components/ui/input'

import { decidedByFor, searchQueue, statesFor, type QueueFilter } from './api'

export function JumpToContact({
  filter,
  onJump,
}: {
  filter: QueueFilter
  onJump: (contactId: number) => Promise<void>
}) {
  const [text, setText] = useState('')
  const trimmed = text.trim()
  const matches = useQuery({
    queryKey: ['triage', 'jump', filter, trimmed],
    queryFn: ({ signal }) =>
      searchQueue({
        states: statesFor(filter),
        decidedBy: decidedByFor(filter),
        text: trimmed,
        signal,
      }),
    enabled: trimmed !== '',
    // The queue moves with every decision, so a cached answer would offer
    // somebody who has just been answered.
    gcTime: 0,
  })

  return (
    <section
      aria-label="Jump to a contact"
      className="flex min-w-0 flex-col gap-2 rounded-xl bg-card p-3 ring-1 ring-foreground/10"
    >
      <div>
        <h3 className="font-medium">Jump to a contact</h3>
        <p className="text-xs text-muted-foreground">
          Triage someone next without deciding everyone before them. The queue carries on where it
          was afterwards.
        </p>
      </div>
      <Input
        type="search"
        aria-label="Search the queue by name"
        placeholder="Search the queue by name"
        value={text}
        onChange={(event) => setText(event.target.value)}
      />
      {trimmed !== '' && matches.isError && (
        <p role="alert" className="text-xs text-destructive">
          The search failed: {matches.error.message}
        </p>
      )}
      {trimmed !== '' && matches.isSuccess && matches.data.length === 0 && (
        <p className="text-xs text-muted-foreground">
          Nobody in {filterName(filter)} matches “{trimmed}”.
        </p>
      )}
      {matches.isSuccess && matches.data.length > 0 && (
        <ul className="flex flex-col gap-0.5 text-sm" aria-label="Matches">
          {matches.data.map((row) => (
            <li key={row.id}>
              <button
                type="button"
                className="flex w-full min-w-0 items-center rounded-md px-2 py-1 text-left hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
                onClick={() => {
                  void onJump(row.id)
                  setText('')
                }}
              >
                <span className="min-w-0 truncate">{row.name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function filterName(filter: QueueFilter): string {
  return {
    unknown: 'the untriaged',
    skip: 'the skipped',
    both: 'this queue',
    automatic: 'the review pass',
  }[filter]
}
