import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

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

  it('opens the enrollments filtered by a tile’s status', async () => {
    const calls: Call[] = []
    mockFetch(backend(calls))
    await renderApp('/campaigns/5')

    const card = within(await resultsCard())
    const bounced = await card.findByRole('link', { name: /^Bounced: 1\./ })
    expect(bounced).toHaveAttribute('href', '#enrollments')
    fireEvent.click(bounced)

    await waitFor(() => {
      const last = calls.filter((c) => c.path === '/api/v1/campaigns/5/enrollments').at(-1)
      expect(last?.query.get('status')).toBe('bounced')
    })
    expect(screen.getByLabelText('Status')).toHaveValue('bounced')

    fireEvent.click(card.getByRole('link', { name: /^Sent: 6\./ }))
    await waitFor(() => {
      const last = calls.filter((c) => c.path === '/api/v1/campaigns/5/enrollments').at(-1)
      expect(last?.query.get('status')).toBeNull()
    })
    expect(screen.getByLabelText('Status')).toHaveValue('')
  })

  it('shows no results card while the campaign is a draft', async () => {
    mockFetch(campaignBackend({ campaign: campaign(), review: review() }))
    await renderApp('/campaigns/5')

    await screen.findByRole('heading', { name: 'Steps' })
    expect(screen.queryByRole('heading', { name: 'Results' })).toBeNull()
  })
})
