import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { pollStatusKeys } from '@/features/poll-status/api'
import { jsonResponse, mockFetch } from '@/test/fetch'

import { ScheduleCard } from './schedule-card'
import {
  backend,
  BUDGET,
  RISK_WARNING,
  SCHEDULE_ARMED,
  SCHEDULE_DISARMED,
  type Call,
  type Handler,
} from './test-support'

function renderCard(handlers: Record<string, Handler> = {}, calls: Call[] = []) {
  mockFetch(
    backend(
      {
        'GET /api/v1/linkedin/schedule': () => jsonResponse(SCHEDULE_DISARMED),
        'GET /api/v1/linkedin/budget': () => jsonResponse(BUDGET),
        ...handlers,
      },
      calls,
    ),
  )
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <ScheduleCard />
    </QueryClientProvider>,
  )
  return { calls, queryClient }
}

describe('ScheduleCard', () => {
  it('shows "Disarmed — nothing runs on its own" as the default state', async () => {
    renderCard()
    expect(await screen.findByText('Disarmed — nothing runs on its own')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Arm scheduled runs' })).toBeInTheDocument()
  })

  it('gates arming behind a confirmation dialog that says what arming does', async () => {
    const { calls } = renderCard()
    await screen.findByText('Disarmed — nothing runs on its own')

    fireEvent.click(screen.getByRole('button', { name: 'Arm scheduled runs' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/start contacting linkedin on their own/i)
    expect(dialog).toHaveTextContent(/active hours/i)

    // Nothing was sent yet: the dialog only opened, it did not arm anything.
    expect(calls.some((call) => call.path === '/api/v1/linkedin/schedule/arm')).toBe(false)
  })

  it('shows the profile-view notice in the dialog, and arming still works (#325)', async () => {
    const { calls } = renderCard({
      'GET /api/v1/linkedin/budget': () => jsonResponse(BUDGET),
      'POST /api/v1/linkedin/schedule/arm': () => jsonResponse(SCHEDULE_ARMED),
    })
    await screen.findByText('Disarmed — nothing runs on its own')

    fireEvent.click(screen.getByRole('button', { name: 'Arm scheduled runs' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(await within(dialog).findByRole('note', { name: 'Profile views' })).toHaveTextContent(
      'Who viewed your profile',
    )

    fireEvent.click(within(dialog).getByRole('button', { name: 'Arm scheduled runs' }))
    expect(await screen.findByText(/^Armed/)).toBeInTheDocument()
    expect(calls.some((call) => call.path === '/api/v1/linkedin/schedule/arm')).toBe(true)
  })

  it('shows the profile-visit risk warning in the dialog above 100 a day, and still arms (#318)', async () => {
    const { calls } = renderCard({
      'GET /api/v1/linkedin/budget': () => jsonResponse({ ...BUDGET, risk_warning: RISK_WARNING }),
      'POST /api/v1/linkedin/schedule/arm': () => jsonResponse(SCHEDULE_ARMED),
    })
    await screen.findByText('Disarmed — nothing runs on its own')

    fireEvent.click(screen.getByRole('button', { name: 'Arm scheduled runs' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(
      await within(dialog).findByRole('note', { name: 'Profile-visit risk' }),
    ).toHaveTextContent(RISK_WARNING)

    // The warning informs; it does not stop arming.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Arm scheduled runs' }))
    expect(await screen.findByText(/^Armed/)).toBeInTheDocument()
    expect(calls.some((call) => call.path === '/api/v1/linkedin/schedule/arm')).toBe(true)
  })

  it('shows no risk warning in the dialog at 100 a day or below (#318)', async () => {
    renderCard()
    await screen.findByText('Disarmed — nothing runs on its own')

    fireEvent.click(screen.getByRole('button', { name: 'Arm scheduled runs' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/start contacting linkedin on their own/i)
    expect(within(dialog).queryByRole('note', { name: 'Profile-visit risk' })).toBeNull()
  })

  it('sends confirm: true only once the dialog is confirmed', async () => {
    const { calls } = renderCard({
      'POST /api/v1/linkedin/schedule/arm': (call) => {
        expect(call.body).toEqual({ confirm: true })
        return jsonResponse(SCHEDULE_ARMED)
      },
    })
    await screen.findByText('Disarmed — nothing runs on its own')

    fireEvent.click(screen.getByRole('button', { name: 'Arm scheduled runs' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Arm scheduled runs' }))

    expect(await screen.findByText(/^Armed/)).toBeInTheDocument()
    const armCall = calls.find((call) => call.path === '/api/v1/linkedin/schedule/arm')
    expect(armCall?.body).toEqual({ confirm: true })
  })

  it('cancelling the dialog arms nothing', async () => {
    const { calls } = renderCard()
    await screen.findByText('Disarmed — nothing runs on its own')

    fireEvent.click(screen.getByRole('button', { name: 'Arm scheduled runs' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(calls.some((call) => call.path === '/api/v1/linkedin/schedule/arm')).toBe(false)
  })

  it('never shows "Armed" while the schedule is still loading (R-09)', async () => {
    let resolvePending: (() => void) | undefined
    const pending = new Promise<Response>((resolve) => {
      resolvePending = () => resolve(jsonResponse(SCHEDULE_ARMED))
    })
    renderCard({ 'GET /api/v1/linkedin/schedule': () => pending })

    // Rendering is gated on isSuccess, never on the `armed` value alone (whose
    // `?? false` fallback only matters while data is undefined, i.e. exactly
    // here) — so nothing "Armed" may appear before the fetch resolves.
    expect(screen.queryByText(/Armed/)).not.toBeInTheDocument()

    resolvePending?.()
    expect(await screen.findByText(/^Armed/)).toBeInTheDocument()
  })

  it('never shows "Armed" when the schedule fails to load (R-09)', async () => {
    renderCard({
      'GET /api/v1/linkedin/schedule': () => jsonResponse({ detail: 'boom' }, 500),
    })

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.queryByText(/Armed/)).not.toBeInTheDocument()
  })

  it('offers no Stop-equivalent control and no arm/disarm confirmation is needed to read the state', async () => {
    // Sanity guard alongside R-09/R-10: the loading and error states render
    // no action buttons for arming or disarming at all.
    renderCard({
      'GET /api/v1/linkedin/schedule': () => jsonResponse({ detail: 'boom' }, 500),
    })
    await screen.findByRole('alert')
    expect(screen.queryByRole('button', { name: 'Arm scheduled runs' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Disarm' })).not.toBeInTheDocument()
  })

  it('disarms in one click, with no confirmation dialog', async () => {
    const { calls } = renderCard({
      'GET /api/v1/linkedin/schedule': () => jsonResponse(SCHEDULE_ARMED),
      'POST /api/v1/linkedin/schedule/disarm': () => jsonResponse(SCHEDULE_DISARMED),
    })
    await screen.findByText(/^Armed/)

    fireEvent.click(screen.getByRole('button', { name: 'Disarm' }))

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(await screen.findByText('Disarmed — nothing runs on its own')).toBeInTheDocument()
    expect(calls.some((call) => call.path === '/api/v1/linkedin/schedule/disarm')).toBe(true)
  })

  it('treats a response with no armed field as disarmed, never as an unknown "Armed" (R-09)', async () => {
    // `armed` is required by `ScheduleOut`, so this can only happen from a
    // malformed or partial response -- exactly the case the `?? false`
    // fallback exists for. It is reachable here, behind `isSuccess`, unlike
    // the loading and error cases above, which never render this text at
    // all regardless of the fallback: an incomplete "success" is the one
    // state where the fallback's own value is what decides what shows.
    const { jobs, armed_at, scheduler_running } = SCHEDULE_DISARMED
    renderCard({
      'GET /api/v1/linkedin/schedule': () => jsonResponse({ jobs, armed_at, scheduler_running }),
    })

    expect(await screen.findByText('Disarmed — nothing runs on its own')).toBeInTheDocument()
    expect(screen.queryByText(/^Armed/)).not.toBeInTheDocument()
  })

  it('arms again after a disarm, even after a mutation that settled before pending ever rendered true (B2)', async () => {
    const calls: Call[] = []
    let armed = false
    renderCard(
      {
        'GET /api/v1/linkedin/schedule': () =>
          jsonResponse(armed ? SCHEDULE_ARMED : SCHEDULE_DISARMED),
        'POST /api/v1/linkedin/schedule/arm': () => {
          armed = true
          return jsonResponse(SCHEDULE_ARMED)
        },
        'POST /api/v1/linkedin/schedule/disarm': () => {
          armed = false
          return jsonResponse(SCHEDULE_DISARMED)
        },
      },
      calls,
    )

    // The same `ConfirmDialog` instance is reused every time `asking` toggles
    // -- it is never unmounted between an arm and the next one -- so a stuck
    // same-tick guard from the first arm would silently drop the second.
    for (let cycle = 0; cycle < 2; cycle += 1) {
      fireEvent.click(await screen.findByRole('button', { name: 'Arm scheduled runs' }))
      const dialog = await screen.findByRole('alertdialog')
      fireEvent.click(within(dialog).getByRole('button', { name: 'Arm scheduled runs' }))
      fireEvent.click(await screen.findByRole('button', { name: 'Disarm' }))
      await screen.findByRole('button', { name: 'Arm scheduled runs' })
    }

    expect(calls.filter((call) => call.path.endsWith('/arm'))).toHaveLength(2)
  })

  it('pauses and unpauses an armed schedule without disarming it (#324)', async () => {
    let paused = false
    const calls: Call[] = []
    const { queryClient } = renderCard(
      {
        'GET /api/v1/linkedin/schedule': () =>
          jsonResponse(
            paused
              ? { ...SCHEDULE_ARMED, paused: true, paused_at: '2026-09-22T10:00:00Z' }
              : SCHEDULE_ARMED,
          ),
        'POST /api/v1/linkedin/schedule/pause': () => {
          paused = true
          return jsonResponse({
            ...SCHEDULE_ARMED,
            paused: true,
            paused_at: '2026-09-22T10:00:00Z',
          })
        },
        'POST /api/v1/linkedin/schedule/unpause': () => {
          paused = false
          return jsonResponse(SCHEDULE_ARMED)
        },
      },
      calls,
    )

    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    fireEvent.click(await screen.findByRole('button', { name: 'Pause schedule' }))
    expect(await screen.findByText(/no new scheduled run starts/)).toBeInTheDocument()
    // The header's poll status says "paused" without waiting for its minute (#401).
    expect(invalidate).toHaveBeenCalledWith({ queryKey: pollStatusKeys.all })
    // Still armed: pausing is not disarming, and no confirmation was needed.
    expect(screen.getByRole('button', { name: 'Disarm' })).toBeInTheDocument()
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Unpause schedule' }))
    expect(await screen.findByRole('button', { name: 'Pause schedule' })).toBeInTheDocument()
    expect(calls.map((call) => `${call.method} ${call.path}`)).toEqual(
      expect.arrayContaining([
        'POST /api/v1/linkedin/schedule/pause',
        'POST /api/v1/linkedin/schedule/unpause',
      ]),
    )
    expect(calls.some((call) => call.path.endsWith('/disarm'))).toBe(false)
  })

  it('offers no pause while disarmed', async () => {
    renderCard()
    await screen.findByText('Disarmed — nothing runs on its own')
    expect(screen.queryByRole('button', { name: 'Pause schedule' })).not.toBeInTheDocument()
  })
})
