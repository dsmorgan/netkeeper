import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import { MAX_BARS, bucketSends } from './format'
import { campaign, campaignBackend, results, review, type Call } from './test-support'

const ACTIVE = campaign({ status: 'active', enrollments: { active: 2, replied: 1, bounced: 1 } })

const SOME_RESULTS = results({
  sends_per_day: [
    { date: '2030-06-03', sent: 4 },
    { date: '2030-06-04', sent: 0 },
    { date: '2030-06-05', sent: 2 },
  ],
  steps: [
    { step_id: 101, position: 1, sent: 4, replied: 1, bounced: 1, opted_out: 0 },
    { step_id: 102, position: 2, sent: 2, replied: 2, bounced: 0, opted_out: 1 },
  ],
  totals: { sent: 6, contacted: 4, replied: 3, reply_rate: 0.75, bounced: 1, opted_out: 1 },
})

function backend(calls: Call[] = [], answer = SOME_RESULTS) {
  return campaignBackend(
    { campaign: ACTIVE, review: review({ status: 'active', missing: [] }) },
    { 'GET /api/v1/campaigns/5/results': () => jsonResponse(answer) },
    calls,
  )
}

async function resultsCard(): Promise<HTMLElement> {
  const heading = await screen.findByRole('heading', { name: 'Results' })
  const card = heading.closest('[data-slot="card"]')
  expect(card).not.toBeNull()
  return card as HTMLElement
}

