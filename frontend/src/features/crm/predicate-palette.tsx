/**
 * The list of predicates you can add to a filter.
 *
 * Every predicate the language defines appears here, including the three the
 * server parses but refuses to compile. Those are shown disabled with the
 * reason spelled out rather than left off the list: a builder that quietly
 * omits part of the language teaches people the language is smaller than it
 * is, and hides that the gap is temporary.
 */
import { Badge } from '@/components/ui/badge'

import { GROUP_LABELS, PREDICATES, UNAVAILABLE_GROUP_LABEL } from './predicates'
import type { PredicateGroup, PredicateSpec } from './predicates'

const GROUP_ORDER: readonly PredicateGroup[] = ['field', 'presence', 'time', 'tags', 'logic']

interface PredicatePaletteProps {
  onPick: (spec: PredicateSpec) => void
  onCancel: () => void
}

export function PredicatePalette({ onPick, onCancel }: PredicatePaletteProps) {
  const available = PREDICATES.filter((spec) => spec.unavailable === undefined)
  const unavailable = PREDICATES.filter((spec) => spec.unavailable !== undefined)

  return (
    <div
      data-slot="predicate-palette"
      aria-label="Add a condition"
      className="space-y-4 rounded-lg border bg-card p-3"
    >
      {GROUP_ORDER.map((group) => {
        const items = available.filter((spec) => spec.group === group)
        if (items.length === 0) return null
        return (
          <section key={group} aria-label={GROUP_LABELS[group]}>
            <h4 className="mb-1.5 text-xs font-medium tracking-wide text-muted-foreground uppercase">
              {GROUP_LABELS[group]}
            </h4>
            <div className="grid gap-1 sm:grid-cols-2">
              {items.map((spec) => (
                <button
                  key={spec.op}
                  type="button"
                  data-op={spec.op}
                  onClick={() => onPick(spec)}
                  className="rounded-md border border-transparent px-2 py-1.5 text-left transition-colors hover:border-border hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
                >
                  <span className="block text-sm font-medium">{spec.label}</span>
                  <span className="block text-xs text-muted-foreground">{spec.hint}</span>
                </button>
              ))}
            </div>
          </section>
        )
      })}

      <section aria-label={UNAVAILABLE_GROUP_LABEL}>
        <h4 className="mb-1.5 text-xs font-medium tracking-wide text-muted-foreground uppercase">
          {UNAVAILABLE_GROUP_LABEL}
        </h4>
        <div className="grid gap-1">
          {unavailable.map((spec) => (
            <div
              key={spec.op}
              data-op={spec.op}
              data-unavailable="true"
              aria-disabled="true"
              className="rounded-md border border-dashed px-2 py-1.5 opacity-80"
            >
              <span className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                {spec.label}
                <Badge variant="outline">Unavailable</Badge>
              </span>
              <span className="block text-xs text-muted-foreground">{spec.unavailable}</span>
            </div>
          ))}
        </div>
      </section>

      <button
        type="button"
        onClick={onCancel}
        className="text-xs text-muted-foreground underline-offset-4 hover:underline"
      >
        Cancel
      </button>
    </div>
  )
}
