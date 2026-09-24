import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import { RunDetail } from './run-detail'
import { backend, run, type Call, type Handler } from './test-support'

function renderDetail(
  runId: number,
  handlers: Record<string, Handler>,
  calls: Call[] = [],
  onResumed = vi.fn(),
) {
  mockFetch(backend(handlers, calls))
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <RunDetail runId={runId} onResumed={onResumed} />
    </QueryClientProvider>,
  )
  return { calls, onResumed }
}

describe('RunDetail', () => {
  it('offers Stop for a running run, disabled once cancel is requested', async () => {
    const { calls } = renderDetail(1, {
      'GET /api/v1/linkedin/runs/1': () => jsonResponse(run({ id: 1, status: 'running' })),
      'POST /api/v1/linkedin/runs/1/cancel': () =>
        jsonResponse(
          run({ id: 1, status: 'running', cancel_requested_at: '2026-09-23T10:05:00Z' }),
        ),
    })
    const stop = await screen.findByRole('button', { name: 'Stop' })

    fireEvent.click(stop)

    expect(await screen.findByRole('button', { name: 'Stopping…' })).toBeDisabled()
    expect(calls.some((call) => call.path === '/api/v1/linkedin/runs/1/cancel')).toBe(true)
  })

  it('shows the network-aging note when a complete full sync refused to age anyone', async () => {
    renderDetail(1, {
      'GET /api/v1/linkedin/runs/1': () =>
        jsonResponse(
          run({
            id: 1,
            kind: 'connections_full',
            status: 'completed',
            completed_at: '2026-09-23T11:00:00Z',
            aging_refused: 'a full sync would have aged more than a tenth of the network',
          }),
        ),
    })
    expect(
      await screen.findByText(/Network aging: a full sync would have aged/i),
    ).toBeInTheDocument()
  })

  it('offers Resume only for a resumable aborted enrichment', async () => {
    renderDetail(1, {
      'GET /api/v1/linkedin/runs/1': () =>
        jsonResponse(
          run({
            id: 1,
            kind: 'enrich',
            status: 'aborted',
            stop_reason: 'throttled',
            planned: 10,
            completed: 4,
          }),
        ),
    })
    expect(await screen.findByRole('button', { name: 'Resume' })).toBeInTheDocument()
  })

  it('does not offer Resume once the plan is complete', async () => {
    renderDetail(1, {
      'GET /api/v1/linkedin/runs/1': () =>
        jsonResponse(run({ id: 1, kind: 'enrich', status: 'aborted', planned: 10, completed: 10 })),
    })
    await screen.findByText('Aborted')
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument()
  })

  it('gates Resume behind a confirmation dialog and reports the new run id', async () => {
    const { calls, onResumed } = renderDetail(1, {
      'GET /api/v1/linkedin/runs/1': () =>
        jsonResponse(run({ id: 1, kind: 'enrich', status: 'aborted', planned: 10, completed: 4 })),
      'POST /api/v1/linkedin/runs/1/resume': (call) => {
        expect(call.body).toEqual({ max_visits: null })
        return jsonResponse({ run_id: 99, task_id: 'task-3' }, 202)
      },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Resume' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('resumed only once')

    fireEvent.click(within(dialog).getByRole('button', { name: 'Resume' }))

    await waitFor(() => expect(onResumed).toHaveBeenCalledWith(99))
    expect(calls.some((call) => call.path === '/api/v1/linkedin/runs/1/resume')).toBe(true)
  })
})
