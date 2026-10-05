import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { jsonResponse } from '@/test/fetch'
import { resetFakeEventSource } from '@/test/fake-event-source'

import { renderLinkedInPage } from './test-render'
import {
  BUDGET,
  HEAT,
  PINS,
  run,
  runPage,
  STATUS_CHECKPOINT,
  STATUS_CLEAR,
  STATUS_LOGGED_OUT,
} from './test-support'

afterEach(() => {
  resetFakeEventSource()
})

describe('session banner', () => {
  it('shows nothing when the session is not flagged', async () => {
    renderLinkedInPage({ 'GET /api/v1/linkedin/status': () => jsonResponse(STATUS_CLEAR) })
    await screen.findByRole('heading', { name: 'LinkedIn' })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('tells a logged-out session how it clears itself, with no clear button', async () => {
    renderLinkedInPage({ 'GET /api/v1/linkedin/status': () => jsonResponse(STATUS_LOGGED_OUT) })
    const banner = await screen.findByRole('alert')
    expect(within(banner).getByText(/log in to linkedin/i)).toBeInTheDocument()
    expect(within(banner).getByText(/netkeeper preflight/i)).toBeInTheDocument()
    expect(banner).toHaveTextContent(/clears this automatically/i)
    // No clear button for a logged-out flag (#181): preflight clears it once it
    // sees a live session, which is better evidence than a click.
    expect(within(banner).queryByRole('button')).not.toBeInTheDocument()
  })

  it('tells a checkpointed session to resolve it and clear the flag by hand', async () => {
    renderLinkedInPage({ 'GET /api/v1/linkedin/status': () => jsonResponse(STATUS_CHECKPOINT) })
    const banner = await screen.findByRole('alert')
    expect(within(banner).getByText(/resolve the checkpoint/i)).toBeInTheDocument()
    expect(within(banner).getByText(/netkeeper linkedin clear-flag/i)).toBeInTheDocument()
    // The by-hand clear (#181), which always asks first (linkedin-clears.test.tsx).
    expect(within(banner).getByRole('button', { name: 'Clear flag…' })).toBeInTheDocument()
    // R-13: nothing here may say or imply netkeeper clears this on its own —
    // a live session cookie is not proof a checkpoint is resolved (spec 9.7),
    // so only a person, with the button or the CLI command above, ends this banner.
    expect(banner).not.toHaveTextContent(/automatic/i)
    expect(banner).not.toHaveTextContent(/on its own/i)
  })
})

describe('scheduled runs, shown plainly', () => {
  it('shows disarmed by default', async () => {
    renderLinkedInPage()
    expect(await screen.findByText('Disarmed — nothing runs on its own')).toBeInTheDocument()
  })
})

describe('SSE updates (spec 14.1, this item’s "done when"): no reload, no poll', () => {
  it('refetches the runs list and status when a run starts', async () => {
    let runsCalls = 0
    const { source } = renderLinkedInPage({
      'GET /api/v1/linkedin/runs': () => {
        runsCalls += 1
        return jsonResponse(runPage(runsCalls === 1 ? [] : [run({ id: 5 })]))
      },
    })
    await screen.findByText('No runs yet.')
    expect(runsCalls).toBe(1)

    act(() => source.emit('run.started', { run_id: 5, kind: 'connections_incremental' }))

    await waitFor(() => expect(runsCalls).toBeGreaterThan(1))
    expect(
      await screen.findByRole('button', { name: 'Incremental connections sync' }),
    ).toBeInTheDocument()
  })

  it('patches the selected run’s live progress in place, without another fetch', async () => {
    let runDetailCalls = 0
    const { source } = renderLinkedInPage({
      'GET /api/v1/linkedin/runs': () => jsonResponse(runPage([run({ id: 5 })])),
      'GET /api/v1/linkedin/runs/5': () => {
        runDetailCalls += 1
        return jsonResponse(run({ id: 5, progress: { pages: 1, connections: 40, total: 400 } }))
      },
    })

    const openRun = await screen.findByRole('button', { name: 'Incremental connections sync' })
    act(() => openRun.click())
    await screen.findByText('40')
    expect(runDetailCalls).toBe(1)

    act(() => source.emit('run.progress', { run_id: 5, pages: 2, connections: 80, total: 400 }))

    expect(await screen.findByText('80')).toBeInTheDocument()
    // The new number came from the cache patch, not a second GET.
    expect(runDetailCalls).toBe(1)
  })

  it('refetches budget, heat, pins, and status when a run finishes', async () => {
    let budgetCalls = 0
    let heatCalls = 0
    let pinsCalls = 0
    let statusCalls = 0
    const { source } = renderLinkedInPage({
      'GET /api/v1/linkedin/budget': () => {
        budgetCalls += 1
        return jsonResponse(BUDGET)
      },
      'GET /api/v1/linkedin/heat': () => {
        heatCalls += 1
        return jsonResponse(HEAT)
      },
      'GET /api/v1/linkedin/pins': () => {
        pinsCalls += 1
        return jsonResponse(PINS)
      },
      'GET /api/v1/linkedin/status': () => {
        statusCalls += 1
        return jsonResponse(STATUS_CLEAR)
      },
    })
    await screen.findByRole('heading', { name: 'Runs' })
    const before = { budgetCalls, heatCalls, pinsCalls, statusCalls }

    act(() => source.emit('run.finished', { run_id: 1, status: 'completed' }))

    await waitFor(() => {
      expect(budgetCalls).toBeGreaterThan(before.budgetCalls)
      expect(heatCalls).toBeGreaterThan(before.heatCalls)
      expect(pinsCalls).toBeGreaterThan(before.pinsCalls)
      expect(statusCalls).toBeGreaterThan(before.statusCalls)
    })
  })

  it('also refetches the runs list itself when a run finishes (R-07)', async () => {
    let runsCalls = 0
    const { source } = renderLinkedInPage({
      'GET /api/v1/linkedin/runs': () => {
        runsCalls += 1
        return jsonResponse(runPage([run({ id: 1 })]))
      },
    })
    await screen.findByRole('heading', { name: 'Runs' })
    const before = runsCalls

    act(() => source.emit('run.finished', { run_id: 1, status: 'completed' }))

    await waitFor(() => expect(runsCalls).toBeGreaterThan(before))
  })

  it('recovers a missed event by refetching once reconnected, not on the first connect (M1)', async () => {
    let runsCalls = 0
    // Starts `disconnected`, the same as a fresh `EventStreamProvider` before
    // its `EventSource` has opened (N-08): the page's very first connect is
    // this transition to `connected` below, not the initial render.
    const { setStatus } = renderLinkedInPage(
      {
        'GET /api/v1/linkedin/runs': () => {
          runsCalls += 1
          return jsonResponse(runPage([run({ id: 1 })]))
        },
      },
      'disconnected',
    )
    await screen.findByRole('heading', { name: 'Runs' })
    const afterMount = runsCalls

    // N-08: the first connect ever must not invalidate anything — nothing
    // was missed, since nothing was ever connected to miss it on.
    act(() => setStatus('connected'))
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(runsCalls).toBe(afterMount)

    // A real reconnect — connected, then dropped, then connected again —
    // is the case something could have been missed on, and must refetch.
    act(() => setStatus('disconnected'))
    expect(runsCalls).toBe(afterMount)
    act(() => setStatus('connected'))
    await waitFor(() => expect(runsCalls).toBeGreaterThan(afterMount))
    const afterReconnect = runsCalls

    // And a spurious "connect" while already connected (no drop in between)
    // must not invalidate a second time.
    act(() => setStatus('connected'))
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(runsCalls).toBe(afterReconnect)
  })

  it('never sends a non-GET request as a reaction to any SSE event (R-02)', async () => {
    const { calls, source } = renderLinkedInPage({
      'GET /api/v1/linkedin/runs': () => jsonResponse(runPage([run({ id: 1 })])),
      'GET /api/v1/linkedin/runs/1': () => jsonResponse(run({ id: 1 })),
    })
    await screen.findByRole('heading', { name: 'Runs' })
    calls.length = 0

    act(() => source.emit('run.started', { run_id: 1, kind: 'connections_incremental' }))
    act(() => source.emit('run.progress', { run_id: 1, pages: 1, connections: 40, total: 400 }))
    act(() => source.emit('run.finished', { run_id: 1, status: 'completed' }))
    // Give every invalidated query's background refetch a turn to settle,
    // including one whose promise rejects — a rejection is swallowed by
    // `void queryClient.invalidateQueries(...)`, not surfaced as a request.
    await waitFor(() => expect(calls.length).toBeGreaterThan(0))
    await new Promise((resolve) => setTimeout(resolve, 50))

    expect(calls.every((call) => call.method === 'GET')).toBe(true)
  })
})

describe('the inbox poll and the prefill on the LinkedIn page (#383)', () => {
  it('Check inbox now starts a manual inbox poll and says how it ended, in plain words', async () => {
    let inboxRun = run({ id: 61, kind: 'inbox', status: 'running' })
    const { calls, source } = renderLinkedInPage({
      'POST /api/v1/linkedin/runs': () => jsonResponse({ run_id: 61, task_id: 't' }, 202),
      'GET /api/v1/linkedin/runs/61': () => jsonResponse(inboxRun),
    })

    const button = await screen.findByRole('button', { name: 'Check inbox now' })
    fireEvent.click(button)

    expect(await screen.findByText('Checking the LinkedIn inbox…')).toBeVisible()
    // Off while its poll runs: one click, one poll.
    expect(button).toBeDisabled()
    fireEvent.click(button)
    const started = calls.filter((c) => c.method === 'POST' && c.path === '/api/v1/linkedin/runs')
    expect(started.map((c) => c.body)).toEqual([{ kind: 'inbox', max_visits: null }])

    inboxRun = run({
      id: 61,
      kind: 'inbox',
      status: 'failed',
      stop_reason: 'no_source',
      stop_reason_text: 'the inbox page source is not built yet',
    })
    act(() => source.emit('run.finished', { run_id: 61, status: 'failed' }))

    expect(
      await screen.findByText(/The inbox check stopped: the inbox page source is not built yet/),
    ).toBeVisible()
    expect(button).toBeEnabled()
  })

  it('shows a refused inbox check', async () => {
    renderLinkedInPage({
      'POST /api/v1/linkedin/runs': () => jsonResponse({ detail: 'a run is already running' }, 409),
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Check inbox now' }))

    expect(
      await screen.findByText(/The inbox check did not start: a run is already running/),
    ).toBeVisible()
  })

  it('names the inbox and prefill runs, and their stop reasons, in plain words', async () => {
    renderLinkedInPage({
      'GET /api/v1/linkedin/runs': () =>
        jsonResponse(
          runPage([
            run({
              id: 2,
              kind: 'message_send',
              status: 'failed',
              stop_reason: 'composer_not_found',
              stop_reason_text: 'the message box did not open',
            }),
            run({
              id: 1,
              kind: 'inbox',
              status: 'completed',
              stop_reason: 'end_of_inbox',
              stop_reason_text: 'read every new conversation',
            }),
          ]),
        ),
    })

    expect(await screen.findByRole('button', { name: 'LinkedIn prefill' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Inbox poll' })).toBeVisible()
    expect(screen.getByText('the message box did not open')).toBeVisible()
    expect(screen.getByText('read every new conversation')).toBeVisible()
    const filter = screen.getByLabelText('Filter by kind')
    expect(within(filter).getByRole('option', { name: 'Inbox poll' })).toBeInTheDocument()
    expect(within(filter).getByRole('option', { name: 'LinkedIn prefill' })).toBeInTheDocument()
  })
})
