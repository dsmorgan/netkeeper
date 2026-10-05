import { X } from 'lucide-react'
import { useEffect, useRef, type RefObject } from 'react'

import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

/** One active search term or filter, shown as a chip you can remove. */
export interface FilterChip {
  /** Stable across renders; also what focus follows when a neighbor goes away. */
  key: string
  /** What the chip says: `"acme"` for a search term, `status: replied` for a filter. */
  label: string
  onRemove: () => void
}

/**
 * Says when a search or filter narrows a list (#402): how many of how many it
 * shows, what narrows it as removable chips, and a Clear action.
 *
 * Render it whether or not anything is active: the count sits in a polite live
 * region that has to exist before its text changes for a screen reader to
 * announce the change. With no chips it renders that region empty.
 *
 * `shown` is the number of matches, every page of them, not the rows on screen.
 * `total` is the list without any of the chips' filters; leave it out while it
 * loads and the line says only how many match.
 */
export function FilterSummary({
  shown,
  total,
  chips,
  onClear,
  returnFocusTo,
  className,
}: {
  shown: number | undefined
  total: number | undefined
  chips: readonly FilterChip[]
  onClear: () => void
  /** Where the focus goes once the last chip is gone, by Clear or by removing it. */
  returnFocusTo?: RefObject<HTMLElement | null>
  className?: string
}) {
  const list = useRef<HTMLUListElement>(null)
  // Where the focus goes once a removal has re-rendered the chips: a chip's key,
  // or `null` for `returnFocusTo`. A page may apply a removal a beat later (a URL
  // change), so this waits for the chips themselves to change, not for a frame.
  const pendingFocus = useRef<{ key: string | null } | undefined>(undefined)
  const active = chips.length > 0
  const keys = chips.map((chip) => chip.key).join('\n')

  useEffect(() => {
    const pending = pendingFocus.current
    if (pending === undefined) return
    if (pending.key === null) {
      if (chips.length > 0) return
      pendingFocus.current = undefined
      returnFocusTo?.current?.focus()
      return
    }
    const target = [
      ...(list.current?.querySelectorAll<HTMLElement>('button[data-chip]') ?? []),
    ].find((button) => button.dataset.chip === pending.key)
    if (target === undefined) return
    pendingFocus.current = undefined
    target.focus()
    // `keys` is what changes when a chip goes; `chips` is a new array every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keys])

  function remove(index: number) {
    // The chip after it takes its place; the last one's neighbor is before it.
    const neighbor = chips[index + 1] ?? chips[index - 1]
    pendingFocus.current = { key: neighbor?.key ?? null }
    chips[index]?.onRemove()
  }

  return (
    <div
      data-slot="filter-summary"
      className={cn(
        active && 'flex flex-wrap items-center gap-x-2 gap-y-1.5 text-sm',
        className,
        !active && 'contents',
      )}
    >
      <p role="status" aria-live="polite" className={active ? 'text-muted-foreground' : 'sr-only'}>
        {active ? summaryText(shown, total) : ''}
        {active && <span className="sr-only"> {chips.map((chip) => chip.label).join(', ')}</span>}
      </p>
      {active && (
        <>
          <ul
            ref={list}
            aria-label="Active filters"
            className="flex min-w-0 flex-wrap items-center gap-1.5"
          >
            {chips.map((chip, index) => (
              <li key={chip.key} className="min-w-0">
                <button
                  type="button"
                  data-chip={chip.key}
                  aria-label={`Remove ${chip.label}`}
                  title={`Remove ${chip.label}`}
                  onClick={() => remove(index)}
                  className="inline-flex h-6 max-w-full min-w-0 items-center gap-1 rounded-4xl border border-border bg-muted/60 pr-1.5 pl-2.5 text-xs font-medium outline-none hover:bg-muted focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
                >
                  <span className="truncate">{chip.label}</span>
                  <X aria-hidden="true" className="size-3 shrink-0" />
                </button>
              </li>
            ))}
          </ul>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              pendingFocus.current = { key: null }
              onClear()
            }}
          >
            Clear
          </Button>
        </>
      )}
    </div>
  )
}

function summaryText(shown: number | undefined, total: number | undefined): string {
  if (shown === undefined) return 'Filtered by:'
  const count = shown.toLocaleString()
  if (total === undefined) return `Showing ${count} · filtered by:`
  return `Showing ${count} of ${total.toLocaleString()} · filtered by:`
}
