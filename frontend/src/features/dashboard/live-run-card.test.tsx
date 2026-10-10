import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { EventStreamContext } from '@/features/events/event-stream-context'
import {
  STATUS_CLEAR,
  backend,
  run,
  runPage,
  type Call,
  type Handler,
} from '@/features/linkedin/test-support'
import { FakeEventSource, resetFakeEventSource } from '@/test/fake-event-source'
import { jsonResponse, mockFetch } from '@/test/fetch'

import { LiveRunCard } from './live-run-card'

const RUNNING = run({
  id: 7,
  kind: 'enrich',
  status: 'running',
  planned: 10,
  completed: 2,
  progress: { planned: 10, visited: 2, harvested: 2, not_found: 0 },
})

const CONTACTS = {
  items: [
    {
      contact_id: 42,
      first_name: 'Rosalind',
      last_name: 'Quillfeather',
      outcome: 'applied',
      outcome_text: 'profile read and saved',
    },
    {
      contact_id: 43,
      first_name: 'Tobias',
      last_name: 'Marrowbone',
      outcome: 'not_found',
      outcome_text: 'profile not found',
    },
  ],
}

/** The card under a minimal router (its contact links need one) and a live event stream. */
function renderCard(handlers: Record<string, Handler>, calls: Call[] = []) {
  mockFetch(backend(handlers, calls))
  const source = new FakeEventSource('/api/v1/events')
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({ component: () => <Outlet /> })
  const router = createRouter({
    routeTree: root.addChildren([
      createRoute({ getParentRoute: () => root, path: '/', component: LiveRunCard }),
      createRoute({
        getParentRoute: () => root,
        path: '/contacts/$contactId',
        component: () => <p>Contact page</p>,
      }),
    ]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  })
  render(
    <EventStreamContext value={{ status: 'connected', source: source as unknown as EventSource }}>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </EventStreamContext>,
  )
  return { calls, source }
}

function running(overrides: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    'GET /api/v1/linkedin/status': () => jsonResponse({ ...STATUS_CLEAR, running_run_id: 7 }),
    'GET /api/v1/linkedin/runs': () => jsonResponse(runPage([RUNNING])),
    'GET /api/v1/linkedin/runs/7': () => jsonResponse(RUNNING),
    'GET /api/v1/linkedin/runs/7/contacts': () => jsonResponse(CONTACTS),
    ...overrides,
  }
}

function posted(calls: Call[], path: string): boolean {
  return calls.some((call) => call.method === 'POST' && call.path === path)
}

afterEach(() => {
  resetFakeEventSource()
})

