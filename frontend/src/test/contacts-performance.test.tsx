/**
 * The measured half of "the table stays responsive at 10,000 rows" (P1-12).
 *
 * Four claims, each counted rather than eyeballed:
 *
 * 1. A long list renders a window, not every row, and the window is exactly the
 *    viewport plus its overscan — too small is as wrong as too large, because a
 *    short window is visible blank space at the bottom of a real viewport.
 * 2. Scrolling mounts another window's worth, not the rows in between.
 * 3. `memo` on the row holds: a selection change re-renders the row that
 *    changed and no other.
 * 4. Typing in the filter box re-renders no rows, and a burst of keystrokes
 *    reaches the server as one query — because of the debounce, not because the
 *    test fired them in the same tick.
 *
 * `rowRenders` counts every render of `ContactTableRow` (see
 * `features/contacts/instrumentation.ts`).
 *
 * A caveat the numbers carry: jsdom has no layout, so the container reports
 * `clientHeight: 0` and the window falls back to `FALLBACK_VIEWPORT`. The row
 * counts here are therefore that constant divided by the row height plus
 * overscan, not a measurement of a browser viewport. What is measured is that
 * the window is bounded and that the counts above are what they are.
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { COLUMNS_BY_ID, type ColumnSpec } from '@/features/contacts/columns'
import type { RowActions } from '@/features/contacts/contact-row'
import {
  ContactsTable,
  FALLBACK_VIEWPORT,
  OVERSCAN,
  ROW_HEIGHT,
} from '@/features/contacts/contacts-table'
import { FILTER_DEBOUNCE_MS } from '@/features/contacts/filter-bar'
import { resetRowRenders, rowRenders } from '@/features/contacts/instrumentation'

import { contactPage, contactRow, mockApi, queries } from './contacts-fixtures'
import { jsonResponse } from './fetch'
import { renderApp } from './render'

const BIG = 10_000
const EMPTY_SORT: never[] = []

/** Rows in view when the container reports no height, plus the overscan below. */
const WINDOW_AT_TOP = Math.ceil(FALLBACK_VIEWPORT / ROW_HEIGHT) + OVERSCAN
/** Once scrolled, the overscan is on both sides. */
const WINDOW_MID_LIST = Math.ceil(FALLBACK_VIEWPORT / ROW_HEIGHT) + 2 * OVERSCAN

/** Columns that render no router links, so the table can be mounted on its own. */
const COLUMNS: ColumnSpec[] = ['headline', 'current_company', 'location', 'met'].map((id) => {
  const column = COLUMNS_BY_ID.get(id as never)
  if (!column) throw new Error(`no such column: ${id}`)
  return column
})

/**
 * Stable callbacks, as the page's own `useCallback` and `useMemo` give the
 * table. A fresh arrow per render would break `memo` on the row by itself, and
 * the memo test below would then be measuring the test, not the component.
 */
const noop = () => undefined

const NO_ACTIONS: RowActions = {
  setMet: () => undefined,
  setDoNotContact: () => undefined,
  setArchived: () => undefined,
  toggleTag: () => undefined,
  addToList: () => undefined,
}

/**
 * Prints a measurement. Vitest swallows console output from a passing test
 * unless it is run with `--disable-console-intercept`, so these show up when
 * somebody goes looking, not on every run.
 */
function report(what: string, value: number, unit: string) {
  console.log(`[P1-12] ${what}: ${value.toFixed(1)}${unit}`)
}

function renderTable(rows: ReturnType<typeof contactRow>[], selected: ReadonlySet<number>) {
  return render(
    <ContactsTable
      rows={rows}
      scrollKey="page-1"
      columns={COLUMNS}
      sort={EMPTY_SORT}
      onSort={noop}
      selectedIds={selected}
      everything={false}
      onSelect={noop}
      onSelectPage={noop}
      actions={NO_ACTIONS}
    />,
  )
}

