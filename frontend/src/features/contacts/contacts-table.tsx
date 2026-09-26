import { ArrowDown, ArrowUp, ChevronsUpDown } from 'lucide-react'
import { useCallback, useLayoutEffect, useRef, useState } from 'react'

import { Checkbox } from '@/components/ui/checkbox'
import { Table, TableBody, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'

import type { ColumnSpec } from './columns'
import { ContactTableRow, type RowActions } from './contact-row'
import type { ContactRow, SortField, SortKey } from './types'

/**
 * Fixed row height, so the window below is arithmetic instead of measurement.
 *
 * The windowing here is about ninety lines and needs one number, because every
 * row is the same height and the page is capped at 200 rows by the API.
 * `@tanstack/react-virtual` is the natural reach in a project that already
 * leans on TanStack, and it is the right answer for variable row heights,
 * measured elements, or horizontal windowing — none of which this table has. It
 * would add a dependency to this repo's lockfile to replace arithmetic that is
 * exactly tested three ways in `contacts-performance.test.tsx`. If a row ever
 * grows to a variable height, swap this out for it rather than growing it.
 */
export const ROW_HEIGHT = 36
/** Rows kept above and below the viewport, so a fast scroll does not show gaps. */
export const OVERSCAN = 8
/**
 * Below this, every row renders: a short page is cheaper whole than windowed,
 * and a windowed table cannot be searched with the browser's own find.
 */
export const VIRTUALIZE_ABOVE = 60
/**
 * The viewport height to assume when the container reports none.
 *
 * jsdom has no layout, so `clientHeight` is always 0 there and every windowing
 * measurement under test is this constant, not a real viewport. In a browser it
 * only covers the first paint, before the container has been laid out.
 */
export const FALLBACK_VIEWPORT = 640

interface RowWindow {
  start: number
  end: number
}

function windowFor(count: number, scrollTop: number, height: number): RowWindow {
  const start = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN)
  const end = Math.min(count, Math.ceil((scrollTop + height) / ROW_HEIGHT) + OVERSCAN)
  return { start, end: Math.max(end, start) }
}

/**
 * Which rows of `count` are worth rendering.
 *
 * Scrolling sets state only when the window actually moves, so a scroll is a
 * handful of row mounts rather than a re-render per pixel, and a page below
 * {@link VIRTUALIZE_ABOVE} skips the whole mechanism.
 */
function useRowWindow(count: number, enabled: boolean, scrollKey: string) {
  const viewportRef = useRef<HTMLDivElement | null>(null)
  const [scrolled, setScrolled] = useState<RowWindow>(() => windowFor(count, 0, FALLBACK_VIEWPORT))

  const measure = useCallback(() => {
    const node = viewportRef.current
    const next = windowFor(count, node?.scrollTop ?? 0, node?.clientHeight || FALLBACK_VIEWPORT)
    setScrolled((current) =>
      current.start === next.start && current.end === next.end ? current : next,
    )
  }, [count])

  useLayoutEffect(() => {
    if (!enabled) return
    measure()
    const node = viewportRef.current
    node?.addEventListener('scroll', measure, { passive: true })
    window.addEventListener('resize', measure)
    return () => {
      node?.removeEventListener('scroll', measure)
      window.removeEventListener('resize', measure)
    }
  }, [enabled, measure])

  // New rows start at the top. Next, Previous, a new filter or sort, or a cached
  // page coming back can all reuse this scroller, which would otherwise keep
  // the old offset and open the new page somewhere in its middle (#88). Keyed on
  // what was asked for, not on the rows: a row action refetches the same page,
  // and that must leave the person where they were.
  const shownKey = useRef(scrollKey)
  useLayoutEffect(() => {
    if (shownKey.current === scrollKey) return
    shownKey.current = scrollKey
    const node = viewportRef.current
    if (node) node.scrollTop = 0
    measure()
  }, [scrollKey, measure])

  // A short page renders whole, and a page that shrank under a stale window is
  // clamped here rather than by a second render.
  const rowWindow: RowWindow = enabled
    ? { start: Math.min(scrolled.start, count), end: Math.min(scrolled.end, count) }
    : { start: 0, end: count }

  return { viewportRef, rowWindow }
}