describe('LiveRunCard', () => {
  it('says so when no run is going', async () => {
    renderCard({
      'GET /api/v1/linkedin/status': () => jsonResponse(STATUS_CLEAR),
      'GET /api/v1/linkedin/runs': () =>
        jsonResponse(runPage([run({ status: 'completed', stop_reason: 'end_of_list' })])),
    })
    expect(await screen.findByText('No LinkedIn run is going.')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('shows the running run, its progress, and the contacts it touched, linked', async () => {
    renderCard(running())
    expect(await screen.findByText('Enrichment')).toBeInTheDocument()
    expect(screen.getByText('Running')).toBeInTheDocument()
    expect(screen.getByLabelText('Progress')).toHaveTextContent('Visited2')

    const recent = await screen.findByRole('list', { name: 'Recent contacts' })
    const links = within(recent).getAllByRole('link')
    expect(links.map((link) => link.textContent)).toEqual([
      'Rosalind Quillfeather',
      'Tobias Marrowbone',
    ])
    expect(links[0]).toHaveAttribute('href', '/contacts/42')
    expect(recent).toHaveTextContent('profile not found')
  })

  it('updates live from run.progress, and refetches the contacts', async () => {
    const calls: Call[] = []
    const { source } = renderCard(running(), calls)
    await screen.findByText('Enrichment')
    await screen.findByRole('list', { name: 'Recent contacts' })
    const before = calls.filter((call) => call.path === '/api/v1/linkedin/runs/7/contacts').length

    act(() => source.emit('run.progress', { run_id: 7, planned: 10, visited: 5, harvested: 4 }))

    await expect.poll(() => screen.getByLabelText('Progress').textContent).toContain('Visited5')
    await screen.findByRole('list', { name: 'Recent contacts' })
    expect(
      calls.filter((call) => call.path === '/api/v1/linkedin/runs/7/contacts').length,
    ).toBeGreaterThan(before)
  })

  it('cancels only after a confirmation', async () => {
    const calls: Call[] = []
    renderCard(
      running({
        'POST /api/v1/linkedin/runs/7/cancel': () =>
          jsonResponse({ ...RUNNING, cancel_requested_at: '2026-09-23T10:05:00Z' }),
      }),
      calls,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel run' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/not resumed/)
    expect(dialog).toHaveTextContent(/pause it instead/)
    expect(posted(calls, '/api/v1/linkedin/runs/7/cancel')).toBe(false)

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel run' }))
    expect(await screen.findByText('Stopping at the next check.')).toBeInTheDocument()
    expect(posted(calls, '/api/v1/linkedin/runs/7/cancel')).toBe(true)
  })

  it('shows the note when another data directory runs the cancelled run', async () => {
    const note = 'the run is in another netkeeper data directory on this database'
    renderCard(
      running({
        'POST /api/v1/linkedin/runs/7/cancel': () =>
          jsonResponse({
            ...RUNNING,
            cancel_requested_at: '2026-09-23T10:05:00Z',
            elsewhere_note: note,
          }),
      }),
    )
    expect(screen.queryByText(note)).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel run' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel run' }))
    expect(await screen.findByText(note)).toBeInTheDocument()
  })

  it('pauses an enrichment and says it keeps its place', async () => {
    const calls: Call[] = []
    renderCard(
      running({
        'POST /api/v1/linkedin/runs/7/pause': () =>
          jsonResponse({
            ...RUNNING,
            cancel_requested_at: '2026-09-23T10:05:00Z',
            pause_requested: true,
          }),
      }),
      calls,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Pause' }))

    expect(
      await screen.findByText('Pausing at the next check; it keeps its place.'),
    ).toBeInTheDocument()
    expect(posted(calls, '/api/v1/linkedin/runs/7/pause')).toBe(true)
    expect(screen.getByRole('button', { name: 'Pause' })).toBeDisabled()
    // Cancel still wins over a pause, so it stays available.
    expect(screen.getByRole('button', { name: 'Cancel run' })).toBeEnabled()
  })

  it('offers no pause for a sync, which keeps no plan to resume', async () => {
    const sync = run({ id: 7, kind: 'connections_full', status: 'running' })
    renderCard(
      running({
        'GET /api/v1/linkedin/runs': () => jsonResponse(runPage([sync])),
        'GET /api/v1/linkedin/runs/7': () => jsonResponse(sync),
        'GET /api/v1/linkedin/runs/7/contacts': () => jsonResponse({ items: [] }),
      }),
    )
    expect(await screen.findByRole('button', { name: 'Cancel run' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Pause' })).not.toBeInTheDocument()
    expect(await screen.findByText('No contacts touched yet.')).toBeInTheDocument()
  })

  it('shows Resume for a paused run, behind a confirmation', async () => {
    const paused = run({
      id: 7,
      kind: 'enrich',
      status: 'aborted',
      stop_reason: 'paused',
      stop_reason_text: 'paused; resume it to continue its plan',
      planned: 10,
      completed: 4,
    })
    const calls: Call[] = []
    renderCard(
      {
        'GET /api/v1/linkedin/status': () => jsonResponse(STATUS_CLEAR),
        'GET /api/v1/linkedin/runs': () => jsonResponse(runPage([paused])),
        'GET /api/v1/linkedin/runs/7': () => jsonResponse(paused),
        'GET /api/v1/linkedin/runs/7/contacts': () => jsonResponse(CONTACTS),
        'POST /api/v1/linkedin/runs/7/resume': () => jsonResponse({ run_id: 8, task_id: 't' }),
      },
      calls,
    )
    expect(await screen.findByText('Paused')).toBeInTheDocument()
    expect(screen.getByText('4 of 10')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancel run' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(posted(calls, '/api/v1/linkedin/runs/7/resume')).toBe(false)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Resume' }))
    await screen.findByRole('button', { name: 'Resume' })
    expect(posted(calls, '/api/v1/linkedin/runs/7/resume')).toBe(true)
  })

  it('offers no Resume for a run that was cancelled, or a paused one already resumed', async () => {
    for (const ended of [
      run({ id: 7, kind: 'enrich', status: 'aborted', stop_reason: 'cancelled' }),
      run({ id: 7, kind: 'enrich', status: 'aborted', stop_reason: 'paused', resumed_by: 9 }),
    ]) {
      renderCard({
        'GET /api/v1/linkedin/status': () => jsonResponse(STATUS_CLEAR),
        'GET /api/v1/linkedin/runs': () => jsonResponse(runPage([ended])),
      })
      expect(await screen.findByText('No LinkedIn run is going.')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument()
      cleanup()
    }
  })
})
