import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import type { EnrollOut, SkippedContact } from './api'
import {
  ENROLLMENTS,
  campaign,
  campaignBackend,
  guardDetails,
  review,
  type Call,
} from './test-support'

function recent(contactId: number, name: string, channel = 'email'): SkippedContact {
  return {
    contact_id: contactId,
    name,
    reasons: ['contacted in the last 30 days'],
    reason_codes: ['contacted_recently'],
    overridable: true,
    last_contacted_at: '2030-06-10T15:00:00Z',
    last_contacted_channel: channel,
  }
}

const SKIPPED: SkippedContact[] = [
  recent(501, 'Odile Fenwick'),
  recent(502, 'Barnaby Thistlewood', 'linkedin'),
  {
    // Contacted recently, and another guard skips it too: never offered.
    contact_id: 503,
    name: 'Cressida Holloway',
    reasons: ['in another campaign', 'contacted in the last 30 days'],
    reason_codes: ['in_another_campaign', 'contacted_recently'],
    overridable: false,
    last_contacted_at: '2030-06-11T15:00:00Z',
    last_contacted_channel: 'email',
  },
  {
    contact_id: 504,
    name: 'Ambrose Pellow',
    reasons: ['no LinkedIn member id yet (a connections sync adds it)'],
    reason_codes: ['no_linkedin'],
    overridable: false,
  },
]

const OVERRIDDEN: EnrollOut = {
  campaign_id: 5,
  enrolled: 1,
  already: 2,
  excluded: 0,
  removed: 0,
  pending: 3,
  summary: '3 will start, 3 skipped (1 contacted in the last 30 days, 1 in another campaign)',
  excluded_summary: '1 will start, none skipped',
  overridden: 1,
}

function setup(calls: Call[], answer: () => Response = () => jsonResponse(OVERRIDDEN)) {
  mockFetch(
    campaignBackend(
      { campaign: campaign(), review: review({ status: 'draft' }) },
      {
        'GET /api/v1/campaigns/5/review/guards': () =>
          jsonResponse(
            guardDetails({
              summary: '2 will start, 4 skipped',
              skipped: SKIPPED,
              skipped_total: 4,
            }),
          ),
        'POST /api/v1/campaigns/5/enroll': answer,
      },
      calls,
    ),
  )
}

async function openList() {
  await renderApp('/campaigns/5')
  fireEvent.click(
    await screen.findByRole('button', { name: 'Show contacts skipped for recent contact' }),
  )
  return screen.findByRole('region', { name: 'Contacted recently' })
}

describe('overriding the recent-contact guard (#446)', () => {
  it('fetches nothing until asked', async () => {
    const calls: Call[] = []
    setup(calls)
    await renderApp('/campaigns/5')
    await screen.findByRole('button', { name: 'Show contacts skipped for recent contact' })
    expect(calls.some((c) => c.path.endsWith('/review/guards'))).toBe(false)
  })

  it('offers only the contacts the recent-contact guard alone skips, with their last contact', async () => {
    setup([])
    const list = within(await openList())

    expect(list.getByRole('checkbox', { name: /Odile Fenwick/ })).toBeVisible()
    expect(list.getByText(/by email/)).toBeVisible()
    expect(list.getByText(/by LinkedIn/)).toBeVisible()
    expect(list.queryByText('Cressida Holloway')).toBeNull()
    expect(list.queryByText('Ambrose Pellow')).toBeNull()
    expect(list.getByText(/1 more contact was contacted recently, but another guard/)).toBeVisible()
    expect(list.getByRole('button', { name: 'Enroll anyway' })).toBeDisabled()
  })

  it('confirms the count, then sends exactly the picked ids with confirm', async () => {
    const calls: Call[] = []
    setup(calls)
    const list = within(await openList())

    fireEvent.click(list.getByRole('checkbox', { name: /Odile Fenwick/ }))
    fireEvent.click(list.getByRole('button', { name: 'Enroll anyway (1)' }))

    const dialog = await screen.findByRole('alertdialog', {
      name: 'Enroll 1 contact contacted recently?',
    })
    expect(dialog).toHaveTextContent('Someone contacted this contact in the last 30 days')
    expect(dialog).toHaveTextContent('Every other guard still applies')
    expect(calls.some((c) => c.method === 'POST')).toBe(false)

    fireEvent.click(within(dialog).getByRole('button', { name: 'Enroll anyway' }))

    await waitFor(() =>
      expect(calls.find((c) => c.path === '/api/v1/campaigns/5/enroll')?.body).toEqual({
        override_recent_contact: [501],
        confirm: true,
      }),
    )
    expect(
      await screen.findByText('The recent-contact guard was overridden for 1 contact.'),
    ).toBeVisible()
  })

  it('selects all of them at once', async () => {
    const calls: Call[] = []
    setup(calls)
    const list = within(await openList())

    fireEvent.click(list.getByRole('checkbox', { name: 'Select all 2' }))
    fireEvent.click(list.getByRole('button', { name: 'Enroll anyway (2)' }))
    const dialog = await screen.findByRole('alertdialog', {
      name: 'Enroll 2 contacts contacted recently?',
    })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Enroll anyway' }))

    await waitFor(() =>
      expect(calls.find((c) => c.path === '/api/v1/campaigns/5/enroll')?.body).toEqual({
        override_recent_contact: [501, 502],
        confirm: true,
      }),
    )
  })

  it('cancelling sends nothing', async () => {
    const calls: Call[] = []
    setup(calls)
    const list = within(await openList())

    fireEvent.click(list.getByRole('checkbox', { name: /Barnaby Thistlewood/ }))
    fireEvent.click(list.getByRole('button', { name: 'Enroll anyway (1)' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(calls.some((c) => c.method === 'POST')).toBe(false)
  })

  it('shows a refusal in the dialog', async () => {
    setup([], () => jsonResponse({ detail: 'not in campaign 5’s audience: 501' }, 422))
    const list = within(await openList())

    fireEvent.click(list.getByRole('checkbox', { name: /Odile Fenwick/ }))
    fireEvent.click(list.getByRole('button', { name: 'Enroll anyway (1)' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Enroll anyway' }))

    expect(await within(dialog).findByText(/not in campaign 5/)).toBeVisible()
  })
})

describe('the enrollment row', () => {
  it('says when the recent-contact guard was overridden', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review({ status: 'draft' }) },
        {
          'GET /api/v1/campaigns/5/enrollments': () =>
            jsonResponse({
              ...ENROLLMENTS,
              items: [
                {
                  ...ENROLLMENTS.items[0],
                  status: 'pending',
                  recent_contact_override_at: '2030-06-15T12:00:00Z',
                  recent_contact_override_by: 1,
                },
                ENROLLMENTS.items[1],
              ],
            }),
        },
      ),
    )
    await renderApp('/campaigns/5')

    const row = (await screen.findByRole('link', { name: 'Rosalind Quillfeather' })).closest('tr')
    expect(row).not.toBeNull()
    expect(row as HTMLElement).toHaveTextContent(
      /Enrolled although contacted recently: recent-contact guard overridden/,
    )
    const other = screen.getByRole('link', { name: 'Tobias Marrowbone' }).closest('tr')
    expect(other as HTMLElement).not.toHaveTextContent(/overridden/)
  })
})
