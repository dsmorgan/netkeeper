import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { resetFakeEventSource } from '@/test/fake-event-source'
import { jsonResponse } from '@/test/fetch'

import { renderLinkedInPage } from './test-render'
import {
  BROWSER_HEALTH,
  HEAT,
  STATUS_CHECKPOINT,
  STATUS_CLEAR,
  STATUS_LOGGED_OUT,
  type Call,
} from './test-support'
import type { Heat } from './types'

afterEach(() => {
  resetFakeEventSource()
})

const posts = (calls: Call[], path: string) =>
  calls.filter((call) => call.method === 'POST' && call.path === path)

describe('clearing the session flag (#181)', () => {
  const FLAG_PATH = '/api/v1/linkedin/session-flag/clear'

  it('asks first, then sends the exact flag that was shown', async () => {
    let flagged = true
    const { calls } = renderLinkedInPage({
      'GET /api/v1/linkedin/status': () => jsonResponse(flagged ? STATUS_CHECKPOINT : STATUS_CLEAR),
      [`POST ${FLAG_PATH}`]: () => {
        flagged = false
        return jsonResponse(STATUS_CLEAR)
      },
    })
    const banner = await screen.findByRole('alert')

    fireEvent.click(within(banner).getByRole('button', { name: 'Clear flag…' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/cannot tell whether the checkpoint is resolved/i)
    expect(dialog).toHaveTextContent(/looks healthy/i)
    // Opening the dialog sent nothing.
    expect(posts(calls, FLAG_PATH)).toHaveLength(0)

    fireEvent.click(within(dialog).getByRole('button', { name: 'Clear flag' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(posts(calls, FLAG_PATH).map((call) => call.body)).toEqual([
      {
        confirm: true,
        outcome: 'checkpoint',
        flagged_at: STATUS_CHECKPOINT.session_flagged_at,
      },
    ])
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Clear flag…' })).not.toBeInTheDocument(),
    )
  })

  it('cancelling sends nothing', async () => {
    const { calls } = renderLinkedInPage({
      'GET /api/v1/linkedin/status': () => jsonResponse(STATUS_CHECKPOINT),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Clear flag…' }))
    const dialog = await screen.findByRole('alertdialog')

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(posts(calls, FLAG_PATH)).toHaveLength(0)
  })

  it('shows a refusal in the dialog and rereads the flag', async () => {
    let statusReads = 0
    renderLinkedInPage({
      'GET /api/v1/linkedin/status': () => {
        statusReads += 1
        return jsonResponse(STATUS_CHECKPOINT)
      },
      [`POST ${FLAG_PATH}`]: () =>
        jsonResponse(
          { detail: 'the session flag changed since you confirmed; not clearing it' },
          409,
        ),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Clear flag…' }))
    const dialog = await screen.findByRole('alertdialog')
    const readsBefore = statusReads

    fireEvent.click(within(dialog).getByRole('button', { name: 'Clear flag' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      /changed since you confirmed/,
    )
    await waitFor(() => expect(statusReads).toBeGreaterThan(readsBefore))
  })

  it('sends the flag shown when the dialog opened, even after a newer one arrives (#364 B1)', async () => {
    const newer = { ...STATUS_CHECKPOINT, session_flagged_at: '2026-09-20T11:30:00Z' }
    let current = STATUS_CHECKPOINT
    const { calls, source } = renderLinkedInPage({
      'GET /api/v1/linkedin/status': () => jsonResponse(current),
      [`POST ${FLAG_PATH}`]: () =>
        jsonResponse(
          { detail: 'the session flag changed since you confirmed; not clearing it' },
          409,
        ),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Clear flag…' }))
    const dialog = await screen.findByRole('alertdialog')
    const shown = dialog.textContent

    // A run ends while the dialog is open and the status refetch finds a newer flag.
    let statusReads = 0
    current = newer
    const countBefore = calls.filter((call) => call.path === '/api/v1/linkedin/status').length
    act(() => source.emit('run.finished', { run_id: 3, status: 'aborted' }))
    await waitFor(() => {
      statusReads = calls.filter((call) => call.path === '/api/v1/linkedin/status').length
      expect(statusReads).toBeGreaterThan(countBefore)
    })
    expect(dialog.textContent).toBe(shown)

    fireEvent.click(within(dialog).getByRole('button', { name: 'Clear flag' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      /changed since you confirmed/,
    )
    expect(posts(calls, FLAG_PATH).map((call) => call.body)).toEqual([
      {
        confirm: true,
        outcome: 'checkpoint',
        flagged_at: STATUS_CHECKPOINT.session_flagged_at,
      },
    ])
  })

  it('offers no button for a logged-out flag, which preflight clears', async () => {
    renderLinkedInPage({ 'GET /api/v1/linkedin/status': () => jsonResponse(STATUS_LOGGED_OUT) })
    const banner = await screen.findByRole('alert')
    expect(within(banner).queryByRole('button')).not.toBeInTheDocument()
  })
})

describe('clearing heat (#181)', () => {
  const HEAT_PATH = '/api/v1/linkedin/heat/clear'
  const RAISED: Heat = {
    ...HEAT,
    score: 2.75,
    multiplier: 2.1,
    tripped: true,
    last_raised_at: '2026-09-23T09:30:00Z',
    resumes_at: '2026-09-23T11:00:00Z',
  }
  const CLEARED: Heat = { ...HEAT, cleared_at: '2026-09-23T10:00:00Z' }

  it('offers nothing while heat was never raised', async () => {
    renderLinkedInPage()
    await screen.findByText('Pacing multiplier: 1.00×')
    expect(screen.queryByRole('button', { name: 'Clear heat…' })).not.toBeInTheDocument()
  })

  it('asks first, then sends when heat was last raised, and shows the cleared heat', async () => {
    const { calls } = renderLinkedInPage({
      'GET /api/v1/linkedin/heat': () => jsonResponse(RAISED),
      [`POST ${HEAT_PATH}`]: () => jsonResponse(CLEARED),
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Clear heat…' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('2.75 of 2.50')
    expect(dialog).toHaveTextContent(/only if you are sure the block that raised it was something/i)
    expect(posts(calls, HEAT_PATH)).toHaveLength(0)

    fireEvent.click(within(dialog).getByRole('button', { name: 'Clear heat' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(posts(calls, HEAT_PATH).map((call) => call.body)).toEqual([
      { confirm: true, last_raised_at: RAISED.last_raised_at },
    ])
    expect(await screen.findByText(/^Cleared /)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Clear heat…' })).not.toBeInTheDocument()
  })

  it('sends the heat shown when the dialog opened, even after a refetch raises it (#364 B1)', async () => {
    const first: Heat = {
      ...RAISED,
      score: 1,
      tripped: false,
      last_raised_at: '2026-09-23T09:30:00Z',
    }
    const second: Heat = { ...first, score: 2, last_raised_at: '2026-09-23T09:45:00Z' }
    let current = first
    const { calls, source } = renderLinkedInPage({
      'GET /api/v1/linkedin/heat': () => jsonResponse(current),
      [`POST ${HEAT_PATH}`]: () =>
        jsonResponse(
          {
            detail: 'heat was raised again since you confirmed; not clearing it. Look at it again',
          },
          409,
        ),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Clear heat…' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('1.00 of 2.50')

    current = second
    act(() => source.emit('run.finished', { run_id: 3, status: 'completed' }))
    // The panel behind the dialog shows the new heat; the dialog keeps what was confirmed.
    expect(await screen.findByText('2.00 / 2.50')).toBeInTheDocument()
    expect(dialog).toHaveTextContent('1.00 of 2.50')

    fireEvent.click(within(dialog).getByRole('button', { name: 'Clear heat' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/raised again/)
    expect(posts(calls, HEAT_PATH).map((call) => call.body)).toEqual([
      { confirm: true, last_raised_at: first.last_raised_at },
    ])
  })

  it('says so when a run is going, without blocking the clear', async () => {
    const { calls } = renderLinkedInPage({
      'GET /api/v1/linkedin/status': () => jsonResponse({ ...STATUS_CLEAR, running_run_id: 7 }),
      'GET /api/v1/linkedin/heat': () => jsonResponse(RAISED),
      [`POST ${HEAT_PATH}`]: () => jsonResponse(CLEARED),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Clear heat…' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(await within(dialog).findByText(/Run 7 is running/)).toBeInTheDocument()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Clear heat' }))

    await waitFor(() => expect(posts(calls, HEAT_PATH)).toHaveLength(1))
  })

  it('shows a refusal in the dialog', async () => {
    renderLinkedInPage({
      'GET /api/v1/linkedin/heat': () => jsonResponse(RAISED),
      [`POST ${HEAT_PATH}`]: () =>
        jsonResponse(
          {
            detail: 'heat was raised again since you confirmed; not clearing it. Look at it again',
          },
          409,
        ),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Clear heat…' }))
    const dialog = await screen.findByRole('alertdialog')

    fireEvent.click(within(dialog).getByRole('button', { name: 'Clear heat' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/raised again/)
  })
})

describe('last known browser state (#181)', () => {
  it('shows what was recorded, and says the page never checks the browser', async () => {
    renderLinkedInPage()
    const section = await screen.findByRole('region', { name: 'Last known browser state' })
    expect(section).toHaveTextContent('not checked yet')
    expect(section).toHaveTextContent('unknown')
    expect(section).toHaveTextContent(/never checks the browser itself/i)
  })

  it('says when a run could not reach Chrome', async () => {
    renderLinkedInPage({
      'GET /api/v1/linkedin/browser/health': () =>
        jsonResponse({
          ...BROWSER_HEALTH,
          session_status: 'on',
          session_summary: 'last confirmed logged in 2026-09-23 08:00 by preflight (4 h ago)',
          session_warnings: [],
          chrome_unreachable_at: '2026-09-23T11:00:00Z',
          chrome_unreachable_run_id: 12,
        }),
    })
    const section = await screen.findByRole('region', { name: 'Last known browser state' })
    expect(within(section).getByRole('alert')).toHaveTextContent(/Run 12 could not reach Chrome/)
  })

  it('refetches when a run finishes, over SSE', async () => {
    let reads = 0
    const { source } = renderLinkedInPage({
      'GET /api/v1/linkedin/browser/health': () => {
        reads += 1
        return jsonResponse(BROWSER_HEALTH)
      },
    })
    await screen.findByRole('region', { name: 'Last known browser state' })
    const before = reads

    act(() => source.emit('run.finished', { run_id: 3, status: 'failed' }))

    await waitFor(() => expect(reads).toBeGreaterThan(before))
  })
})
