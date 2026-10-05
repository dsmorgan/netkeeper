import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import { formatFields } from './fields'
import { RunDetail } from './run-detail'
import { renderLinkedInPage } from './test-render'
import { BUDGET, backend, run, runPage, type Call, type Handler } from './test-support'

const ABORTED = run({
  id: 37,
  kind: 'enrich',
  status: 'aborted',
  trigger: 'scheduled',
  started_at: '2026-10-04T15:30:00Z',
  completed_at: '2026-10-04T16:58:00Z',
  stop_reason: 'route_changed',
  stop_reason_text: 'the page’s answers changed shape',
  counts: {
    planned: 44,
    not_found: 0,
    unreadable: 3,
    unreadable_visits: [{ visit: 3, contact_id: 42, reason: 'overlay_never_answered' }],
  },
})

const REASONS = {
  unreadable_visits: [
    {
      visit: 3,
      contact_id: 42,
      contact_exists: true,
      first_name: 'Rosalind',
      last_name: 'Quillfeather',
      reason: 'overlay_never_answered',
      reason_text: 'the Contact info overlay never answered',
    },
    {
      visit: 7,
      contact_id: 43,
      contact_exists: false,
      first_name: null,
      last_name: null,
      reason: 'id_mismatch',
      reason_text: 'the profile’s id is not the contact’s; nothing saved',
    },
  ],
  lost_answers: [],
  stopped_by: null,
}

