import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { Profiler } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import { StartRunCard } from './start-run-card'
import { backend, BUDGET, type Call, type Handler } from './test-support'

function renderCard(handlers: Record<string, Handler> = {}, calls: Call[] = []) {
  mockFetch(
    backend({ 'GET /api/v1/linkedin/budget': () => jsonResponse(BUDGET), ...handlers }, calls),
  )
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onStarted = vi.fn()
  render(
    <QueryClientProvider client={queryClient}>
      <StartRunCard onStarted={onStarted} />
    </QueryClientProvider>,
  )
  return { calls, onStarted, queryClient }
}

describe('StartRunCard', () => {
  it('offers only the kinds the worker can run', async () => {
    renderCard()
    const select = await screen.findByLabelText('Kind')
    const options = within(select)
      .getAllByRole('option')
      .map((option) => option.textContent)
    expect(options).toEqual(['Full connections sync', 'Incremental connections sync', 'Enrichment'])
  })

  it('shows the max-visits field only for enrichment', async () => {
    renderCard()
    const select = await screen.findByLabelText('Kind')
    expect(screen.queryByLabelText(/max visits/i)).not.toBeInTheDocument()

    fireEvent.change(select, { target: { value: 'enrich' } })
    expect(await screen.findByLabelText(/max visits/i)).toBeInTheDocument()
  })

  it('shows the profile-view notice for enrichment only, and starting stays one click (#325)', async () => {
    const { calls } = renderCard({
      'POST /api/v1/linkedin/runs': () => jsonResponse({ run_id: 7 }),
    })
    const select = await screen.findByLabelText('Kind')
    // Let the budget query resolve first, so a missing note is not just a note not loaded yet.
    await waitFor(() =>
      expect(calls.some((call) => call.path === '/api/v1/linkedin/budget')).toBe(true),
    )
    await screen.findByRole('button', { name: 'Start run' })
    expect(screen.queryByRole('note', { name: 'Profile views' })).not.toBeInTheDocument()

    fireEvent.change(select, { target: { value: 'enrich' } })
    expect(await screen.findByRole('note', { name: 'Profile views' })).toHaveTextContent(
      'Who viewed your profile',
    )

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByRole('note', { name: 'Profile views' })).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start run' }))
    await waitFor(() =>
      expect(calls.some((call) => call.path === '/api/v1/linkedin/runs')).toBe(true),
    )
  })

  it('drops the profile-view notice from the card and the dialog when the kind goes back to a sync (#325)', async () => {
    renderCard()
    const select = await screen.findByLabelText('Kind')
    fireEvent.change(select, { target: { value: 'enrich' } })
    expect(await screen.findByRole('note', { name: 'Profile views' })).toBeInTheDocument()

    fireEvent.change(select, { target: { value: 'connections_incremental' } })
    expect(screen.queryByRole('note', { name: 'Profile views' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).queryByRole('note', { name: 'Profile views' })).not.toBeInTheDocument()
  })

  it('clamps max visits to today’s remaining budget: it can only lower it', async () => {
    renderCard()
    fireEvent.change(await screen.findByLabelText('Kind'), { target: { value: 'enrich' } })
    const maxVisits = await screen.findByLabelText(/max visits/i)
    // BUDGET fixture's profile_visits_today.remaining is 45.

    fireEvent.change(maxVisits, { target: { value: '999' } })
    expect(maxVisits).toHaveValue(45)

    fireEvent.change(maxVisits, { target: { value: '10' } })
    expect(maxVisits).toHaveValue(10)
  })

  it('gates starting behind a confirmation dialog naming the kind', async () => {
    const { calls } = renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Start run' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Start incremental connections sync?')
    expect(calls.some((call) => call.path === '/api/v1/linkedin/runs')).toBe(false)
  })

  it('starts the run only once confirmed, and reports the new run id', async () => {
    const { calls, onStarted } = renderCard({
      'POST /api/v1/linkedin/runs': (call) => {
        expect(call.body).toEqual({ kind: 'connections_incremental', max_visits: null })
        return jsonResponse({ run_id: 42, task_id: 'task-1' }, 202)
      },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start run' }))

    await waitFor(() => expect(onStarted).toHaveBeenCalledWith(42))
    expect(calls.some((call) => call.path === '/api/v1/linkedin/runs')).toBe(true)
  })

  it('sends the clamped max_visits for an enrichment start', async () => {
    const { calls } = renderCard({
      'POST /api/v1/linkedin/runs': (call) => {
        expect(call.body).toEqual({ kind: 'enrich', max_visits: 5 })
        return jsonResponse({ run_id: 7, task_id: 'task-2' }, 202)
      },
    })
    fireEvent.change(await screen.findByLabelText('Kind'), { target: { value: 'enrich' } })
    fireEvent.change(await screen.findByLabelText(/max visits/i), { target: { value: '5' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start run' }))

    await waitFor(() =>
      expect(calls.some((call) => call.path === '/api/v1/linkedin/runs')).toBe(true),
    )
    expect(calls.find((call) => call.path === '/api/v1/linkedin/runs')?.body).toEqual({
      kind: 'enrich',
      max_visits: 5,
    })
  })

  it('sends max_visits: null for a sync kind, even with a leftover value from enrich (R-16)', async () => {
    const { calls } = renderCard({
      'POST /api/v1/linkedin/runs': (call) => {
        expect(call.body).toEqual({ kind: 'connections_full', max_visits: null })
        return jsonResponse({ run_id: 8, task_id: 'task-3' }, 202)
      },
    })
    const select = await screen.findByLabelText('Kind')
    // Type a max-visits value under enrich, then switch back to a sync kind
    // without clearing it — the field itself disappears, but the state
    // behind it does not, so the guard has to be in what gets sent, not in
    // what is visible.
    fireEvent.change(select, { target: { value: 'enrich' } })
    fireEvent.change(await screen.findByLabelText(/max visits/i), { target: { value: '5' } })
    fireEvent.change(select, { target: { value: 'connections_full' } })
    expect(screen.queryByLabelText(/max visits/i)).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start run' }))

    await waitFor(() =>
      expect(calls.some((call) => call.path === '/api/v1/linkedin/runs')).toBe(true),
    )
    expect(calls.find((call) => call.path === '/api/v1/linkedin/runs')?.body).toEqual({
      kind: 'connections_full',
      max_visits: null,
    })
  })

  it('disables the input and Start, and says why, when today’s budget is spent (L1)', async () => {
    renderCard({
      'GET /api/v1/linkedin/budget': () =>
        jsonResponse({
          ...BUDGET,
          profile_visits_today: { ...BUDGET.profile_visits_today, remaining: 0 },
        }),
    })
    fireEvent.change(await screen.findByLabelText('Kind'), { target: { value: 'enrich' } })

    const maxVisits = await screen.findByLabelText(/max visits/i)
    expect(maxVisits).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Start run' })).toBeDisabled()
    expect(screen.getByText(/budget is spent/i)).toBeInTheDocument()
  })

  it('does not disable a sync start when the enrichment budget is spent', async () => {
    renderCard({
      'GET /api/v1/linkedin/budget': () =>
        jsonResponse({
          ...BUDGET,
          profile_visits_today: { ...BUDGET.profile_visits_today, remaining: 0 },
        }),
    })
    // Default kind is a sync, not enrich: the profile-visit budget does not gate it.
    expect(await screen.findByRole('button', { name: 'Start run' })).not.toBeDisabled()
  })

  it('re-clamps an already-typed value the moment remaining drops, not only on the next keystroke (L1)', async () => {
    let remaining = 45
    const { queryClient } = renderCard({
      'GET /api/v1/linkedin/budget': () =>
        jsonResponse({
          ...BUDGET,
          profile_visits_today: { ...BUDGET.profile_visits_today, remaining },
        }),
    })
    fireEvent.change(await screen.findByLabelText('Kind'), { target: { value: 'enrich' } })
    const maxVisits = await screen.findByLabelText(/max visits/i)
    fireEvent.change(maxVisits, { target: { value: '40' } })
    expect(maxVisits).toHaveValue(40)

    // Budget shrinks elsewhere (another run, or a background refetch) — no
    // keystroke on this field at all, only a new number from the query.
    remaining = 10
    await queryClient.invalidateQueries({ queryKey: ['linkedin', 'budget'] })

    await waitFor(() => expect(maxVisits).toHaveValue(10))
  })

  it('re-clamps all the way to disabled without a render loop, from a live query update', async () => {
    let remaining = 45
    let renders = 0
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    mockFetch(
      backend({
        'GET /api/v1/linkedin/budget': () =>
          jsonResponse({
            ...BUDGET,
            profile_visits_today: { ...BUDGET.profile_visits_today, remaining },
          }),
      }),
    )
    render(
      <QueryClientProvider client={queryClient}>
        <Profiler id="start-run-card" onRender={() => (renders += 1)}>
          <StartRunCard onStarted={vi.fn()} />
        </Profiler>
      </QueryClientProvider>,
    )
    fireEvent.change(await screen.findByLabelText('Kind'), { target: { value: 'enrich' } })
    const maxVisits = (await screen.findByLabelText(/max visits/i)) as HTMLInputElement
    fireEvent.change(maxVisits, { target: { value: '30' } })
    expect(maxVisits.value).toBe('30')
    const before = renders

    remaining = 7
    await act(async () => {
      await queryClient.invalidateQueries()
    })
    await waitFor(() => expect(maxVisits.value).toBe('7'))

    remaining = 0
    await act(async () => {
      await queryClient.invalidateQueries()
    })
    await waitFor(() => expect(maxVisits.value).toBe(''))
    expect(maxVisits).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Start run' })).toBeDisabled()
    // The render-time re-clamp (a `previousRemaining` state compared during
    // render, not a `useEffect`) reads `remaining` back to the same value it
    // just set every time it fires -- if it ever set state unconditionally
    // instead of only on an actual change, React would keep re-rendering
    // forever rather than settling. A generous ceiling, not an exact count:
    // this asserts it settles at all, not how many renders getting there took.
    expect(renders - before).toBeLessThan(20)
  })

  it('retries after an instant 409, from the same dialog (confirm-dialog guard)', async () => {
    let attempts = 0
    const { calls } = renderCard({
      'POST /api/v1/linkedin/runs': () => {
        attempts += 1
        return attempts === 1
          ? jsonResponse({ detail: 'run 3 is still running' }, 409)
          : jsonResponse({ run_id: 9, task_id: 't' }, 202)
      },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Start run' }))
    const dialog = await screen.findByRole('alertdialog')

    fireEvent.click(within(dialog).getByRole('button', { name: 'Start run' }))
    await within(dialog).findByText(/still running/)

    // The mutation settled (with an error) fast enough in this jsdom test
    // that `pending` may never have rendered `true` at all — exactly the case
    // that used to leave `ConfirmDialog`'s same-tick click guard stuck. If it
    // is stuck, this second click never reaches the backend and the count
    // below stays at 1.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(calls.filter((call) => call.method === 'POST')).toHaveLength(2))
  })
})
