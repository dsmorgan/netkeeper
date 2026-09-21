/**
 * The bulk suggestion of spec 10.2: "you have message threads with 143
 * untriaged people — mark them all met?"
 *
 * Three rules shape this component:
 *
 * - It is drawn only when the API offers a suggestion. A suggestion that matches
 *   nobody is not returned, so an empty list means no banner, not a banner
 *   reading zero.
 * - The count on screen is sent back with the apply. If the set moved since the
 *   banner was drawn, the API answers `409` and nothing is written — that is not
 *   an error to report, it is a stale preview to take again, so the banner
 *   refetches and shows the new number for the person to accept or not.
 * - Applying is one batch, and `u` takes the whole batch back. The banner says
 *   so, because "applied to 143 people" is not a thing to leave someone guessing
 *   about.
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'

import {
  SUGGESTIONS_KEY,
  fetchSuggestions,
  statesFor,
  type QueueFilter,
  type TriageSuggestion,
} from './api'
import type { BulkOutcome } from './use-triage-queue'

export function SuggestionBanner({
  filter,
  onApply,
}: {
  filter: QueueFilter
  onApply: (key: string, expectedCount: number) => Promise<BulkOutcome>
}) {
  const [notice, setNotice] = useState<string | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // A suggestion is an offer, not the screen's job: a preview that fails simply
  // means no banner, so the error branch is left to fall through to `[]`.
  const preview = useQuery({
    queryKey: [...SUGGESTIONS_KEY, filter],
    queryFn: ({ signal }) => fetchSuggestions({ states: statesFor(filter), signal }),
    retry: false,
  })
  const suggestions = preview.data ?? []

  async function apply(suggestion: TriageSuggestion) {
    setBusy(true)
    setProblem(null)
    setNotice(null)
    const outcome = await onApply(suggestion.key, suggestion.count)
    if (outcome.kind === 'applied') {
      setNotice(
        `Marked ${outcome.applied} ${outcome.applied === 1 ? 'contact' : 'contacts'} as met, as one batch. Press u to take the whole batch back.`,
      )
    } else if (outcome.kind === 'count-changed') {
      setNotice(
        'The set moved while this was on screen, so nothing was applied. Here is the count as it stands now.',
      )
    } else {
      setProblem(outcome.detail)
    }
    await preview.refetch()
    setBusy(false)
  }

  if (suggestions.length === 0 && notice === null && problem === null) return null

  return (
    <div className="flex flex-col gap-2">
      {suggestions.map((suggestion) => (
        <div
          key={suggestion.key}
          data-testid="bulk-suggestion"
          className="flex flex-wrap items-center justify-between gap-3 rounded-lg bg-muted/60 px-3 py-2 ring-1 ring-foreground/10"
        >
          <div className="min-w-0">
            <p className="font-medium">{suggestion.description}</p>
            <p className="text-sm text-muted-foreground">
              {suggestion.title} · applies to {suggestion.count}, as one batch you can undo.
            </p>
          </div>
          <Button size="sm" disabled={busy} onClick={() => void apply(suggestion)}>
            {busy ? 'Applying…' : `Mark ${suggestion.count} as met`}
          </Button>
        </div>
      ))}
      {notice !== null && (
        <p role="status" className="text-sm text-muted-foreground">
          {notice}
        </p>
      )}
      {problem !== null && (
        <p role="alert" className="text-sm text-destructive">
          {problem}
        </p>
      )}
    </div>
  )
}
