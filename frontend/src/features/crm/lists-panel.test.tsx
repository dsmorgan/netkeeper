/**
 * Lists: CRUD, membership, and the smart list's live filter.
 *
 * The round-trip assertion is here rather than in the builder's own tests
 * because this is where the tree leaves the browser: the body of the PATCH
 * must be the same tree the builder showed, with nothing added or dropped on
 * the way.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse } from '@/test/fetch'

import { ListsPanel } from './lists-panel'
import { mockApi, renderWithClient, requestsTo } from './harness'

const STATIC_LIST = {
  id: 1,
  name: 'First 100',
  kind: 'static',
  filter: null,
  member_count: 2,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const SMART_LIST = {
  id: 2,
  name: 'Warm engineers',
  kind: 'smart',
  filter: { where: { op: 'has_email' }, include_archived: false },
  member_count: 40,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const MEMBERS = {
  items: [
    {
      id: 11,
      first_name: 'Ada',
      last_name: 'Quill',
      preferred_name: 'Ada',
      headline: null,
      current_title: 'Engineer',
      current_company: 'Northwind Example',
      met: 'met',
      li_url: null,
    },
    {
      id: 12,
      first_name: 'Bo',
      last_name: 'Marsh',
      preferred_name: 'Bo',
      headline: null,
      current_title: null,
      current_company: null,
      met: 'unknown',
      li_url: null,
    },
  ],
  total: 2,
}

function routes(overrides: Record<string, () => Response> = {}) {
  return {
    'GET /api/v1/tags': () => jsonResponse([]),
    'GET /api/v1/lists': () => jsonResponse([STATIC_LIST, SMART_LIST]),
    'GET /api/v1/lists/1/members': () => jsonResponse(MEMBERS),
    'GET /api/v1/lists/2/members': () => jsonResponse(MEMBERS),
    'POST /api/v1/contacts/query': () =>
      jsonResponse({ items: [], total: 40, describe: 'has email' }),
    ...overrides,
  }
}

async function openList(name: string) {
  fireEvent.click(await screen.findByRole('button', { name: new RegExp(name) }))
}

describe('the lists page', () => {
  it('shows every list with its live count', async () => {
    mockApi(routes())
    renderWithClient(<ListsPanel />)
    expect(await screen.findByText('First 100')).toBeInTheDocument()
    expect(screen.getByText('Warm engineers')).toBeInTheDocument()
    expect(screen.getByText('40')).toBeInTheDocument()
  })

  it('says so plainly when there are none', async () => {
    mockApi(routes({ 'GET /api/v1/lists': () => jsonResponse([]) }))
    renderWithClient(<ListsPanel />)
    expect(await screen.findByText('No lists yet')).toBeInTheDocument()
  })

  it('reports a failed load', async () => {
    mockApi(routes({ 'GET /api/v1/lists': () => jsonResponse({ detail: 'no such user' }, 500) }))
    renderWithClient(<ListsPanel />)
    expect(await screen.findByText('no such user')).toBeInTheDocument()
  })

  it('shows a loading state first', () => {
    mockApi(routes({ 'GET /api/v1/lists': () => new Promise<Response>(() => {}) as never }))
    renderWithClient(<ListsPanel />)
    expect(screen.getByText('Loading lists…')).toBeInTheDocument()
  })

  it('creates a static list with no filter and a smart one with an empty filter', async () => {
    const seen = mockApi(routes({ 'POST /api/v1/lists': () => jsonResponse(STATIC_LIST, 201) }))
    renderWithClient(<ListsPanel />)
    await screen.findByText('First 100')

    fireEvent.change(screen.getByLabelText('New list'), { target: { value: 'Second 100' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(requestsTo(seen, 'POST', '/api/v1/lists')).toHaveLength(1))
    expect(requestsTo(seen, 'POST', '/api/v1/lists')[0]?.body).toEqual({
      name: 'Second 100',
      kind: 'static',
      filter: null,
    })

    fireEvent.change(screen.getByLabelText('New list'), { target: { value: 'Smart one' } })
    fireEvent.change(screen.getByLabelText('Kind'), { target: { value: 'smart' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(requestsTo(seen, 'POST', '/api/v1/lists')).toHaveLength(2))
    expect(requestsTo(seen, 'POST', '/api/v1/lists')[1]?.body).toEqual({
      name: 'Smart one',
      kind: 'smart',
      filter: { where: null, include_archived: false },
    })
  })
})

describe('a static list', () => {
  it('lists its members and lets one go', async () => {
    const seen = mockApi(
      routes({ 'DELETE /api/v1/lists/1/members/11': () => new Response(null, { status: 204 }) }),
    )
    renderWithClient(<ListsPanel />)
    await openList('First 100')

    expect(await screen.findByText('Ada Quill')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Remove Ada Quill' }))
    await waitFor(() =>
      expect(requestsTo(seen, 'DELETE', '/api/v1/lists/1/members/11')).toHaveLength(1),
    )
  })

  it('adds contacts by id', async () => {
    const seen = mockApi(
      routes({ 'POST /api/v1/lists/1/members': () => jsonResponse({ added: 2 }, 201) }),
    )
    renderWithClient(<ListsPanel />)
    await openList('First 100')

    fireEvent.change(await screen.findByLabelText('Add contacts by id'), {
      target: { value: '12, 40' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    await waitFor(() =>
      expect(requestsTo(seen, 'POST', '/api/v1/lists/1/members')[0]?.body).toEqual({
        contact_ids: [12, 40],
      }),
    )
  })

  it('offers no export button, and says why, because list_member does not compile', async () => {
    mockApi(routes())
    renderWithClient(<ListsPanel />)
    await openList('First 100')

    expect(await screen.findByText('No export button here yet')).toBeInTheDocument()
    expect(screen.getByText(/issue #73/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Export/ })).toBeNull()
  })

  it('offers no filter builder, because membership is explicit', async () => {
    mockApi(routes())
    renderWithClient(<ListsPanel />)
    await openList('First 100')
    expect(await screen.findByText('Membership')).toBeInTheDocument()
    expect(screen.queryByText('No conditions: this selects every contact.')).toBeNull()
  })
})

describe('a smart list', () => {
  it('offers the export dialog, because its filter is the selection', async () => {
    mockApi(routes())
    renderWithClient(<ListsPanel />)
    await openList('Warm engineers')
    expect(await screen.findByRole('button', { name: /^Export/ })).toBeInTheDocument()
  })

  it('sends the tree the builder shows, unchanged', async () => {
    const seen = mockApi(routes({ 'PATCH /api/v1/lists/2': () => jsonResponse(SMART_LIST) }))
    renderWithClient(<ListsPanel />)
    await openList('Warm engineers')

    // Wrap the stored `has_email` in a company condition of our own.
    const filterCard = (await screen.findByText('Filter')).closest('[data-slot="card"]')
    const scoped = within(filterCard as HTMLElement)
    fireEvent.click(scoped.getByRole('button', { name: 'Remove has an email' }))
    fireEvent.click(scoped.getByRole('button', { name: /^Add a condition/ }))
    fireEvent.click(document.querySelector('[data-op="contains"]') as HTMLElement)

    const row = document.querySelector('[data-op="contains"][data-slot="filter-node"]')
    const inRow = within(row as HTMLElement)
    fireEvent.change(inRow.getByLabelText('Field'), { target: { value: 'current_company' } })
    fireEvent.change(inRow.getByLabelText('Value'), { target: { value: 'northwind' } })

    fireEvent.click(scoped.getByRole('button', { name: 'Save filter' }))

    await waitFor(() => expect(requestsTo(seen, 'PATCH', '/api/v1/lists/2')).toHaveLength(1))
    expect(requestsTo(seen, 'PATCH', '/api/v1/lists/2')[0]?.body).toEqual({
      filter: {
        include_archived: false,
        where: { op: 'contains', field: 'current_company', value: 'northwind' },
      },
    })
  })

  it('asks for membership again after any list write, never reusing the old page', async () => {
    // The mechanism is prefix invalidation: the members key sits under
    // ['lists'], and every write in this feature invalidates that prefix. The
    // list's `updated_at` is deliberately NOT part of the key, so this fixture
    // holds it still — a backend does not have to bump it for an identical
    // save, and membership must be re-read either way.
    const seen = mockApi(routes({ 'PATCH /api/v1/lists/2': () => jsonResponse(SMART_LIST) }))
    renderWithClient(<ListsPanel />)
    await openList('Warm engineers')
    await screen.findByText('Ada Quill')
    const before = requestsTo(seen, 'GET', '/api/v1/lists/2/members').length
    expect(before).toBeGreaterThan(0)

    const filterCard = (await screen.findByText('Filter')).closest('[data-slot="card"]')
    const scoped = within(filterCard as HTMLElement)
    fireEvent.click(scoped.getByRole('button', { name: 'Save filter' }))

    await waitFor(() =>
      expect(requestsTo(seen, 'GET', '/api/v1/lists/2/members').length).toBeGreaterThan(before),
    )
  })

  it('refuses to save a filter the server would reject, and says what is missing', async () => {
    mockApi(routes())
    renderWithClient(<ListsPanel />)
    await openList('Warm engineers')

    const filterCard = (await screen.findByText('Filter')).closest('[data-slot="card"]')
    const scoped = within(filterCard as HTMLElement)
    fireEvent.click(scoped.getByRole('button', { name: 'Remove has an email' }))
    fireEvent.click(scoped.getByRole('button', { name: /^Add a condition/ }))
    fireEvent.click(document.querySelector('[data-op="contains"]') as HTMLElement)

    expect(scoped.getByText(/needs something to look for/)).toBeInTheDocument()
    expect(scoped.getByRole('button', { name: 'Save filter' })).toBeDisabled()
  })
})

describe('bulk actions', () => {
  function countRoutes(body: Record<string, unknown>) {
    return { 'POST /api/v1/contacts/bulk/count': () => jsonResponse(body) }
  }

  it('confirms a count before it does anything', async () => {
    const seen = mockApi(
      routes({
        ...countRoutes({
          count: 40,
          describe: 'has email',
          token: 'token-1',
          expires_at: '2030-01-01T00:00:00Z',
        }),
        'POST /api/v1/contacts/bulk': () => jsonResponse({ affected: 40 }),
      }),
    )
    renderWithClient(<ListsPanel />)
    await openList('Warm engineers')

    fireEvent.click(await screen.findByRole('button', { name: 'Count first' }))
    expect(await screen.findByText(/This affects/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Apply to 40' }))
    await waitFor(() => expect(requestsTo(seen, 'POST', '/api/v1/contacts/bulk')).toHaveLength(1))
    expect(requestsTo(seen, 'POST', '/api/v1/contacts/bulk')[0]?.body).toMatchObject({
      action: 'archive',
      token: 'token-1',
    })
    expect(await screen.findByText('Applied to 40 contacts.')).toBeInTheDocument()
  })

  it('applies the selection the token was minted for, not the one on screen now', async () => {
    // A static list's selection is the ids of a members query, and any list
    // write re-reads it. The token is bound to the ids that were counted, so
    // the action has to carry those ids — not whatever the query holds by the
    // time the button is clicked.
    let members = {
      items: [MEMBERS.items[0], MEMBERS.items[1]],
      total: 2,
    }
    const seen = mockApi(
      routes({
        'GET /api/v1/lists/1/members': () => jsonResponse(members),
        ...countRoutes({
          count: 2,
          describe: '2 contacts',
          token: 'token-for-11-12',
          expires_at: '2030-01-01T00:00:00Z',
        }),
        'POST /api/v1/lists/1/members': () => jsonResponse({ added: 1 }, 201),
        'POST /api/v1/contacts/bulk': () => jsonResponse({ affected: 2 }),
      }),
    )
    renderWithClient(<ListsPanel />)
    await openList('First 100')
    await screen.findByText('Ada Quill')

    fireEvent.click(await screen.findByRole('button', { name: 'Count first' }))
    await screen.findByRole('button', { name: 'Apply to 2' })
    expect(requestsTo(seen, 'POST', '/api/v1/contacts/bulk/count')[0]?.body).toMatchObject({
      selection: { ids: [11, 12] },
    })

    // The list gains a member while the confirmation is on screen.
    members = {
      items: [...MEMBERS.items, { ...MEMBERS.items[0]!, id: 13, preferred_name: 'Cy' }],
      total: 3,
    }
    fireEvent.change(screen.getByLabelText('Add contacts by id'), { target: { value: '13' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(screen.getByText('Added 1 contacts.')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Apply to 2' }))
    await waitFor(() => expect(requestsTo(seen, 'POST', '/api/v1/contacts/bulk')).toHaveLength(1))
    expect(requestsTo(seen, 'POST', '/api/v1/contacts/bulk')[0]?.body).toMatchObject({
      token: 'token-for-11-12',
      selection: { ids: [11, 12] },
    })
  })

  it('says the selection moved, and nothing else, on a count mismatch', async () => {
    let count = 40
    const seen = mockApi(
      routes({
        'POST /api/v1/contacts/bulk/count': () =>
          jsonResponse({
            count,
            describe: 'has email',
            token: `token-${count}`,
            expires_at: '2030-01-01T00:00:00Z',
          }),
        'POST /api/v1/contacts/bulk': () => {
          count = 38
          return jsonResponse(
            { detail: 'count mismatch', expected_count: 40, actual_count: 38 },
            409,
          )
        },
      }),
    )
    renderWithClient(<ListsPanel />)
    await openList('Warm engineers')

    fireEvent.click(await screen.findByRole('button', { name: 'Count first' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Apply to 40' }))

    expect(
      await screen.findByText(/The selection changed while the confirmation was open/),
    ).toBeInTheDocument()
    expect(screen.getByText('Nothing was changed')).toBeInTheDocument()
    expect(screen.queryByText('Could not apply the action')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Count again' }))
    await screen.findByRole('button', { name: 'Apply to 38' })
    await waitFor(() =>
      expect(requestsTo(seen, 'POST', '/api/v1/contacts/bulk/count')).toHaveLength(2),
    )
  })

  it('says a confirmation expired, and does not claim the selection moved', async () => {
    mockApi(
      routes({
        ...countRoutes({
          count: 40,
          describe: 'has email',
          token: 'token-1',
          expires_at: '2030-01-01T00:00:00Z',
        }),
        'POST /api/v1/contacts/bulk': () =>
          jsonResponse({ detail: 'the confirmation expired', reason: 'expired' }, 409),
      }),
    )
    renderWithClient(<ListsPanel />)
    await openList('Warm engineers')

    fireEvent.click(await screen.findByRole('button', { name: 'Count first' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Apply to 40' }))

    expect(await screen.findByText(/This confirmation expired/)).toBeInTheDocument()
    expect(screen.queryByText(/The selection changed/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Count again' })).toBeEnabled()
  })

  it('offers a fresh count when the token itself is refused', async () => {
    mockApi(
      routes({
        ...countRoutes({
          count: 40,
          describe: 'has email',
          token: 'token-1',
          expires_at: '2030-01-01T00:00:00Z',
        }),
        'POST /api/v1/contacts/bulk': () =>
          jsonResponse({ detail: 'the token is for another selection', reason: 'selection' }, 422),
      }),
    )
    renderWithClient(<ListsPanel />)
    await openList('Warm engineers')

    fireEvent.click(await screen.findByRole('button', { name: 'Count first' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Apply to 40' }))

    expect(await screen.findByText(/another selection/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Count again' })).toBeEnabled()
  })

  it('does not offer to apply an action to nobody', async () => {
    mockApi(
      routes({
        ...countRoutes({
          count: 0,
          describe: 'has email',
          token: 'token-0',
          expires_at: '2030-01-01T00:00:00Z',
        }),
      }),
    )
    renderWithClient(<ListsPanel />)
    await openList('Warm engineers')

    fireEvent.click(await screen.findByRole('button', { name: 'Count first' }))
    expect(await screen.findByRole('button', { name: 'Nothing to apply' })).toBeDisabled()
  })
})