export interface ContactsTableProps {
  rows: readonly ContactRow[]
  /**
   * What the rows are an answer to (filter, sort, page). A new key scrolls back
   * to the top; the same key with new rows, after a row action, does not.
   */
  scrollKey: string
  columns: readonly ColumnSpec[]
  sort: readonly SortKey[]
  onSort: (field: SortField) => void
  selectedIds: ReadonlySet<number>
  /** True while the whole filter is selected: every row shows as picked. */
  everything: boolean
  onSelect: (id: number, selected: boolean) => void
  onSelectPage: (selected: boolean) => void
  actions: RowActions
}

export function ContactsTable({
  rows,
  scrollKey,
  columns,
  sort,
  onSort,
  selectedIds,
  everything,
  onSelect,
  onSelectPage,
  actions,
}: ContactsTableProps) {
  const virtualized = rows.length > VIRTUALIZE_ABOVE
  const { viewportRef, rowWindow } = useRowWindow(rows.length, virtualized, scrollKey)
  const visible = rows.slice(rowWindow.start, rowWindow.end)
  const padTop = rowWindow.start * ROW_HEIGHT
  const padBottom = (rows.length - rowWindow.end) * ROW_HEIGHT
  const span = columns.length + 2

  const pageSelected =
    everything || (rows.length > 0 && rows.every((row) => selectedIds.has(row.id)))
  const somePicked = !pageSelected && rows.some((row) => selectedIds.has(row.id))

  return (
    <div
      ref={viewportRef}
      data-testid="contacts-scroll"
      data-virtualized={virtualized || undefined}
      className="max-h-[calc(100vh-16rem)] overflow-auto rounded-xl ring-1 ring-foreground/10"
    >
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-8 pl-3">
              <Checkbox
                checked={pageSelected}
                indeterminate={somePicked}
                onCheckedChange={(checked) => onSelectPage(checked === true)}
                aria-label="Select every contact on this page"
              />
            </TableHead>
            {columns.map((column) => {
              const key = sort.find((entry) => entry.field === column.sort)
              return (
                <TableHead key={column.id} aria-sort={ariaSort(key?.direction)}>
                  {column.sort ? (
                    <button
                      type="button"
                      onClick={() => onSort(column.sort as SortField)}
                      className="-mx-1 inline-flex items-center gap-1 rounded px-1 py-0.5 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                    >
                      {column.label}
                      <SortIcon direction={key?.direction} />
                    </button>
                  ) : (
                    column.label
                  )}
                </TableHead>
              )
            })}
            <TableHead className="w-10 pr-3">
              <span className="sr-only">Row actions</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {padTop > 0 && (
            <tr aria-hidden="true" data-testid="pad-top">
              <td colSpan={span} style={{ height: padTop }} />
            </tr>
          )}
          {visible.map((row) => (
            <ContactTableRow
              key={row.id}
              row={row}
              columns={columns}
              selected={everything || selectedIds.has(row.id)}
              onSelect={onSelect}
              actions={actions}
              height={ROW_HEIGHT}
            />
          ))}
          {padBottom > 0 && (
            <tr aria-hidden="true" data-testid="pad-bottom">
              <td colSpan={span} style={{ height: padBottom }} />
            </tr>
          )}
        </TableBody>
      </Table>
    </div>
  )
}

function ariaSort(direction: 'asc' | 'desc' | undefined) {
  if (direction === 'asc') return 'ascending'
  if (direction === 'desc') return 'descending'
  return 'none'
}

function SortIcon({ direction }: { direction: 'asc' | 'desc' | undefined }) {
  const Icon = direction === 'asc' ? ArrowUp : direction === 'desc' ? ArrowDown : ChevronsUpDown
  return (
    <Icon className={cn('size-3', direction ? 'text-foreground' : 'text-muted-foreground/60')} />
  )
}
