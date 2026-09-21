/**
 * The visual filter builder: a tree of predicates, and what it selects right now.
 *
 * The tree it edits is the filter language's own JSON (spec 10.4) — the same
 * document a smart list stores, the contacts table sends, and an export
 * respects — so nothing is translated on the way out and a filter built here
 * round-trips through the API unchanged.
 *
 * The count under the header comes from `POST /contacts/query`, which runs the
 * very compiler lists and exports run. It is asked for again on every edit and
 * never cached across one: a smart list's membership is live, never
 * materialized, and a count left over from the previous filter would be a
 * quietly wrong answer.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'

import { countFilter } from './api'
import { Callout } from './controls'
import { NodeEditor } from './node-editor'
import { PredicatePalette } from './predicate-palette'
import type { PredicateSpec } from './predicates'
import type { FilterPath } from './tree'
import { appendAt, pathKey, removeAt, replaceAt, validateTree } from './tree'
import type { FilterTree, TagOut } from './types'
import { useDebounced } from './use-debounced'

const ROOT_KEY = 'root'

export interface FilterBuilderProps {
  value: FilterTree
  onChange: (next: FilterTree) => void
  tags: readonly TagOut[]
  /** Off for a builder inside a dialog that already shows its own count. */
  showCount?: boolean
}

export function FilterBuilder({ value, onChange, tags, showCount = true }: FilterBuilderProps) {
  const [openPaletteKey, setOpenPaletteKey] = useState<string | null>(null)
  const issues = validateTree(value)

  const handlePick = (path: FilterPath, spec: PredicateSpec) => {
    setOpenPaletteKey(null)
    const fresh = spec.create()
    if (value.where === null || value.where === undefined) {
      onChange({ ...value, where: fresh })
      return
    }
    onChange({ ...value, where: appendAt(value.where, path, fresh) })
  }

  const renderPalette = (path: FilterPath) => (
    <PredicatePalette
      onPick={(spec) => handlePick(path, spec)}
      onCancel={() => setOpenPaletteKey(null)}
    />
  )

  return (
    <div data-slot="filter-builder" className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <Label className="gap-2">
          <Checkbox
            checked={value.include_archived}
            onCheckedChange={(checked) =>
              onChange({ ...value, include_archived: checked === true })
            }
          />
          Include archived contacts
        </Label>
        {showCount && <FilterCount value={value} blocked={issues.length > 0} />}
      </div>

      {value.where === null || value.where === undefined ? (
        <div className="space-y-2 rounded-lg border border-dashed p-3">
          <p className="text-sm text-muted-foreground">
            No conditions: this selects every contact.
          </p>
          {openPaletteKey === ROOT_KEY ? (
            renderPalette([])
          ) : (
            <button
              type="button"
              onClick={() => setOpenPaletteKey(ROOT_KEY)}
              className="text-sm text-primary underline-offset-4 hover:underline"
            >
              Add a condition
            </button>
          )}
        </div>
      ) : (
        <NodeEditor
          node={value.where}
          path={[]}
          tags={tags}
          openPaletteKey={openPaletteKey}
          renderPalette={renderPalette}
          onRequestAdd={(path) => setOpenPaletteKey(pathKey(path))}
          onChange={(path, next) => {
            if (value.where === null || value.where === undefined) return
            onChange({ ...value, where: replaceAt(value.where, path, () => next) })
          }}
          onRemove={(path) => {
            if (value.where === null || value.where === undefined) return
            onChange({ ...value, where: removeAt(value.where, path) ?? null })
          }}
        />
      )}

      {issues.length > 0 && (
        <Callout tone="warning" title="Not ready to run">
          <ul className="list-disc space-y-0.5 pl-4">
            {issues.map((issue) => (
              <li key={`${pathKey(issue.path)}-${issue.message}`}>{issue.message}</li>
            ))}
          </ul>
        </Callout>
      )}
    </div>
  )
}

/**
 * How many contacts the filter selects, asked again whenever the filter settles.
 *
 * `gcTime: 0` and a key carrying the whole tree mean an edited filter never
 * shows the previous filter's count, not even for the moment before the new
 * answer lands.
 */
export function FilterCount({ value, blocked }: { value: FilterTree; blocked: boolean }) {
  const settled = useDebounced(JSON.stringify(value))
  const query = useQuery({
    queryKey: ['contacts', 'count', settled],
    queryFn: ({ signal }) => countFilter(JSON.parse(settled) as FilterTree, signal),
    enabled: !blocked,
    retry: false,
    gcTime: 0,
  })

  if (blocked) {
    return (
      <span role="status" className="text-sm text-muted-foreground">
        Finish the conditions to see a count.
      </span>
    )
  }
  if (query.isPending) {
    return (
      <span role="status" className="text-sm text-muted-foreground">
        Counting…
      </span>
    )
  }
  if (query.isError) {
    return (
      <span role="status" className="text-sm text-destructive">
        {query.error instanceof Error ? query.error.message : 'Could not count the filter.'}
      </span>
    )
  }
  return (
    <span role="status" className="text-sm">
      <strong>{query.data.total.toLocaleString()}</strong> contacts match
      <span className="text-muted-foreground"> — {query.data.describe}</span>
    </span>
  )
}
