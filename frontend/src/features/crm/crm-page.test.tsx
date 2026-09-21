/**
 * The `/lists` page and its third tab, saved views.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse } from '@/test/fetch'

import { CrmPage } from './crm-page'
import { mockApi, renderWithClient, requestsTo } from './harness'
import { SavedViewsPanel } from './saved-views-panel'

const VIEW = {
  id: 3,
  name: 'Untriaged engineers',
  columns: ['preferred_name', 'last_name', 'current_title'],
  sort: [{ field: 'last_name', direction: 'asc' }],
  filter: { where: { op: 'has_email' }, include_archived: false },
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

function routes(overrides: Record<string, () => Response> = {}) {
  return {
    'GET /api/v1/tags': () => jsonResponse([]),
    'GET /api/v1/autotag-rules': () => jsonResponse([]),
    'GET /api/v1/lists': () => jsonResponse([]),
    'GET /api/v1/views': () => jsonResponse([VIEW]),
    'POST /api/v1/contacts/query': () =>
      jsonResponse({ items: [], total: 0, describe: 'all contacts' }),
    ...overrides,
  }
}

describe('the lists page', () => {
  it('keeps lists, tags and rules, and saved views on one page', async () => {
    mockApi(routes())
    renderWithClient(<CrmPage />)

    expect(await screen.findByText('No lists yet')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: 'Tags and rules' }))
    expect(await screen.findByText('No tags yet')).toBeInTheDocument()
    expect(screen.getByText('Auto-tag rules')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: 'Saved views' }))
    expect(await screen.findByText('Untriaged engineers')).toBeInTheDocument()
  })
})

describe('saved views', () => {
  it('says so plainly when there are none', async () => {
    mockApi(routes({ 'GET /api/v1/views': () => jsonResponse([]) }))
    renderWithClient(<SavedViewsPanel />)
    expect(await screen.findByText('No saved views yet')).toBeInTheDocument()
  })

  it('reports a failed load', async () => {
    mockApi(routes({ 'GET /api/v1/views': () => jsonResponse({ detail: 'unreadable' }, 500) }))
    renderWithClient(<SavedViewsPanel />)
    expect(await screen.findByText('unreadable')).toBeInTheDocument()
  })

  it('shows a loading state first', () => {
    mockApi(routes({ 'GET /api/v1/views': () => new Promise<Response>(() => {}) as never }))
    renderWithClient(<SavedViewsPanel />)
    expect(screen.getByText('Loading saved views…')).toBeInTheDocument()
  })

  it('saves a new view with its columns, sort, and filter', async () => {
    const seen = mockApi(routes({ 'POST /api/v1/views': () => jsonResponse(VIEW, 201) }))
    renderWithClient(<SavedViewsPanel />)
    await screen.findByText('Untriaged engineers')

    fireEvent.click(screen.getByRole('button', { name: 'New view' }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Recent connections' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add a sort' }))
    fireEvent.change(screen.getByLabelText('Sort direction 1'), { target: { value: 'desc' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save view' }))

    await waitFor(() => expect(requestsTo(seen, 'POST', '/api/v1/views')).toHaveLength(1))
    expect(requestsTo(seen, 'POST', '/api/v1/views')[0]?.body).toEqual({
      name: 'Recent connections',
      columns: ['preferred_name', 'last_name', 'current_title', 'current_company', 'met'],
      sort: [{ field: 'last_name', direction: 'desc' }],
      filter: { where: null, include_archived: false },
    })
  })

  it('opens an existing view with its filter in the builder', async () => {
    mockApi(routes())
    const { container } = renderWithClient(<SavedViewsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Untriaged engineers' }))

    expect(container.querySelector('[data-op="has_email"]')).not.toBeNull()
    const columns = within(screen.getByRole('group', { name: 'Columns' }))
    expect(columns.getByRole('checkbox', { name: 'current title' })).toBeChecked()
    expect(columns.getByRole('checkbox', { name: 'notes' })).not.toBeChecked()
  })

  it('will not save a view with no columns', async () => {
    mockApi(routes())
    renderWithClient(<SavedViewsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Untriaged engineers' }))

    const columns = within(screen.getByRole('group', { name: 'Columns' }))
    for (const name of ['preferred name', 'last name', 'current title']) {
      fireEvent.click(columns.getByRole('checkbox', { name }))
    }
    expect(screen.getByRole('button', { name: 'Save view' })).toBeDisabled()
    expect(screen.getByText('Pick at least one column.')).toBeInTheDocument()
  })
})
