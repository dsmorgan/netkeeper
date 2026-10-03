import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import type { SendingHours, SendingHoursIn } from './api'
import { sendingHoursProblem, stepTimeWarning } from './sending-hours'
import { SendingHoursSection } from './sending-hours-section'

const DEFAULT: SendingHours = {
  enabled: true,
  days: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'],
  start: '09:00',
  end: '17:00',
  timezone: 'America/New_York',
  summary: 'Mon to Fri, 09:00 to 17:00',
  readable: true,
}

function renderSection(current: SendingHours = DEFAULT) {
  const puts: SendingHoursIn[] = []
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url)
    if (pathname !== '/api/v1/settings/sending-hours') return jsonResponse({}, 500)
    if (request.method === 'GET') return jsonResponse(puts.length === 0 ? current : DEFAULT)
    expect(request.headers.get('X-Netkeeper-Client')).not.toBeNull() // the CSRF header
    const body = (await request.json()) as SendingHoursIn
    puts.push(body)
    return jsonResponse({
      ...DEFAULT,
      ...body,
      summary: body.enabled ? `${body.days.join(', ')}, ${body.start} to ${body.end}` : 'any time',
    })
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <SendingHoursSection />
    </QueryClientProvider>,
  )
  return { puts }
}

describe('SendingHoursSection', () => {
  it('shows the current hours and says only the start ignores them', async () => {
    renderSection()
    const form = within(await screen.findByRole('form', { name: 'Sending hours' }))
    expect(screen.getByText(/Only a campaign's start ignores them/)).toBeVisible()
    const days = within(form.getByRole('group', { name: 'Days' }))
    expect(days.getByRole('checkbox', { name: 'Mon' })).toBeChecked()
    expect(days.getByRole('checkbox', { name: 'Sat' })).not.toBeChecked()
    expect(form.getByLabelText('From')).toHaveValue('09:00')
    expect(form.getByLabelText('To')).toHaveValue('17:00')
  })

  it('saves new days and times', async () => {
    const { puts } = renderSection()
    const form = within(await screen.findByRole('form', { name: 'Sending hours' }))
    const days = within(form.getByRole('group', { name: 'Days' }))
    fireEvent.click(days.getByRole('checkbox', { name: 'Sat' }))
    fireEvent.click(days.getByRole('checkbox', { name: 'Mon' }))
    fireEvent.change(form.getByLabelText('To'), { target: { value: '16:00' } })
    fireEvent.click(form.getByRole('button', { name: 'Save sending hours' }))
    await waitFor(() => expect(puts).toHaveLength(1))
    expect(puts[0]).toEqual({
      enabled: true,
      days: ['Tue', 'Wed', 'Thu', 'Fri', 'Sat'],
      start: '09:00',
      end: '16:00',
    })
    expect(await screen.findByText(/Saved: Tue, Wed, Thu, Fri, Sat, 09:00 to 16:00/)).toBeVisible()
  })

  it('shows the defaults to save when the stored value cannot be read', async () => {
    const { puts } = renderSection({ ...DEFAULT, readable: false })
    expect(await screen.findByText(/no campaign sends until you save them/)).toBeVisible()
    const form = within(screen.getByRole('form', { name: 'Sending hours' }))
    fireEvent.click(form.getByRole('button', { name: 'Save sending hours' }))
    await waitFor(() => expect(puts).toHaveLength(1))
    expect(puts[0]?.days).toEqual(['Mon', 'Tue', 'Wed', 'Thu', 'Fri'])
  })

  it('turns them off with any time', async () => {
    const { puts } = renderSection()
    const form = within(await screen.findByRole('form', { name: 'Sending hours' }))
    fireEvent.click(form.getByRole('checkbox', { name: 'Any time (no sending hours)' }))
    expect(form.getByLabelText('From')).toBeDisabled()
    fireEvent.click(form.getByRole('button', { name: 'Save sending hours' }))
    await waitFor(() => expect(puts[0]?.enabled).toBe(false))
  })

  it('refuses no day, or an end before the start, before saving', async () => {
    const { puts } = renderSection()
    const form = within(await screen.findByRole('form', { name: 'Sending hours' }))
    fireEvent.change(form.getByLabelText('To'), { target: { value: '08:00' } })
    expect(form.getByRole('alert')).toHaveTextContent('The end must be after the start.')
    expect(form.getByRole('button', { name: 'Save sending hours' })).toBeDisabled()
    expect(puts).toEqual([])
  })
})

describe('sending hours helpers', () => {
  it('says what is wrong with a draft', () => {
    const ok = { enabled: true, days: ['Mon' as const], start: '09:00', end: '17:00' }
    expect(sendingHoursProblem(ok)).toBeNull()
    expect(sendingHoursProblem({ ...ok, days: [] })).toBe('Choose at least one day.')
    expect(sendingHoursProblem({ ...ok, enabled: false, days: [] })).toBeNull()
  })

  it('warns for a step time outside the hours only', () => {
    expect(stepTimeWarning(DEFAULT, '22:00')).toMatch(/22:00 is outside the sending hours/)
    expect(stepTimeWarning(DEFAULT, '17:00')).not.toBeNull()
    expect(stepTimeWarning(DEFAULT, '10:00')).toBeNull()
    expect(stepTimeWarning({ ...DEFAULT, enabled: false }, '22:00')).toBeNull()
  })
})
