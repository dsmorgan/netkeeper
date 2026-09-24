import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
  return { calls, onStarted }
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
})
