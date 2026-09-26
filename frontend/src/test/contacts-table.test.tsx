import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { jsonResponse } from './fetch'
import { renderApp } from './render'
import {
  contactPage,
  contactRow,
  lastQuery,
  mockApi,
  queries,
  type SeenRequest,
} from './contacts-fixtures'

function page(count: number, total = count) {
  return contactPage(
    Array.from({ length: count }, (_, index) => contactRow(index + 1)),
    total,
  )
}

/** Answers the table's query with `body`, and nothing else. */
function serveTable(body: unknown, status = 200) {
  return mockApi((request) => {
    const { pathname } = new URL(request.url)
    if (pathname === '/api/v1/contacts/query') return jsonResponse(body, status)
    return undefined
  })
}

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(() => {
  window.localStorage.clear()
})

describe('contacts table', () => {
  it('asks the server for the filter, puts it in the URL, and the back button undoes it', async () => {
    const seen = serveTable(page(3))
    const { router } = await renderApp('/contacts')
    await screen.findByText('Bo Quill')

    fireEvent.change(screen.getByLabelText('Search contacts'), { target: { value: 'ferry' } })

    // The URL is where a shareable view lives (spec 10.1).
    await waitFor(() => expect(router.state.location.searchStr).toContain('q=ferry'))

    const query = await waitFor(() => {
      const latest = lastQuery(seen)
      expect(JSON.stringify(latest.filter)).toContain('ferry')
      return latest
    })
    // Free text is a server-side filter, not a client-side slice.
    expect(JSON.stringify(query.filter)).toContain('"op":"contains"')
    expect(query.offset).toBe(0)

    router.history.back()
    await waitFor(() => expect(router.state.location.searchStr).not.toContain('q=ferry'))
    await waitFor(() => expect(screen.getByLabelText('Search contacts')).toHaveValue(''))
  })

  it('restores the filter from the URL, so a pasted link shows the same table', async () => {
    // 52 matches, so page 2 exists; a total below the offset is clamped to the last page.
    const seen = serveTable(page(2, 52))
    await renderApp('/contacts?q=grebe&met=met&archived=true&sort=current_company%3Adesc&page=2')
    await screen.findByText('Bo Quill')

    const query = lastQuery(seen)
    expect(JSON.stringify(query.filter)).toContain('grebe')
    expect(JSON.stringify(query.filter)).toContain('"op":"eq","field":"met","value":"met"')
    expect(query.filter?.include_archived).toBe(true)
    expect(query.sort).toEqual([{ field: 'current_company', direction: 'desc' }])
    expect(query.offset).toBe(50)
    expect(screen.getByLabelText('Search contacts')).toHaveValue('grebe')
  })

  it('sorts from a column header and pages with the pager', async () => {
    const seen = serveTable(page(50, 120))
    const { router } = await renderApp('/contacts')
    await screen.findByRole('table')

    fireEvent.click(screen.getByRole('button', { name: /Company/ }))
    await waitFor(() =>
      expect(router.state.location.searchStr).toContain('sort=current_company%3Aasc'),
    )
    await waitFor(() =>
      expect(lastQuery(seen).sort).toEqual([{ field: 'current_company', direction: 'asc' }]),
    )

    // A second click turns it around rather than adding a second key.
    fireEvent.click(screen.getByRole('button', { name: /Company/ }))
    await waitFor(() =>
      expect(lastQuery(seen).sort).toEqual([{ field: 'current_company', direction: 'desc' }]),
    )

    expect(screen.getByRole('button', { name: /Previous/ })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /Next/ }))
    await waitFor(() => expect(router.state.location.searchStr).toContain('page=2'))
    await waitFor(() => expect(lastQuery(seen).offset).toBe(50))
    expect(screen.getByRole('status', { name: 'Contacts shown' })).toHaveTextContent(
      '51–100 of 120',
    )
  })

  it('remembers the columns the picker chose', async () => {
    serveTable(page(2))
    const first = await renderApp('/contacts')
    await screen.findByText('Bo Quill')
    expect(screen.getByRole('columnheader', { name: /Location/ })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Columns/ }))
    fireEvent.click(await screen.findByRole('menuitemcheckbox', { name: 'Location' }))
    await waitFor(() => expect(screen.queryByRole('columnheader', { name: /Location/ })).toBeNull())
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })

    // A reload is a fresh app against the same storage.
    first.unmount()
    serveTable(page(2))
    await renderApp('/contacts')
    await screen.findByText('Bo Quill')
    expect(screen.queryByRole('columnheader', { name: /Location/ })).toBeNull()
    expect(screen.getByRole('columnheader', { name: /Headline/ })).toBeInTheDocument()
  })

  it('saves a view on the server and applies it again', async () => {
    const stored: Array<Record<string, unknown>> = []
    const seen = mockApi((request, body) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/query') return jsonResponse(page(2))
      if (pathname === '/api/v1/views' && request.method === 'GET') return jsonResponse(stored)
      if (pathname === '/api/v1/views' && request.method === 'POST') {
        const sent = body as Record<string, unknown>
        const view = {
          id: 7,
          name: sent.name,
          columns: sent.columns,
          sort: sent.sort,
          filter: sent.filter,
          created_at: '2026-09-21T00:00:00Z',
          updated_at: '2026-09-21T00:00:00Z',
        }
        stored.push(view)
        return jsonResponse(view, 201)
      }
      return undefined
    })

    const { router } = await renderApp('/contacts?q=ferry')
    await screen.findByText('Bo Quill')

    fireEvent.click(screen.getByRole('button', { name: /Views/ }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Save this view…' }))
    const dialog = within(await screen.findByRole('dialog'))
    fireEvent.change(dialog.getByLabelText('Name'), { target: { value: 'Ferry people' } })
    fireEvent.click(dialog.getByRole('button', { name: 'Save view' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    const created = seen.find((entry) => entry.method === 'POST' && entry.path === '/api/v1/views')
      ?.body as { name: string; columns: string[]; filter: { where: unknown } }
    expect(created.name).toBe('Ferry people')
    expect(created.columns).toContain('name')
    expect(JSON.stringify(created.filter)).toContain('ferry')

    // Saving applies it, so the URL now names the view.
    await waitFor(() => expect(router.state.location.searchStr).toContain('view=7'))

    // Move away from the filter, then come back through the saved view.
    fireEvent.click(screen.getByRole('button', { name: /Clear filters/ }))
    await waitFor(() => expect(router.state.location.searchStr).not.toContain('q=ferry'))

    fireEvent.click(screen.getByRole('button', { name: /Views/ }))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Ferry people/ }))
    await waitFor(() => expect(router.state.location.searchStr).toContain('q=ferry'))
    await waitFor(() => expect(router.state.location.searchStr).toContain('view=7'))
  })

  it('saves the filter the table is running, not the one the bar can show', async () => {
    // A stored tree the filter bar has no control for: it can show no date
    // comparison, so `buildFilter` on the bar would come back empty.
    const stored = {
      id: 9,
      name: 'Before 2020',
      columns: ['name', 'connected_on'],
      sort: [{ field: 'connected_on', direction: 'asc' }],
      filter: {
        include_archived: false,
        where: { op: 'lt', field: 'connected_on', value: '2020-01-01' },
      },
      created_at: '2026-09-01T00:00:00Z',
      updated_at: '2026-09-01T00:00:00Z',
    }
    const seen = mockApi((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/query') return jsonResponse(page(2))
      if (pathname === '/api/v1/views' && request.method === 'GET') return jsonResponse([stored])
      if (pathname === '/api/v1/views' && request.method === 'POST') {
        return jsonResponse({ ...stored, id: 10, name: 'Copy' }, 201)
      }
      return undefined
    })

    await renderApp('/contacts?view=9')
    await screen.findByRole('table')

    // The applied view's own tree is what the table runs.
    await waitFor(() =>
      expect(JSON.stringify(lastQuery(seen).filter)).toContain('"field":"connected_on"'),
    )

    fireEvent.click(screen.getByRole('button', { name: /Before 2020/ }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Save this view…' }))
    const dialog = within(await screen.findByRole('dialog'))
    fireEvent.change(dialog.getByLabelText('Name'), { target: { value: 'Copy' } })
    fireEvent.click(dialog.getByRole('button', { name: 'Save view' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    const created = seen.find((entry) => entry.method === 'POST' && entry.path === '/api/v1/views')
      ?.body as { filter: unknown; sort: unknown }
    // Saving "everybody" here would have thrown the date away silently.
    expect(JSON.stringify(created.filter)).toContain('"field":"connected_on"')
    expect(created.sort).toEqual([{ field: 'connected_on', direction: 'asc' }])
  })

  it('shows loading, then an empty state that reads the filter back', async () => {
    serveTable(contactPage([], 0, 'contacts matching “nobody”'))
    await renderApp('/contacts?q=nobody')
    expect(screen.getByText('Loading contacts…')).toBeInTheDocument()
    expect(await screen.findByText('No contacts match this filter.')).toBeInTheDocument()
    expect(screen.getByText('contacts matching “nobody”')).toBeInTheDocument()
  })

  it('shows an error with a way to retry, not a blank table', async () => {
    let attempts = 0
    mockApi((request) => {
      const { pathname } = new URL(request.url)
      if (pathname !== '/api/v1/contacts/query') return undefined
      attempts += 1
      return attempts === 1
        ? jsonResponse({ detail: 'the database is locked' }, 500)
        : jsonResponse(page(1))
    })
    await renderApp('/contacts')

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/Contacts could not be loaded/)
    expect(alert).toHaveTextContent(/the database is locked/)

    fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('Bo Quill')).toBeInTheDocument()
  })
})

describe('bulk actions', () => {
  /** A table with one contact picked and the bulk bar showing. */
  async function pickOne(handler: Parameters<typeof mockApi>[0]) {
    const seen = mockApi((request, body) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/query') return jsonResponse(page(3, 3))
      return handler(request, body)
    })
    await renderApp('/contacts')
    await screen.findByText('Bo Quill')
    fireEvent.click(screen.getByLabelText('Select Bo Quill'))
    await screen.findByRole('region', { name: 'Bulk actions' })
    return seen
  }

  it('confirms the count the server gives, then applies it with that token', async () => {
    const seen = await pickOne((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/bulk/count') {
        return jsonResponse({
          count: 1,
          describe: '1 contact by id',
          token: 'token-one',
          expires_at: '2026-09-21T12:05:00Z',
        })
      }
      if (pathname === '/api/v1/contacts/bulk') return jsonResponse({ affected: 1 })
      return undefined
    })

    fireEvent.click(screen.getByRole('button', { name: /Bulk actions/ }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Mark as met' }))

    const dialog = within(await screen.findByRole('dialog'))
    // The number is the thing being confirmed, and it comes from the server.
    expect(await dialog.findByText(/1 contact by id/)).toBeInTheDocument()
    fireEvent.click(dialog.getByRole('button', { name: /Apply to 1/ }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    // A success reads as one, and is not in the failure box.
    const done = await screen.findByText('1 contact updated.')
    expect(done).toHaveAttribute('role', 'status')
    expect(screen.queryByRole('alert')).toBeNull()

    const count = seen.find((entry) => entry.path === '/api/v1/contacts/bulk/count')
    const apply = seen.find((entry) => entry.path === '/api/v1/contacts/bulk')
    // The token binds the value too, so the count has to carry it (spec 14.1).
    expect(count?.body).toMatchObject({ action: 'set_met', value: 'met' })
    expect(apply?.body).toMatchObject({ action: 'set_met', value: 'met', token: 'token-one' })
  })

  it('explains a count that moved underneath, and offers to count again', async () => {
    let counts = 0
    let applies = 0
    const seen: SeenRequest[] = await pickOne((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/bulk/count') {
        counts += 1
        return jsonResponse({
          count: counts === 1 ? 3 : 2,
          describe: 'contacts by id',
          token: `token-${counts}`,
          expires_at: '2026-09-21T12:05:00Z',
        })
      }
      if (pathname === '/api/v1/contacts/bulk') {
        applies += 1
        return applies === 1
          ? jsonResponse({ detail: 'count mismatch', expected_count: 3, actual_count: 2 }, 409)
          : jsonResponse({ affected: 2 })
      }
      return undefined
    })

    fireEvent.click(screen.getByRole('button', { name: /Bulk actions/ }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Archive' }))

    const dialog = within(await screen.findByRole('dialog'))
    fireEvent.click(await dialog.findByRole('button', { name: /Apply to 3/ }))

    // The refusal is the feature: it says what happened and that nothing changed.
    const alert = await dialog.findByRole('alert')
    expect(alert).toHaveTextContent(/now matches 2 contacts, not the 3 you confirmed/)
    expect(alert).toHaveTextContent(/Nothing was changed/)

    fireEvent.click(dialog.getByRole('button', { name: 'Count again' }))
    fireEvent.click(await dialog.findByRole('button', { name: /Apply to 2/ }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(await screen.findByText('2 contacts updated.')).toBeInTheDocument()

    const tokens = seen
      .filter((entry) => entry.path === '/api/v1/contacts/bulk')
      .map((entry) => (entry.body as { token: string }).token)
    expect(tokens).toEqual(['token-1', 'token-2'])
  })

  it('settles the do-not-contact reason before counting, and sends the same one back', async () => {
    const counted: Array<Record<string, unknown>> = []
    const seen = await pickOne((request, body) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/bulk/count') {
        counted.push(body as Record<string, unknown>)
        return jsonResponse({
          count: 1,
          describe: '1 contact by id',
          token: `token-${counted.length}`,
          expires_at: '2026-09-21T12:05:00Z',
        })
      }
      if (pathname === '/api/v1/contacts/bulk') return jsonResponse({ affected: 1 })
      return undefined
    })

    fireEvent.click(screen.getByRole('button', { name: /Bulk actions/ }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Set do not contact' }))

    const dialog = within(await screen.findByRole('dialog'))
    // The reason is part of what the token binds, so nothing is counted yet.
    expect(dialog.getByLabelText('Reason (optional)')).toBeInTheDocument()
    expect(counted).toHaveLength(0)

    fireEvent.change(dialog.getByLabelText('Reason (optional)'), {
      target: { value: 'asked not to be contacted' },
    })
    fireEvent.click(dialog.getByRole('button', { name: 'Count the selection' }))

    await waitFor(() => expect(counted).toHaveLength(1))
    expect(counted[0]).toMatchObject({
      action: 'set_do_not_contact',
      value: true,
      reason: 'asked not to be contacted',
    })
    expect(await dialog.findByText(/Reason: asked not to be contacted/)).toBeInTheDocument()

    // Changing it goes back through the count, because the old token binds the old reason.
    fireEvent.click(dialog.getByRole('button', { name: 'Edit reason' }))
    fireEvent.change(dialog.getByLabelText('Reason (optional)'), {
      target: { value: 'left the industry' },
    })
    fireEvent.click(dialog.getByRole('button', { name: 'Count the selection' }))
    await waitFor(() => expect(counted).toHaveLength(2))
    expect(counted[1]).toMatchObject({ reason: 'left the industry' })

    fireEvent.click(await dialog.findByRole('button', { name: /Apply to 1/ }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    const applied = seen.find((entry) => entry.path === '/api/v1/contacts/bulk')?.body
    // The token and the reason travel together: the second count's token, the
    // second count's reason.
    expect(applied).toMatchObject({
      action: 'set_do_not_contact',
      value: true,
      reason: 'left the industry',
      token: 'token-2',
    })
  })

  it('says an expired confirmation expired, and that nothing was changed', async () => {
    await pickOne((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/bulk/count') {
        return jsonResponse({
          count: 1,
          describe: '1 contact by id',
          token: 'stale',
          expires_at: '2026-09-21T12:05:00Z',
        })
      }
      if (pathname === '/api/v1/contacts/bulk') {
        return jsonResponse({ detail: 'the confirmation expired', reason: 'expired' }, 409)
      }
      return undefined
    })

    fireEvent.click(screen.getByRole('button', { name: /Bulk actions/ }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Archive' }))
    const dialog = within(await screen.findByRole('dialog'))
    fireEvent.click(await dialog.findByRole('button', { name: /Apply to 1/ }))

    const alert = await dialog.findByRole('alert')
    expect(alert).toHaveTextContent(/expired/)
    expect(alert).toHaveTextContent(/Nothing was changed/)
    expect(dialog.getByRole('button', { name: 'Count again' })).toBeEnabled()
  })

  it('reads a schema refusal out of its validation-error list, not as a bare 422', async () => {
    await pickOne((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/bulk/count') {
        return jsonResponse(
          {
            detail: [
              {
                type: 'too_long',
                loc: ['body', 'selection', 'ids'],
                msg: 'List should have at most 1000 items after validation, not 1200',
              },
            ],
          },
          422,
        )
      }
      return undefined
    })

    fireEvent.click(screen.getByRole('button', { name: /Bulk actions/ }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Archive' }))
    const dialog = within(await screen.findByRole('dialog'))
    const alert = await dialog.findByRole('alert')
    expect(alert).toHaveTextContent('List should have at most 1000 items after validation')
    expect(alert).not.toHaveTextContent('count: 422')
  })

  it('selects the whole filter, and sends the filter rather than a list of ids', async () => {
    const seen = await pickOne((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/bulk/count') {
        return jsonResponse({
          count: 3,
          describe: 'every live contact',
          token: 'by-filter',
          expires_at: '2026-09-21T12:05:00Z',
        })
      }
      return undefined
    })

    fireEvent.click(screen.getByRole('button', { name: /Select all 3 matching this filter/ }))
    fireEvent.click(screen.getByRole('button', { name: /Bulk actions/ }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Mark as skipped' }))
    await screen.findByRole('dialog')

    await waitFor(() => {
      const count = seen.find((entry) => entry.path === '/api/v1/contacts/bulk/count')
      expect(
        (count?.body as { selection: { ids: unknown; filter: unknown } }).selection.ids,
      ).toBeNull()
      expect((count?.body as { selection: { filter: unknown } }).selection.filter).not.toBeNull()
    })
  })
})

describe('row actions', () => {
  it('offers every action spec 10.1 lists, and disables the two with no API yet', async () => {
    mockApi((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/query') return jsonResponse(page(1))
      return undefined
    })
    await renderApp('/contacts')
    await screen.findByText('Bo Quill')

    fireEvent.click(screen.getByRole('button', { name: /Actions for Bo Quill/ }))
    const menu = within(await screen.findByRole('menu'))

    expect(menu.getByRole('menuitem', { name: /Tag…/ })).toBeInTheDocument()
    expect(menu.getByRole('menuitem', { name: /Set met/ })).toBeInTheDocument()
    expect(menu.getByRole('menuitem', { name: /Set do not contact/ })).toBeInTheDocument()
    expect(menu.getByRole('menuitem', { name: /Open on LinkedIn/ })).toHaveAttribute(
      'href',
      'https://www.linkedin.com/in/person-1',
    )
    expect(menu.getByRole('menuitem', { name: /Open in Gmail/ })).toHaveAttribute(
      'href',
      expect.stringContaining('person-1%40example.test'),
    )
    expect(menu.getByRole('menuitem', { name: /Add to list…/ })).not.toHaveAttribute(
      'aria-disabled',
      'true',
    )
    // The one action whose endpoint genuinely does not exist: unavailable with
    // a reason, never a dead click. `/linkedin/pins` is not in the schema.
    const pin = menu.getByRole('menuitem', { name: /Pin for enrichment/ })
    expect(pin).toHaveAttribute('aria-disabled', 'true')
    expect(pin).toHaveTextContent('not built yet')
  })

  it('adds a contact to a static list, and will not offer a smart one', async () => {
    const seen = mockApi((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/query') return jsonResponse(page(1))
      if (pathname === '/api/v1/lists' && request.method === 'GET') {
        return jsonResponse([
          {
            id: 3,
            name: 'First 100',
            kind: 'static',
            filter: null,
            member_count: 12,
            created_at: '2026-09-01T00:00:00Z',
            updated_at: '2026-09-01T00:00:00Z',
          },
          {
            id: 4,
            name: 'Validated',
            kind: 'smart',
            filter: { include_archived: false, where: { op: 'eq', field: 'met', value: 'met' } },
            member_count: 40,
            created_at: '2026-09-01T00:00:00Z',
            updated_at: '2026-09-01T00:00:00Z',
          },
        ])
      }
      if (pathname === '/api/v1/lists/3/members') return jsonResponse({ added: 1 }, 201)
      return undefined
    })
    await renderApp('/contacts')
    await screen.findByText('Bo Quill')

    fireEvent.click(screen.getByRole('button', { name: /Actions for Bo Quill/ }))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Add to list…/ }))

    // A smart list's membership is its filter, so the API refuses members for
    // one; it is shown unavailable rather than offered and refused.
    const smart = await screen.findByRole('menuitem', { name: /Validated/ })
    expect(smart).toHaveAttribute('aria-disabled', 'true')

    fireEvent.click(screen.getByRole('menuitem', { name: /First 100/ }))
    await waitFor(() => {
      const added = seen.find((entry) => entry.path === '/api/v1/lists/3/members')
      expect(added?.body).toEqual({ contact_ids: [1] })
    })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('sets met through a PATCH and refreshes the table', async () => {
    const seen = mockApi((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/query') return jsonResponse(page(1))
      if (pathname === '/api/v1/contacts/1') return jsonResponse({ id: 1 })
      return undefined
    })
    await renderApp('/contacts')
    await screen.findByText('Bo Quill')

    fireEvent.click(screen.getByRole('button', { name: /Actions for Bo Quill/ }))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Set met/ }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Met' }))

    await waitFor(() => {
      const patch = seen.find(
        (entry) => entry.method === 'PATCH' && entry.path === '/api/v1/contacts/1',
      )
      expect(patch?.body).toEqual({ met: 'met' })
    })
    // The page is asked for again, so the row shows the new value.
    await waitFor(() => expect(queries(seen).length).toBeGreaterThan(1))
  })
})
