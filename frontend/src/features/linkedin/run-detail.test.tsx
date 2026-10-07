import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import { RunDetail } from './run-detail'
import { backend, BUDGET, run, type Call, type Handler } from './test-support'

function renderDetail(
  runId: number,
  handlers: Record<string, Handler>,
  calls: Call[] = [],
  onResumed = vi.fn(),
) {
  mockFetch(
    backend({ 'GET /api/v1/linkedin/budget': () => jsonResponse(BUDGET), ...handlers }, calls),
  )
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

  it('offers no Stop for a run that has already ended (R-10)', async () => {
    const labels = { completed: 'Completed', aborted: 'Aborted', failed: 'Failed' } as const
    for (const status of ['completed', 'aborted', 'failed'] as const) {
      renderDetail(1, {
        'GET /api/v1/linkedin/runs/1': () => jsonResponse(run({ id: 1, status })),
      })
      await screen.findByText(labels[status])
      expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Stopping…' })).not.toBeInTheDocument()
      cleanup()
    }
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

  it('says plainly that active hours stopped the run, and when the window opens (#213)', async () => {
    renderDetail(1, {
      'GET /api/v1/linkedin/runs/1': () =>
        jsonResponse(
          run({
            id: 1,
            kind: 'enrich',
            status: 'aborted',
            stop_reason: 'inactive',
            stop_reason_text: 'outside active hours',
            notes:
              'stopped outside active hours (08:30-21:30 America/New_York); the next window opens at 08:30 tomorrow. Change the active hours in Settings to adjust.',
          }),
        ),
    })
    expect(await screen.findByText('Stopped: outside active hours')).toBeInTheDocument()
    expect(screen.getByText(/the next window opens at 08:30 tomorrow/)).toBeInTheDocument()
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

  it('offers Resume for a failed enrichment with a remaining plan (L3)', async () => {
    // failed: the run never touched the browser (BrowserUnavailable, a
    // pre-attach refusal) — its stored plan is exactly as resumable as an
    // aborted one, and the backend already allows it (enrich_plan.start_resume).
    renderDetail(1, {
      'GET /api/v1/linkedin/runs/1': () =>
        jsonResponse(
          run({
            id: 1,
            kind: 'enrich',
            status: 'failed',
            stop_reason: 'browser_unavailable',
            error: 'BrowserUnavailable: tab lost',
            planned: 10,
            completed: 4,
          }),
        ),
    })
    expect(await screen.findByRole('button', { name: 'Resume' })).toBeInTheDocument()
  })

  it('offers no Resume once the run has already been resumed (L3)', async () => {
    renderDetail(1, {
      'GET /api/v1/linkedin/runs/1': () =>
        jsonResponse(
          run({
            id: 1,
            kind: 'enrich',
            status: 'aborted',
            planned: 10,
            completed: 4,
            resumed_by: 42,
          }),
        ),
    })
    await screen.findByText('Aborted')
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument()
  })

  it('offers no Resume for a completed enrichment, whatever its counts say (R-05)', async () => {
    // A completed run's plan.completed always equals plan.contact_ids in real
    // data, so `completed < planned` alone would never be false-positive here
    // in practice — the status check is what stands between "done" and
    // "resumable" if that arithmetic invariant is ever wrong, so this pins it
    // as its own check rather than trusting the counts alone.
    renderDetail(1, {
      'GET /api/v1/linkedin/runs/1': () =>
        jsonResponse(
          run({ id: 1, kind: 'enrich', status: 'completed', planned: 10, completed: 4 }),
        ),
    })
    await screen.findByText('Completed')
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument()
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

  it('shows the profile-view notice in the Resume dialog, and confirming still resumes (#325)', async () => {
    const { calls, onResumed } = renderDetail(1, {
      'GET /api/v1/linkedin/runs/1': () =>
        jsonResponse(run({ id: 1, kind: 'enrich', status: 'aborted', planned: 10, completed: 4 })),
      'POST /api/v1/linkedin/runs/1/resume': () => jsonResponse({ run_id: 99, task_id: 't' }, 202),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Resume' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(await within(dialog).findByRole('note', { name: 'Profile views' })).toHaveTextContent(
      'Who viewed your profile',
    )

    fireEvent.click(within(dialog).getByRole('button', { name: 'Resume' }))

    await waitFor(() => expect(onResumed).toHaveBeenCalledWith(99))
    expect(calls.some((call) => call.path === '/api/v1/linkedin/runs/1/resume')).toBe(true)
  })
})
