/**
 * `/lists` and `/exports` are real pages now, mounted through the app's own router.
 */
import { screen, within } from '@testing-library/react'
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

  it('puts the filter builder and the preset picker on /exports', async () => {
    mockApi(routes())
    await renderApp('/exports')
    const main = within(screen.getByRole('main'))
    expect(await main.findByText('Who to export')).toBeInTheDocument()
    expect(main.getByLabelText('Preset')).toBeInTheDocument()
    expect(main.getByLabelText('Format')).toBeInTheDocument()
  })
})
