/**
 * `/lists` and `/exports` are real pages now, mounted through the app's own router.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse } from '@/test/fetch'
import { renderApp } from '@/test/render'

import { mockApi } from './harness'

function routes() {
  return {
    'GET /api/v1/health': () => jsonResponse({ status: 'ok', version: '0.0.1-test' }),
    'GET /api/v1/me': () =>
      jsonResponse({ id: 1, kind: 'local', display_name: null, email: null, timezone: 'UTC' }),
    'GET /api/v1/tags': () => jsonResponse([]),
    'GET /api/v1/autotag-rules': () => jsonResponse([]),
    'GET /api/v1/lists': () => jsonResponse([]),
    'GET /api/v1/views': () => jsonResponse([]),
    'POST /api/v1/contacts/query': () =>
      jsonResponse({ items: [], total: 0, describe: 'all contacts' }),
  }
}

describe('the routes this lane owns', () => {
  it('puts lists, tags and rules, and saved views on /lists', async () => {
    mockApi(routes())
    await renderApp('/lists')
    const main = within(screen.getByRole('main'))
    expect(await main.findByRole('tab', { name: 'Lists' })).toBeInTheDocument()
    expect(main.getByRole('tab', { name: 'Tags and rules' })).toBeInTheDocument()
    expect(main.getByRole('tab', { name: 'Saved views' })).toBeInTheDocument()
  })

  it('opens the tab ?tab= names, so a link can mean "Tags and rules" (#134)', async () => {
    mockApi(routes())
    await renderApp('/lists?tab=tags')
    const main = within(screen.getByRole('main'))
    expect(await main.findByRole('tab', { name: 'Tags and rules' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(await main.findByRole('button', { name: 'Run all rules now' })).toBeInTheDocument()
  })

  it('opens the Lists tab for no ?tab=, or one it does not know', async () => {
    mockApi(routes())
    const { router } = await renderApp('/lists?tab=nonsense')
    const main = within(screen.getByRole('main'))
    const lists = await main.findByRole('tab', { name: 'Lists' })
    await waitFor(() => expect(lists).toHaveAttribute('aria-selected', 'true'))
    expect(await main.findByText('No lists yet')).toBeInTheDocument()

    // And a tab click from there writes a clean URL.
    fireEvent.click(main.getByRole('tab', { name: 'Tags and rules' }))
    await waitFor(() => expect(router.state.location.search).toEqual({ tab: 'tags' }))
  })

  it('puts the tab in the URL when one is clicked, without adding history', async () => {
    mockApi(routes())
    const { router } = await renderApp('/lists')
    const main = within(screen.getByRole('main'))

    fireEvent.click(await main.findByRole('tab', { name: 'Saved views' }))
    await waitFor(() => expect(router.state.location.search).toEqual({ tab: 'views' }))
    fireEvent.click(main.getByRole('tab', { name: 'Lists' }))
    await waitFor(() => expect(router.state.location.search).toEqual({}))
    expect(router.history.length).toBe(1)
  })

  it('puts the filter builder and the preset picker on /exports', async () => {
    mockApi(routes())
    await renderApp('/exports')
    const main = within(screen.getByRole('main'))
    expect(await main.findByText('Who to export')).toBeInTheDocument()
    expect(main.getByLabelText('Preset')).toBeInTheDocument()
    expect(main.getByLabelText('Format')).toBeInTheDocument()
  })
})