describe('campaign results', () => {
  it('shows the summary tiles and a bar for each day', async () => {
    mockFetch(backend())
    await renderApp('/campaigns/5')

    const card = within(await resultsCard())
    expect(await card.findByRole('link', { name: /^Sent: 6\./ })).toHaveTextContent('to 4 contacts')
    expect(card.getByRole('link', { name: /^Reply rate: 75%\./ })).toHaveTextContent('3 replied')
    expect(card.getByRole('link', { name: /^Bounced: 1\./ })).toBeVisible()
    expect(card.getByRole('link', { name: /^Opted out: 1\./ })).toBeVisible()

    const bars = card.getAllByTestId('sends-bar')
    expect(bars.map((bar) => bar.style.height)).toEqual(['100%', '0%', '50%'])
    expect(bars[0]).toHaveAttribute('title', 'Jun 3: 4 sent')
    expect(card.getByRole('img', { name: /6 over 3 days, at most 4 in a day/ })).toBeVisible()
    expect(card.getByText('Jun 3')).toBeVisible()
    expect(card.getByText('Jun 5')).toBeVisible()
  })

  it('says so when nothing has been sent yet', async () => {
    mockFetch(backend([], results()))
    await renderApp('/campaigns/5')

    const card = within(await resultsCard())
    expect(await card.findByText('Nothing sent yet, so no sends per day.')).toBeVisible()
    expect(card.queryAllByTestId('sends-bar')).toHaveLength(0)
    expect(card.getByRole('link', { name: /^Reply rate: —\./ })).toBeVisible()
  })

  it('adds replied, bounced and opted out to each step', async () => {
    mockFetch(backend())
    await renderApp('/campaigns/5')

    const headers = (await screen.findAllByRole('columnheader')).map((h) => h.textContent)
    expect(headers).toEqual(expect.arrayContaining(['Replied', 'Bounced', 'Opted out']))
    const row = (n: string) => {
      const cells = within(
        screen.getByRole('rowheader', { name: n }).closest('tr') as HTMLElement,
      ).getAllByRole('cell')
      // Replied, bounced, opted out, then the timing editor's cell.
      return cells.slice(-4, -1).map((c) => c.textContent)
    }
    await waitFor(() => expect(row('1')).toEqual(['1', '1', '0']))
    expect(row('2')).toEqual(['2', '0', '1'])
  })

  it('opens the enrollments filtered by a tile’s status, in the URL, and focuses them', async () => {
    const calls: Call[] = []
    mockFetch(backend(calls))
    const { router } = await renderApp('/campaigns/5')

    const card = within(await resultsCard())
    const bounced = await card.findByRole('link', { name: /^Bounced: 1\./ })
    expect(bounced).toHaveAttribute('href', '/campaigns/5?status=bounced')
    fireEvent.click(bounced)

    await waitFor(() => {
      const last = calls.filter((c) => c.path === '/api/v1/campaigns/5/enrollments').at(-1)
      expect(last?.query.get('status')).toBe('bounced')
    })
    expect(router.state.location.search).toEqual({ status: 'bounced' })
    expect(screen.getByLabelText('Status')).toHaveValue('bounced')
    const enrollments = screen
      .getByRole('heading', { name: 'Enrollments' })
      .closest('[data-slot="card"]')
    expect(enrollments).toHaveFocus()

    fireEvent.click(card.getByRole('link', { name: /^Sent: 6\./ }))
    await waitFor(() => expect(screen.getByLabelText('Status')).toHaveValue(''))
    expect(router.state.location.search).toEqual({})
    const last = calls.filter((c) => c.path === '/api/v1/campaigns/5/enrollments').at(-1)
    expect(last?.query.get('status')).toBeNull()

    // Back returns to the bounced filter.
    router.history.back()
    await waitFor(() => expect(screen.getByLabelText('Status')).toHaveValue('bounced'))
  })

  it('reads the filter from the URL on load', async () => {
    const calls: Call[] = []
    mockFetch(backend(calls))
    await renderApp('/campaigns/5?status=opted_out')

    await waitFor(() => expect(screen.getByLabelText('Status')).toHaveValue('opted_out'))
    const first = calls.find((c) => c.path === '/api/v1/campaigns/5/enrollments')
    expect(first?.query.get('status')).toBe('opted_out')
  })

  it('ignores a status in the URL it does not know', async () => {
    const calls: Call[] = []
    mockFetch(backend(calls))
    await renderApp('/campaigns/5?status=nonsense')

    await waitFor(() => expect(screen.getByLabelText('Status')).toHaveValue(''))
    const first = calls.find((c) => c.path === '/api/v1/campaigns/5/enrollments')
    expect(first?.query.get('status')).toBeNull()
  })

  it('draws a long campaign by week so it fits a phone screen', async () => {
    window.innerWidth = 390
    const start = new Date(2030, 0, 1)
    const days = Array.from({ length: 300 }, (_, i) => {
      const day = new Date(start)
      day.setDate(start.getDate() + i)
      const iso = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(
        day.getDate(),
      ).padStart(2, '0')}`
      return { date: iso, sent: i % 7 === 0 ? 2 : 1 }
    })
    mockFetch(backend([], results({ ...SOME_RESULTS, sends_per_day: days })))
    await renderApp('/campaigns/5')

    const card = within(await resultsCard())
    const bars = await card.findAllByTestId('sends-bar')
    // 300 days are 43 weeks, the last one 6 days long.
    expect(bars).toHaveLength(43)
    expect(bars.length).toBeLessThanOrEqual(MAX_BARS)
    // Each bar is at least 2px with 1px between: the widest the chart can need.
    expect(MAX_BARS * 2 + (MAX_BARS - 1)).toBeLessThan(390 - 2 * 16 - 2 * 24)
    expect(bars[0]).toHaveAttribute('title', 'Week of Jan 1: 8 sent')
    const axis = within(card.getByTestId('sends-axis'))
    expect(axis.getByText('Week of Jan 1')).toBeVisible()
    // The 43rd week starts on day 295: October 22.
    expect(axis.getByText('Week of Oct 22')).toBeVisible()
    expect(card.getByText(/^Sends per week \(America\/New_York\)/)).toBeVisible()
    expect(card.getByRole('img', { name: /Sends per week: 343 over 300 days/ })).toBeVisible()
    expect(card.getByRole('figure').className).toContain('overflow-x-auto')
  })

  it('shows no results card while the campaign is a draft', async () => {
    mockFetch(campaignBackend({ campaign: campaign(), review: review() }))
    await renderApp('/campaigns/5')

    await screen.findByRole('heading', { name: 'Steps' })
    expect(screen.queryByRole('heading', { name: 'Results' })).toBeNull()
  })
})

describe('bucketSends', () => {
  const day = (n: number) => ({ date: `2030-06-${String(n).padStart(2, '0')}`, sent: n })

  it('keeps one bar a day while the days fit', () => {
    const days = [1, 2, 3].map(day)
    expect(bucketSends(days, 3)).toEqual({
      days: 1,
      buckets: days.map((d) => ({ start: d.date, sent: d.sent })),
    })
  })

  it('sums by week, then by several weeks, when they do not', () => {
    const days = Array.from({ length: 15 }, (_, i) => day(i + 1))
    expect(bucketSends(days, 14)).toEqual({
      days: 7,
      buckets: [
        { start: '2030-06-01', sent: 28 },
        { start: '2030-06-08', sent: 77 },
        { start: '2030-06-15', sent: 15 },
      ],
    })
    expect(bucketSends(days, 2)).toEqual({
      days: 14,
      buckets: [
        { start: '2030-06-01', sent: 105 },
        { start: '2030-06-15', sent: 15 },
      ],
    })
  })
})
