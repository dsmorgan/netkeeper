import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import { ScheduleCard } from './schedule-card'
import { backend, SCHEDULE_ARMED, SCHEDULE_DISARMED, type Call, type Handler } from './test-support'

function renderCard(handlers: Record<string, Handler> = {}, calls: Call[] = []) {
  mockFetch(
    backend(
      { 'GET /api/v1/linkedin/schedule': () => jsonResponse(SCHEDULE_DISARMED), ...handlers },
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
})