/** The detail under a minimal router (its contact links need one). */
function renderDetail(runId: number, handlers: Record<string, Handler>, calls: Call[] = []) {
  mockFetch(
    backend({ 'GET /api/v1/linkedin/budget': () => jsonResponse(BUDGET), ...handlers }, calls),
  )
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({ component: () => <Outlet /> })
  const router = createRouter({
    routeTree: root.addChildren([
      createRoute({
        getParentRoute: () => root,
        path: '/',
        component: () => <RunDetail runId={runId} />,
      }),
      createRoute({
        getParentRoute: () => root,
        path: '/contacts/$contactId',
        component: () => <p>Contact page</p>,
      }),
    ]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  })
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { calls, router, queryClient }
}

describe('RunDetail reasons (#405)', () => {
  it('shows the run id, its trigger, and each unreadable visit with its contact', async () => {
    const { router } = renderDetail(37, {
      'GET /api/v1/linkedin/runs/37': () => jsonResponse(ABORTED),
      'GET /api/v1/linkedin/runs/37/diagnostics': () => jsonResponse(REASONS),
    })

    const detail = await screen.findByRole('region', { name: 'Run 37' })
    expect(within(detail).getByText('Run 37')).toBeInTheDocument()
    expect(within(detail).getByText(/^Scheduled · started/)).toBeInTheDocument()
    expect(
      within(detail).getByText('Stopped: the page’s answers changed shape'),
    ).toBeInTheDocument()

    const table = await within(detail).findByRole('table')
    const rows = within(table).getAllByRole('row')
    expect(rows).toHaveLength(3)
    expect(within(rows[1]!).getByText('3')).toBeInTheDocument()
    expect(
      within(rows[1]!).getByText('the Contact info overlay never answered'),
    ).toBeInTheDocument()
    expect(within(rows[1]!).getByText('overlay_never_answered')).toBeInTheDocument()
    expect(within(rows[2]!).getByText('Contact 43 (deleted)')).toBeInTheDocument()
    expect(within(rows[2]!).queryByRole('link')).not.toBeInTheDocument()

    fireEvent.click(within(rows[1]!).getByRole('link', { name: 'Rosalind Quillfeather' }))
    expect(await screen.findByText('Contact page')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/contacts/42')
  })

  it('never shows the stored list as a counts field', async () => {
    renderDetail(37, {
      'GET /api/v1/linkedin/runs/37': () => jsonResponse(ABORTED),
      'GET /api/v1/linkedin/runs/37/diagnostics': () => jsonResponse(REASONS),
    })
    await screen.findByRole('region', { name: 'Run 37' })
    expect(screen.queryByText('Unreadable Visits')).not.toBeInTheDocument()
    expect(screen.getByText('Unreadable')).toBeInTheDocument()
    expect(formatFields(ABORTED.counts).map((field) => field.label)).toEqual([
      'Planned',
      'Not Found',
      'Unreadable',
    ])
  })

  it('shows a connections sync’s lost answers', async () => {
    renderDetail(9, {
      'GET /api/v1/linkedin/runs/9': () =>
        jsonResponse(
          run({
            id: 9,
            kind: 'connections_full',
            status: 'aborted',
            stop_reason: 'answer_lost',
            counts: { pages: 2, lost: [{ start: 80, cause: 'Error (no resource)' }] },
          }),
        ),
      'GET /api/v1/linkedin/runs/9/diagnostics': () =>
        jsonResponse({
          unreadable_visits: [],
          lost_answers: [
            { start: 80, cause: 'Error (no resource)', ending: 'the page moved past it' },
          ],
          stopped_by: null,
        }),
    })
    const lost = await screen.findByRole('region', { name: 'Lost answers' })
    expect(within(lost).getByText('80')).toBeInTheDocument()
    expect(within(lost).getByText('the page moved past it')).toBeInTheDocument()
  })

  it('shows nothing extra for a run with nothing unreadable', async () => {
    renderDetail(1, {
      'GET /api/v1/linkedin/runs/1': () => jsonResponse(run({ id: 1, status: 'completed' })),
    })
    await screen.findByRole('region', { name: 'Run 1' })
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('a route_changed stop always says why (#415 review)', () => {
  const STOPPED = run({
    id: 51,
    kind: 'enrich',
    status: 'aborted',
    stop_reason: 'route_changed',
    stop_reason_text: 'the page’s answers changed shape',
    counts: { planned: 44, unreadable: 0 },
  })

  it('names the visit whose answer stopped the run at once, and its code', async () => {
    renderDetail(51, {
      'GET /api/v1/linkedin/runs/51': () => jsonResponse(STOPPED),
      'GET /api/v1/linkedin/runs/51/diagnostics': () =>
        jsonResponse({
          unreadable_visits: [],
          lost_answers: [],
          stopped_by: {
            visit: 2,
            contact_id: 42,
            contact_exists: true,
            first_name: 'Rosalind',
            last_name: 'Quillfeather',
            reason: 'profile_status',
            reason_text: 'the profile answered a status that stopped the run at once',
          },
        }),
    })
    const note = await screen.findByText(/Stopped at once by the page’s answer on visit 2/)
    expect(note).toHaveTextContent('the profile answered a status that stopped the run at once')
    expect(within(note).getByText('profile_status')).toBeInTheDocument()
    expect(within(note).getByRole('link', { name: 'Rosalind Quillfeather' })).toBeInTheDocument()
  })

  it('says nothing was recorded rather than show an empty list', async () => {
    renderDetail(51, { 'GET /api/v1/linkedin/runs/51': () => jsonResponse(STOPPED) })
    expect(
      await screen.findByText('No per-visit reasons were recorded for this run.'),
    ).toBeInTheDocument()
  })
})

describe('the runs list (#405)', () => {
  it('shows each run’s id, and a click anywhere on the row opens it', async () => {
    renderLinkedInPage({
      'GET /api/v1/linkedin/runs': () => jsonResponse(runPage([ABORTED])),
      'GET /api/v1/linkedin/runs/37': () => jsonResponse(ABORTED),
    })

    await screen.findByRole('columnheader', { name: 'Kind' })
    const runs = screen
      .getAllByRole('table')
      .find((table) => within(table).queryByRole('columnheader', { name: 'Kind' }))!
    const [header, row] = within(runs).getAllByRole('row')
    const headers = within(header!).getAllByRole('columnheader')
    expect(headers[0]).toHaveTextContent('Kind')
    expect(headers[1]).toHaveTextContent('Run')
    // The kind stays the row's header.
    expect(within(row!).getByRole('rowheader')).toHaveTextContent('Enrichment')
    const cell = within(row!).getByText('37')

    expect(screen.queryByRole('region', { name: 'Run 37' })).not.toBeInTheDocument()
    act(() => fireEvent.click(cell))
    expect(await screen.findByRole('region', { name: 'Run 37' })).toBeInTheDocument()
  })
})

describe('bringing the detail into view (#415 review)', () => {
  function stubMotion(reduced: boolean) {
    const scrolled: ScrollIntoViewOptions[] = []
    const scroll = vi.fn(function (options?: ScrollIntoViewOptions | boolean) {
      if (typeof options === 'object') scrolled.push(options)
    })
    Element.prototype.scrollIntoView = scroll
    window.matchMedia = vi.fn().mockReturnValue({ matches: reduced }) as typeof window.matchMedia
    return { scrolled, scroll }
  }

  afterEach(() => {
    // jsdom has neither; the page must work without them too.
    delete (Element.prototype as Partial<Element>).scrollIntoView
    delete (window as Partial<Window>).matchMedia
  })

  it.each([
    [false, 'smooth'],
    [true, 'auto'],
  ] as const)('scrolls on a row click (reduced motion %s: %s)', async (reduced, behavior) => {
    const { scrolled } = stubMotion(reduced)
    renderLinkedInPage({
      'GET /api/v1/linkedin/runs': () => jsonResponse(runPage([ABORTED])),
      'GET /api/v1/linkedin/runs/37': () => jsonResponse(ABORTED),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Enrichment' }))
    await screen.findByRole('region', { name: 'Run 37' })
    expect(scrolled).toEqual([{ block: 'nearest', behavior }])
  })
})
