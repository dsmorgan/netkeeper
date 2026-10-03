/**
 * The scheduled start and step timing (#338): the activate dialog's start, "Now", the
 * warning outside the suggested slots (never a block), the reminder, changing the start
 * until the first send, and a step's own day offset and time of day.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import type { Campaign } from './api'
import { formatStart, fromLocalInput, timingText, toLocalInput } from './format'
import {
  DEFAULT_START,
  campaign,
  campaignBackend,
  review,
  startOptions,
  type Call,
} from './test-support'

function ready() {
  return {
    campaign: campaign({ status: 'reviewing', enrollments: { pending: 2 } }) as Campaign,
    review: review({ missing: [] }),
  }
}

async function openActivate() {
  await renderApp('/campaigns/5')
  expect(await screen.findByText('Every requirement is met.')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: 'Activate' }))
  return within(await screen.findByRole('alertdialog'))
}

function activations(calls: Call[]) {
  return calls.filter((c) => c.method === 'POST' && c.path.endsWith('/activate'))
}

describe('format', () => {
  it('says a start as the campaign page does, in the reader’s zone', () => {
    const local = new Date(2030, 5, 18, 9, 0).toISOString() // a Tuesday, 09:00 here
    expect(formatStart(local)).toBe('Tue Jun 18, 09:00')
    expect(formatStart(null)).toBe('—')
  })

  it('round-trips a datetime-local value', () => {
    const iso = new Date(2030, 5, 18, 22, 30).toISOString()
    expect(toLocalInput(iso)).toBe('2030-06-18T22:30')
    expect(fromLocalInput('2030-06-18T22:30')).toBe(iso)
    expect(fromLocalInput('')).toBeNull()
  })

  it('says each step’s timing', () => {
    expect(timingText({ position: 1, delay_days: 0, send_time: null })).toBe('at the start')
    expect(timingText({ position: 2, delay_days: 7, send_time: null })).toBe(
      '7 days after the step before, next suggested slot',
    )
    expect(timingText({ position: 2, delay_days: 3, send_time: '22:00' })).toBe(
      '3 days after the step before, at 22:00',
    )
    expect(timingText({ position: 1, delay_days: 1, send_time: null })).toBe(
      '1 day after the start, next suggested slot',
    )
  })
})

describe('activating with a scheduled start', () => {
  it('defaults to the next Tuesday at 09:00, with the suggestion and the reminder', async () => {
    const calls: Call[] = []
    const state = ready()
    mockFetch(
      campaignBackend(
        state,
        {
          'POST /api/v1/campaigns/5/activate': () => {
            state.campaign = { ...state.campaign, status: 'active', starts_at: DEFAULT_START }
            return jsonResponse({ ...state.review, status: 'active' })
          },
        },
        calls,
      ),
    )
    const dialog = await openActivate()

    expect(await dialog.findByText(`Starts ${formatStart(DEFAULT_START)}`)).toBeVisible()
    expect(dialog.getByText('Most effective: Tue–Thu mornings.')).toBeVisible()
    expect(dialog.getByText('serve', { selector: 'code' }).parentElement).toHaveTextContent(
      'netkeeper sends only while serve is running and this Mac is awake.',
    )
    expect(dialog.getByLabelText('Start date and time')).toHaveValue(toLocalInput(DEFAULT_START))
    expect(dialog.getByText(/Sending hours: Mon to Fri, 09:00 to 17:00\./)).toBeVisible()
    expect(dialog.queryByText('Not a suggested time')).toBeNull()

    fireEvent.click(dialog.getByRole('button', { name: 'Activate campaign' }))
    await waitFor(() => expect(activations(calls)).toHaveLength(1))
    const body = activations(calls)[0]?.body as { starts_at: string }
    expect(Date.parse(body.starts_at)).toBe(Date.parse(DEFAULT_START))
  })

  it('starts now in one click', async () => {
    const calls: Call[] = []
    const state = ready()
    mockFetch(
      campaignBackend(
        state,
        {
          'POST /api/v1/campaigns/5/activate': () => {
            state.campaign = { ...state.campaign, status: 'active' }
            return jsonResponse({ ...state.review, status: 'active' })
          },
        },
        calls,
      ),
    )
    const dialog = await openActivate()
    await dialog.findByText(`Starts ${formatStart(DEFAULT_START)}`)

    const before = Date.now()
    fireEvent.click(dialog.getByRole('button', { name: 'Now' }))
    expect(dialog.getByText('Starts now')).toBeVisible()
    fireEvent.click(dialog.getByRole('button', { name: 'Activate campaign' }))
    await waitFor(() => expect(activations(calls)).toHaveLength(1))
    const sent = Date.parse((activations(calls)[0]?.body as { starts_at: string }).starts_at)
    expect(sent).toBeGreaterThanOrEqual(before - 1000)
    expect(sent).toBeLessThanOrEqual(Date.now() + 1000)
  })

  it('warns outside the suggested slots and still activates', async () => {
    const calls: Call[] = []
    const state = ready()
    const late = '2030-06-22T22:00'
    mockFetch(
      campaignBackend(
        state,
        {
          'GET /api/v1/campaigns/5/start-options': (call) => {
            const at = call.query.get('at')
            return jsonResponse(
              startOptions({
                at,
                warning:
                  at === fromLocalInput(late)
                    ? 'That is outside the suggested slots (Tue to Thu, 09:00 to 16:30). netkeeper will still send then.'
                    : null,
              }),
            )
          },
          'POST /api/v1/campaigns/5/activate': () => {
            state.campaign = { ...state.campaign, status: 'active' }
            return jsonResponse({ ...state.review, status: 'active' })
          },
        },
        calls,
      ),
    )
    const dialog = await openActivate()
    await dialog.findByText(`Starts ${formatStart(DEFAULT_START)}`)

    fireEvent.change(dialog.getByLabelText('Start date and time'), { target: { value: late } })
    expect(await dialog.findByText('Not a suggested time')).toBeVisible()
    expect(dialog.getByText(/netkeeper will still send then/)).toBeVisible()
    const confirm = dialog.getByRole('button', { name: 'Activate campaign' })
    expect(confirm).toBeEnabled()
    fireEvent.click(confirm)
    await waitFor(() => expect(activations(calls)).toHaveLength(1))
    expect(activations(calls)[0]?.body).toEqual({ starts_at: fromLocalInput(late) })
  })
})

describe('the campaign page', () => {
  it('shows the start and changes it until the first send', async () => {
    const calls: Call[] = []
    const start = '2030-06-25T13:00:00Z'
    const state = {
      campaign: campaign({
        status: 'active',
        enrollments: { active: 2 },
        starts_at: start,
        start_editable: true,
        next_action_at: start,
      }),
      review: review({ status: 'active', missing: [] }),
    }
    mockFetch(
      campaignBackend(
        state,
        {
          'PUT /api/v1/campaigns/5/start': (call) => {
            const startsAt = (call.body as { starts_at: string }).starts_at
            state.campaign = { ...state.campaign, starts_at: startsAt }
            return jsonResponse(state.campaign)
          },
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    expect(await screen.findByText(formatStart(start))).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Change start' }))
    const form = within(screen.getByRole('region', { name: 'Change start' }))
    expect(form.getByLabelText('Start date and time')).toHaveValue(toLocalInput(start))
    fireEvent.click(form.getByRole('button', { name: 'Now' }))
    fireEvent.click(form.getByRole('button', { name: 'Save start' }))

    await waitFor(() =>
      expect(calls.filter((c) => c.method === 'PUT' && c.path.endsWith('/start'))).toHaveLength(1),
    )
    expect(await screen.findByRole('button', { name: 'Change start' })).toBeVisible()
  })

  it('offers no change once the campaign has sent', async () => {
    mockFetch(
      campaignBackend({
        campaign: campaign({
          status: 'active',
          starts_at: '2030-06-25T13:00:00Z',
          start_editable: false,
        }),
        review: review({ status: 'active', missing: [] }),
      }),
    )
    await renderApp('/campaigns/5')
    expect(await screen.findByText(formatStart('2030-06-25T13:00:00Z'))).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Change start' })).toBeNull()
  })

  it('sets a step’s day offset and time of day', async () => {
    const calls: Call[] = []
    const state = {
      campaign: campaign({ status: 'active', starts_at: '2030-06-25T13:00:00Z' }),
      review: review({ status: 'active', missing: [] }),
    }
    mockFetch(
      campaignBackend(
        state,
        {
          'PUT /api/v1/campaigns/5/steps/102/schedule': (call) => {
            const body = call.body as { delay_days: number; send_time: string | null }
            state.campaign = {
              ...state.campaign,
              steps: state.campaign.steps.map((s) =>
                s.id === 102 ? { ...s, delay_days: body.delay_days, send_time: body.send_time } : s,
              ),
            }
            return jsonResponse(state.campaign)
          },
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    expect(
      await screen.findByText(/7 days after the step before, next suggested slot/),
    ).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Edit step 2 timing' }))
    const form = within(screen.getByRole('form', { name: 'Step 2 timing' }))
    fireEvent.change(form.getByLabelText('Days after the step before'), { target: { value: '3' } })
    fireEvent.change(form.getByLabelText(/Time of day/), { target: { value: '22:00' } })
    expect(await form.findByText(/22:00 is outside the sending hours/)).toBeVisible()
    fireEvent.click(form.getByRole('button', { name: 'Save timing' }))

    expect(await screen.findByText(/3 days after the step before, at 22:00/)).toBeVisible()
    const puts = calls.filter((c) => c.method === 'PUT')
    expect(puts.map((c) => c.body)).toEqual([{ delay_days: 3, send_time: '22:00' }])
  })
})
