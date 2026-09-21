/**
 * The live "matches N contacts" count under an auto-tag rule's pattern.
 *
 * Two things here are load-bearing.
 *
 * The server refuses an unsafe pattern with a sentence written for whoever
 * typed it — a nested unbounded repeat like `(a+)+` can backtrack for a minute
 * while holding the SQLite write lock, and nested counted repeats can ask for
 * gigabytes before the first search. That sentence is rendered as it arrives;
 * replacing it with "invalid pattern" would hide the fix.
 *
 * The preview also reports `timeouts`: contacts whose search hit the 50 ms
 * budget and were counted as no match. A non-zero count means the number
 * beside it is a floor, not the answer, so it is shown rather than dropped.
 */
import { useQuery } from '@tanstack/react-query'

import { previewRule } from './api'
import { Callout } from './controls'
import type { RuleField } from './types'
import { useDebounced } from './use-debounced'

export function RulePreview({ field, pattern }: { field: RuleField; pattern: string }) {
  const settled = useDebounced(pattern)
  const query = useQuery({
    queryKey: ['autotag-rules', 'preview', field, settled],
    queryFn: ({ signal }) => previewRule(field, settled, signal),
    enabled: settled.trim() !== '',
    retry: false,
    gcTime: 0,
  })

  if (settled.trim() === '') {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        Type a pattern to see how many contacts it matches.
      </p>
    )
  }
  if (query.isPending) {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        Counting matches…
      </p>
    )
  }
  if (query.isError) {
    return (
      <Callout tone="danger" title="The server refused this pattern">
        <p>{query.error instanceof Error ? query.error.message : 'Unusable pattern.'}</p>
      </Callout>
    )
  }
  return (
    <div className="space-y-2">
      <p role="status" className="text-sm">
        Matches <strong>{query.data.count.toLocaleString()}</strong>{' '}
        {query.data.count === 1 ? 'contact' : 'contacts'}.
      </p>
      {query.data.timeouts > 0 && (
        <Callout tone="warning" title="Some contacts were not searched in time">
          <p>
            {query.data.timeouts.toLocaleString()}{' '}
            {query.data.timeouts === 1 ? 'contact' : 'contacts'} hit the 50 ms search budget and
            were counted as no match, so the number above may understate the matches. A simpler
            pattern searches faster.
          </p>
        </Callout>
      )}
    </div>
  )
}