function renderedRows(): number {
  return document.querySelectorAll('tbody tr[data-slot="table-row"]').length
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

describe('contacts table responsiveness', () => {
  it('renders exactly one window of a 10,000-row list', () => {
    const rows = Array.from({ length: BIG }, (_, index) => contactRow(index + 1))
    resetRowRenders()

    const started = performance.now()
    renderTable(rows, new Set())
    const mount = performance.now() - started
    report(`first render of ${BIG} rows`, mount, 'ms')
    report('rows rendered', rowRenders.count, '')

    // Exact, not a ceiling: a window nine rows short would leave real blank
    // space below the last row, and `toBeLessThan(60)` would not notice.
    expect(rowRenders.count).toBe(WINDOW_AT_TOP)
    expect(renderedRows()).toBe(WINDOW_AT_TOP)
    expect(mount).toBeLessThan(1000)

    // The rows that are not rendered are spacers, so the scrollbar is honest.
    const padBottom = screen.getByTestId('pad-bottom').firstElementChild as HTMLElement
    expect(padBottom.style.height).toBe(`${(BIG - WINDOW_AT_TOP) * ROW_HEIGHT}px`)
    expect(screen.queryByTestId('pad-top')).toBeNull()
  })

  it('mounts only the rows a scroll brought into view', () => {
    const rows = Array.from({ length: BIG }, (_, index) => contactRow(index + 1))
    renderTable(rows, new Set())
    const viewport = screen.getByTestId('contacts-scroll')
    expect(viewport).toHaveAttribute('data-virtualized', 'true')

    resetRowRenders()
    const started = performance.now()
    Object.defineProperty(viewport, 'scrollTop', { value: 5_000 * ROW_HEIGHT, configurable: true })
    fireEvent.scroll(viewport)
    const scroll = performance.now() - started
    report('scroll 5,000 rows down', scroll, 'ms')
    report('rows mounted by that scroll', rowRenders.count, '')

    expect(rowRenders.count).toBe(WINDOW_MID_LIST)
    expect(renderedRows()).toBe(WINDOW_MID_LIST)
    expect(scroll).toBeLessThan(500)

    // The spacer above accounts for every row scrolled past.
    const padTop = screen.getByTestId('pad-top').firstElementChild as HTMLElement
    expect(padTop.style.height).toBe(`${(5_000 - OVERSCAN) * ROW_HEIGHT}px`)
  })

  it('re-renders one row when one row is selected, not the window', () => {
    const rows = Array.from({ length: BIG }, (_, index) => contactRow(index + 1))
    const view = renderTable(rows, new Set())
    expect(rowRenders.count).toBeGreaterThan(0)

    // The parent re-renders with a new selection; `memo` on the row decides how
    // much of the window goes with it. Without it this is WINDOW_AT_TOP.
    resetRowRenders()
    view.rerender(
      <ContactsTable
        rows={rows}
        scrollKey="page-1"
        columns={COLUMNS}
        sort={EMPTY_SORT}
        onSort={noop}
        selectedIds={new Set([3])}
        everything={false}
        onSelect={noop}
        onSelectPage={noop}
        actions={NO_ACTIONS}
      />,
    )
    report('rows re-rendered by selecting one', rowRenders.count, '')
    expect(rowRenders.count).toBe(1)
  })

  it('re-renders no rows while the filter is typed, and debounces the burst into one query', async () => {
    const seen = mockApi((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/query') {
        return jsonResponse(
          contactPage(
            Array.from({ length: 200 }, (_, index) => contactRow(index + 1)),
            BIG,
          ),
        )
      }
      return undefined
    })

    await renderApp('/contacts?size=200')
    await screen.findByRole('table')

    // A 200-row page is windowed too: the rendered page is what has to stay cheap.
    report('rows rendered from a 200-row page', renderedRows(), '')
    expect(renderedRows()).toBe(WINDOW_AT_TOP)

    const before = queries(seen).length
    resetRowRenders()
    const input = screen.getByLabelText('Search contacts')

    // Typed at a human cadence, with a real gap between keystrokes: a debounce
    // of zero would have sent a query during the first pause.
    const gap = Math.floor(FILTER_DEBOUNCE_MS / 2)
    const started = performance.now()
    for (const text of ['f', 'fe', 'fer', 'ferr', 'ferry']) {
      fireEvent.change(input, { target: { value: text } })
      await act(async () => {
        await sleep(gap)
      })
      expect(queries(seen).length).toBe(before)
    }
    const typing = performance.now() - started
    report('five keystrokes a half-debounce apart', typing, 'ms')
    report('rows re-rendered by typing', rowRenders.count, '')

    // The headline number: typing touches the input, and nothing else.
    expect(rowRenders.count).toBe(0)

    // One query for the whole burst, once the pause outlasts the debounce.
    await waitFor(() => expect(queries(seen).length).toBe(before + 1))
    expect(JSON.stringify(queries(seen).at(-1)?.filter)).toContain('ferry')

    // And nothing more arrives afterwards.
    await act(async () => {
      await sleep(FILTER_DEBOUNCE_MS * 2)
    })
    expect(queries(seen).length).toBe(before + 1)
  })
})
