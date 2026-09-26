/**
 * The Contacts table's edges from the #88 review: a `?view=` link read before
 * `/views` answers, a view that failed or is gone, a view the filter bar cannot
 * fully show, a page past the end, and a scroller that kept its offset.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { COLUMNS_BY_ID, type ColumnSpec } from '@/features/contacts/columns'
import type { RowActions } from '@/features/contacts/contact-row'
import { ContactsTable } from '@/features/contacts/contacts-table'

import { contactPage, contactRow, lastQuery, mockApi, queries } from './contacts-fixtures'
import { jsonResponse } from './fetch'
import { renderApp } from './render'

const BEFORE_2020 = {
  id: 9,
  name: 'Before 2020',
  columns: ['name', 'connected_on'],
  sort: [],
  filter: {
    include_archived: false,
    where: { op: 'lt', field: 'connected_on', value: '2020-01-01' },
  },
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
}

const MET_ONLY = {
  ...BEFORE_2020,
  id: 11,
  name: 'Met',
  filter: { include_archived: false, where: { op: 'eq', field: 'met', value: 'met' } },
}

function rows(count: number, from = 1) {
  return Array.from({ length: count }, (_, index) => contactRow(from + index))
}

beforeEach(() => window.localStorage.clear())
afterEach(() => window.localStorage.clear())

describe('a ?view= link', () => {
  it('sends no query until /views answers, then sends the view’s own tree', async () => {
    let answerViews: (response: Response) => void = () => undefined
    const viewsAnswered = new Promise<Response>((resolve) => (answerViews = resolve))
    const seen = mockApi((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/views') return viewsAnswered
      if (pathname === '/api/v1/contacts/query') return jsonResponse(contactPage(rows(2)))
      return undefined
    })

    await renderApp('/contacts?view=9')
    expect(screen.getByText('Loading contacts…')).toBeInTheDocument()
    // Give the table every chance to jump the gun with the bar's `{where: null}`.
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(queries(seen)).toEqual([])

    answerViews(jsonResponse([BEFORE_2020]))
    await screen.findByRole('table')
    expect(queries(seen)).toHaveLength(1)
    expect(JSON.stringify(lastQuery(seen).filter)).toContain('"field":"connected_on"')
  })

  it('says the view failed to load, and shows no contacts rather than a wider filter', async () => {
    let failing = true
    const seen = mockApi((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/views') {
        return failing
          ? jsonResponse({ detail: 'the database is locked' }, 503)
          : jsonResponse([BEFORE_2020])
      }
      if (pathname === '/api/v1/contacts/query') return jsonResponse(contactPage(rows(2)))
      return undefined
    })

    await renderApp('/contacts?view=9')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The saved view in this link could not be loaded')
    expect(alert).toHaveTextContent('the database is locked')
    expect(screen.queryByText('Loading contacts…')).toBeNull()
    expect(screen.queryByRole('table')).toBeNull()
    expect(queries(seen)).toEqual([])

    failing = false
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await screen.findByRole('table')
    expect(JSON.stringify(lastQuery(seen).filter)).toContain('"field":"connected_on"')
  })

  it('says so when the view no longer exists', async () => {
    mockApi((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/views') return jsonResponse([MET_ONLY])
      if (pathname === '/api/v1/contacts/query') return jsonResponse(contactPage(rows(2)))
      return undefined
    })
    await renderApp('/contacts?view=9')
    expect(await screen.findByText(/Saved view 9 no longer exists/)).toBeInTheDocument()
  })

  it('warns that touching a filter widens a view the bar cannot show', async () => {
    mockApi((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/views') return jsonResponse([BEFORE_2020, MET_ONLY])
      if (pathname === '/api/v1/contacts/query') return jsonResponse(contactPage(rows(2)))
      return undefined
    })
    const { router } = await renderApp('/contacts?view=9')
    await screen.findByRole('table')
    expect(screen.getByRole('note')).toHaveTextContent(
      '“Before 2020” filters on more than the controls above can show.',
    )

    // A view the bar says exactly carries no warning.
    await router.navigate({ to: '/contacts', search: { met: 'met', view: 11 } })
    await waitFor(() => expect(router.state.location.searchStr).toContain('view=11'))
    await screen.findByRole('table')
    expect(screen.queryByRole('note')).toBeNull()
  })
})

describe('a page past the end', () => {
  function serveTotal(total: number) {
    return mockApi((request, body) => {
      const { pathname } = new URL(request.url)
      if (pathname !== '/api/v1/contacts/query') return undefined
      const { offset, limit } = body as { offset: number; limit: number }
      const count = Math.max(0, Math.min(limit, total - offset))
      return jsonResponse(contactPage(rows(count, offset + 1), total))
    })
  }

  it('lands on the last page', async () => {
    const seen = serveTotal(120)
    const { router } = await renderApp('/contacts?page=9')
    await waitFor(() => expect(router.state.location.search).toMatchObject({ page: 3 }))
    await waitFor(() => expect(lastQuery(seen).offset).toBe(100))
    await waitFor(() =>
      expect(screen.getByRole('status', { name: 'Contacts shown' })).toHaveTextContent(
        '101–120 of 120',
      ),
    )
  })

  it('lands on page one when nothing matches, and replaces the bad entry', async () => {
    const seen = serveTotal(0)
    const { router } = await renderApp('/contacts?page=4')
    await waitFor(() => expect(router.state.location.search).not.toHaveProperty('page'))
    await waitFor(() => expect(lastQuery(seen).offset).toBe(0))
    await screen.findByText('No contacts match this filter.')
    // Back does not walk into `?page=4` again, only to be bounced forward.
    expect(router.history.length).toBe(1)
  })
})

describe('the row window', () => {
  const noop = () => undefined
  const NO_ACTIONS: RowActions = {
    setMet: noop,
    setDoNotContact: noop,
    setArchived: noop,
    toggleTag: noop,
    addToList: noop,
  }
  // Columns that render no router links, so the table can be mounted on its own.
  const columns: ColumnSpec[] = ['headline', 'current_company', 'met'].map((id) => {
    const column = COLUMNS_BY_ID.get(id as never)
    if (!column) throw new Error(`no such column: ${id}`)
    return column
  })
  const empty = new Set<number>()

  function table(scrollKey: string, page: ReturnType<typeof rows>) {
    return (
      <ContactsTable
        rows={page}
        scrollKey={scrollKey}
        columns={columns}
        sort={[]}
        onSort={noop}
        selectedIds={empty}
        everything={false}
        onSelect={noop}
        onSelectPage={noop}
        actions={NO_ACTIONS}
      />
    )
  }

  it('goes back to the top for a new page, and stays put for a refetch of this one', () => {
    const pageOne = rows(200)
    const view = render(table('page-1', pageOne))
    const viewport = screen.getByTestId('contacts-scroll')
    Object.defineProperty(viewport, 'scrollTop', { value: 5_400, writable: true })
    fireEvent.scroll(viewport)

    // A row action refetches the same page: new rows, same request.
    view.rerender(
      table(
        'page-1',
        pageOne.map((row) => ({ ...row, met: 'met' as const })),
      ),
    )
    expect(viewport.scrollTop).toBe(5_400)

    // Next: the same scroller, a different request.
    view.rerender(table('page-2', rows(200, 201)))
    expect(viewport.scrollTop).toBe(0)
    // And the window was measured again from the top, not left at row 150.
    expect(screen.queryByTestId('pad-top')).toBeNull()
  })
})
