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
    tagged_by_rule: 0,
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

/**
 * `GET /api/v1/lists` on a real server: `ensure_validated_list` seeds this
 * smart list at every start (`netkeeper/web/app.py`), before any contact
 * exists and with nothing in the response marking it as built-in (PR #129
 * review, finding 1). A fake that defaults `lists` to `[]` renders a state
 * the server only reaches if the user deletes the seeded list, which is
 * exactly what hid "build a list" reading "Done" for nobody having built one.
 */
function seededValidatedList() {
  return {
    id: 1,
    name: 'Validated',
    kind: 'smart',
    filter: { where: { op: 'eq', field: 'met', value: 'met' }, include_archived: false },
    member_count: 0,
  }
}

/** Routes the three queries the dashboard makes; anything else answers 404. */
function serveDashboard({
  stats,
  drafts = { items: [], total: 0 },
  lists = [seededValidatedList()],
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

/**
 * Every field a person can see for each rendered step, read from the DOM
 * structurally rather than by scattered `getByText`s. PR #129's review found
 * five mutations — a step's title blanked, or its route repointed to another
 * step's screen — that left both suites green, because only the import
 * step's title, detail, badge, and href were ever asserted together. This
 * reads all five rows as one table, in order, so a wrong title or a swapped
 * destination on any step fails a single assertion.
 */
function renderedSteps() {
  return screen.getAllByRole('listitem').map((li) => {
    const title = li.querySelector('[data-slot="card-title"]')?.textContent?.trim() ?? null
    const detail = li.querySelector('[data-slot="card-description"]')?.textContent?.trim() ?? null
    const badge = li.querySelector('[data-slot="badge"]')?.textContent?.trim() ?? null
    const link = li.querySelector('a')
    return {
      title,
      detail,
      badge,
      href: link?.getAttribute('href') ?? null,
      cta: link?.textContent?.trim() ?? null,
    }
  })
}

describe('dashboard: a fresh install with nothing imported', () => {
  it('says exactly what to do first, and renders all five real steps correctly', async () => {
    serveDashboard({ stats: statsBody() })
    await renderApp('/')
    const main = within(screen.getByRole('main'))

    expect(await main.findByText('Start here')).toBeInTheDocument()
    expect(main.getByText(/bring in a csv or your linkedin archive first/i)).toBeInTheDocument()

    expect(renderedSteps()).toEqual([
      {
        title: '1. Import your data',
        detail: 'Nothing imported yet',
        badge: 'Not started',
        href: '/imports',
        cta: 'Import contacts',
      },
      {
        title: '2. Review what was tagged automatically',
        detail: 'Nothing tagged yet',
        badge: 'Not started',
        href: '/lists',
        cta: 'Review tags',
      },
      {
        title: '3. Triage',
        detail: 'Nothing to triage yet',
        badge: 'Not started',
        href: '/triage',
        cta: 'Continue triage',
      },
      // The seeded "Validated" list is present from server start, before any
      // contact exists — so this step never gets a "Not started" badge (or a
      // "Done" one), only the honest count.
      {
        title: '4. Build a list',
        detail: '1 list',
        badge: null,
        href: '/lists',
        cta: 'Open lists',
      },
      {
        title: '5. Export',
        detail: 'Nothing to export yet',
        badge: 'Not started',
        href: '/exports',
        cta: 'Export contacts',
      },
    ])
  })
})

describe('dashboard: an account with contacts', () => {
  it('shows every step its real count, state, title, and destination, from the API alone', async () => {
    // `tagged: 5` (any source) but `tagged_by_rule: 4` — one of the five was
    // hand-applied, not the rules. The step must report the rule-only count.
    const seen = serveDashboard({
      stats: statsBody({
        total: 10,
        met: 5,
        not_met: 2,
        untriaged: 3,
        tagged: 5,
        tagged_by_rule: 4,
      }),
      lists: [
        seededValidatedList(),
        { id: 2, name: 'First 100', kind: 'static', member_count: 12 },
      ],
    })
    await renderApp('/')
    const main = within(screen.getByRole('main'))
    await main.findByText('10 contacts imported')

    expect(renderedSteps()).toEqual([
      {
        title: '1. Import your data',
        detail: '10 contacts imported',
        badge: 'Done',
        href: '/imports',
        cta: 'Import contacts',
      },
      // Real, both directions, since the stats lane split tagged_by_rule
      // (TagSource.RULE only) out from tagged (any source) — #129 review,
      // finding 2. 4, not 5: the hand-applied tag doesn't count.
      {
        title: '2. Review what was tagged automatically',
        detail: '4 contacts tagged automatically',
        badge: 'Done',
        href: '/lists',
        cta: 'Review tags',
      },
      {
        title: '3. Triage',
        detail: '7 of 10 triaged',
        badge: 'In progress',
        href: '/triage',
        cta: 'Continue triage',
      },
      {
        title: '4. Build a list',
        detail: '2 lists',
        badge: null,
        href: '/lists',
        cta: 'Open lists',
      },
      {
        title: '5. Export',
        detail: 'Not tracked — export runs whenever you like',
        badge: null,
        href: '/exports',
        cta: 'Export contacts',
      },
    ])

    // The setup path replaces the scaffold; there is no separate "current user" card here
    // (spec 14.3: it is settings' own, via the raw /me payload).
    expect(main.queryByText('Current user')).not.toBeInTheDocument()

    // The CSRF marker (spec 14.2) rides on every request this page makes.
    expect(seen.length).toBeGreaterThanOrEqual(3)
    for (const request of seen) {
      expect(request.headers.get('X-Netkeeper-Client')).toBe('1')
    }
  })

  it('does not let a hand-applied tag read as automatic progress', async () => {
    // Three contacts tagged by hand, none by a rule: `tagged: 3`, `tagged_by_rule: 0`.
    serveDashboard({ stats: statsBody({ total: 10, tagged: 3, tagged_by_rule: 0 }) })
    await renderApp('/')
    const main = within(screen.getByRole('main'))
    await main.findByText('10 contacts imported')

    const reviewRow = renderedSteps()[1]
    expect(reviewRow).toEqual({
      title: '2. Review what was tagged automatically',
      detail: 'Nothing tagged yet',
      badge: 'Not started',
      href: '/lists',
      cta: 'Run auto-tag rules',
    })
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

  it('restores the coverage the deleted scaffold cards took with them: health version and live status', async () => {
    serveDashboard({ stats: statsBody({ total: 3 }) })
    await renderApp('/')
    const banner = within(screen.getByRole('banner'))
    const main = within(screen.getByRole('main'))

    // The old "Backend" card asserted the version; the shell's own health dot
    // (`components/layout/backend-health.tsx`) is the only place left that does.
    expect(await banner.findByText('Backend ok · 0.0.1-test')).toBeInTheDocument()

    // The old "Event stream" card asserted "Disconnected"; jsdom's EventSource
    // never connects, so the inline indicator that replaced the card reads the
    // same way here.
    expect(main.getByText('Live updates disconnected')).toBeInTheDocument()
  })
})

describe('dashboard: the backend is unreachable', () => {
  it('shows a plain unreachable state, not a crash, announced to assistive tech', async () => {
    // The default handler rejects every fetch (src/test/fetch.ts).
    await renderApp('/')
    const main = within(screen.getByRole('main'))

    expect(await main.findByText('Backend unreachable')).toBeInTheDocument()
    expect(main.getByText(/make dev/)).toBeInTheDocument()
    expect(within(screen.getByRole('banner')).getByText('Backend unreachable')).toBeInTheDocument()

    // role="alert" so a screen reader announces the failure without the user
    // having to go looking for it (#129 review, finding 8).
    expect(screen.getByRole('alert')).toHaveTextContent('Backend unreachable')

    // Nothing from the setup path renders on top of a count nobody could read.
    expect(main.queryByText('Import your data')).not.toBeInTheDocument()
  })
})
