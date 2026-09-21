import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from './fetch'
import { renderApp } from './render'

/** A stats response with every count at its default; override only what a test cares about. */
function statsBody(overrides: Record<string, number> = {}) {
  return {
    total: 0,
    met: 0,
    not_met: 0,
    skipped: 0,
    untriaged: 0,
    archived: 0,
    merged_away: 0,
    with_email: 0,
    with_phone: 0,
    tagged: 0,
    ...overrides,
  }
}

function draftRun(id: number) {
  return {
    id,
    filename: 'contacts.csv',
    preset: 'nine-column',
    mapping: {},
    source_kind: 'csv',
    status: 'draft',
    total_rows: 8,
    candidate_count: 1,
    matched_count: 3,
    created_count: 4,
    skipped_count: 0,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    committed_at: null,
    rolled_back_at: null,
  }
}

/** Routes the three queries the dashboard makes; anything else answers 404. */
function serveDashboard({
  stats,
  drafts = { items: [], total: 0 },
  lists = [],
}: {
  stats: ReturnType<typeof statsBody>
  drafts?: { items: ReturnType<typeof draftRun>[]; total: number }
  lists?: unknown[]
}) {
  const seen: Request[] = []
  mockFetch((request) => {
    seen.push(request)
    const url = new URL(request.url)
    if (url.pathname === '/api/v1/health')
      return jsonResponse({ status: 'ok', version: '0.0.1-test' })
    if (url.pathname === '/api/v1/contacts/stats') return jsonResponse(stats)
    if (url.pathname === '/api/v1/imports' && url.searchParams.get('status') === 'draft') {
      return jsonResponse(drafts)
    }
    if (url.pathname === '/api/v1/lists') return jsonResponse(lists)
    return new Response('not found', { status: 404 })
  })
  return seen
}

describe('dashboard: a fresh install with nothing imported', () => {
  it('says exactly what to do first, and marks the rest not started', async () => {
    serveDashboard({ stats: statsBody() })
    await renderApp('/')
    const main = within(screen.getByRole('main'))

    expect(await main.findByText('Start here')).toBeInTheDocument()
    expect(main.getByText(/bring in a csv or your linkedin archive first/i)).toBeInTheDocument()

    expect(main.getByText('Import your data')).toBeInTheDocument()
    expect(main.getByText('Nothing imported yet')).toBeInTheDocument()
    expect(main.getByText('Nothing tagged yet')).toBeInTheDocument()
    expect(main.getByText('Nothing to triage yet')).toBeInTheDocument()
    expect(main.getByText('No lists yet')).toBeInTheDocument()
    expect(main.getByText('Nothing to export yet')).toBeInTheDocument()
    expect(main.getAllByText('Not started')).toHaveLength(5)

    expect(main.getByRole('link', { name: 'Import contacts' })).toHaveAttribute('href', '/imports')
  })
})

describe('dashboard: an account with contacts', () => {
  it('shows every step its real count and state, from the API alone', async () => {
    const seen = serveDashboard({
      stats: statsBody({ total: 10, met: 5, not_met: 2, untriaged: 3, tagged: 4 }),
      lists: [{ id: 1, name: 'First 100', kind: 'static', filter: null, member_count: 12 }],
    })
    await renderApp('/')
    const main = within(screen.getByRole('main'))

    expect(await main.findByText('10 contacts imported')).toBeInTheDocument()
    expect(main.getByText('4 contacts tagged automatically')).toBeInTheDocument()
    expect(main.getByText('7 of 10 triaged')).toBeInTheDocument()
    expect(main.getByText('1 list built')).toBeInTheDocument()
    expect(main.getByText('Not tracked — export runs whenever you like')).toBeInTheDocument()

    expect(main.getAllByText('Done')).toHaveLength(2) // import, build a list
    expect(main.getByText('In progress')).toBeInTheDocument() // triage
    expect(main.getAllByText('Not tracked')).toHaveLength(2) // review tags, export

    // The setup path replaces the scaffold; there is no separate "current user" card here
    // (spec 14.3: it is settings' own, via the raw /me payload).
    expect(main.queryByText('Current user')).not.toBeInTheDocument()

    // The CSRF marker (spec 14.2) rides on every request this page makes.
    expect(seen.length).toBeGreaterThanOrEqual(3)
    for (const request of seen) {
      expect(request.headers.get('X-Netkeeper-Client')).toBe('1')
    }
  })

  it('offers to resume an open draft instead of claiming the import step is done', async () => {
    serveDashboard({
      stats: statsBody({ total: 5 }),
      drafts: { items: [draftRun(42)], total: 1 },
    })
    await renderApp('/')
    const main = within(screen.getByRole('main'))

    expect(await main.findByText('1 draft import waiting to be finished')).toBeInTheDocument()
    const resume = main.getByRole('link', { name: 'Continue this import' })
    expect(resume).toHaveAttribute('href', '/imports?run=42')
  })
})

describe('dashboard: the backend is unreachable', () => {
  it('shows a plain unreachable state, not a crash', async () => {
    // The default handler rejects every fetch (src/test/fetch.ts).
    await renderApp('/')
    const main = within(screen.getByRole('main'))

    expect(await main.findByText('Backend unreachable')).toBeInTheDocument()
    expect(main.getByText(/make dev/)).toBeInTheDocument()
    expect(within(screen.getByRole('banner')).getByText('Backend unreachable')).toBeInTheDocument()

    // Nothing from the setup path renders on top of a count nobody could read.
    expect(main.queryByText('Import your data')).not.toBeInTheDocument()
  })
})
