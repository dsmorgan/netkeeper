/**
 * A step adopts the newest version of its template (#397): the campaign page offers it
 * for an active or paused campaign, the confirm shows the change and who gets it, and a
 * refused version cannot be confirmed.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import type { Adoption, Campaign } from './api'
import { STEPS, campaign, campaignBackend, message, review, type Call } from './test-support'

function adoption(overrides: Partial<Adoption> = {}): Adoption {
  return {
    campaign_id: 5,
    campaign_status: 'paused',
    step_id: 101,
    position: 1,
    current: {
      template_id: 11,
      name: 'Catching up',
      version: 1,
      subject: 'Hi',
      body: 'Hi {{ first_name }}',
    },
    newest: {
      template_id: 13,
      name: 'Catching up',
      version: 2,
      subject: 'Hi',
      body: 'Hello {{ first_name }}',
    },
    diff: '--- v1\n+++ v2\n@@ -1,3 +1,3 @@\n-Hi {{ first_name }}\n+Hello {{ first_name }}',
    errors: [],
    warnings: [],
    refusal: null,
    affected_total: 2,
    affected: [
      { enrollment_id: 1, contact_id: 1, contact_name: 'Ada Lovelace', status: 'active' },
      { enrollment_id: 2, contact_id: 2, contact_name: 'Grace Hopper', status: 'active' },
    ],
    released: 1,
    kept: { sent: 3, scheduled: 1 },
    open_messages: [
      { message_id: 9, enrollment_id: 3, contact_name: 'Alan Turing', status: 'scheduled' },
    ],
    samples: [message({ body: 'Hello Ada' })],
    blocked_total: 0,
    blocked: [],
    blocked_capped: false,
    fingerprint: 'adopt-fp',
    ...overrides,
  }
}

const [STEP_1, STEP_2] = STEPS as [Campaign['steps'][number], Campaign['steps'][number]]

function withNewer(status: Campaign['status']): Campaign {
  return campaign({
    status,
    missing: [],
    steps: [{ ...STEP_1, newest_template_version: 2 }, STEP_2],
  })
}

describe('template adoption', () => {
  it('shows the change, then adopts it with the preview fingerprint', async () => {
    const calls: Call[] = []
    const state = {
      campaign: withNewer('paused'),
      review: review({ status: 'paused', missing: [] }),
    }
    mockFetch(
      campaignBackend(
        state,
        {
          'GET /api/v1/campaigns/5/steps/101/adoption': () => jsonResponse(adoption()),
          'POST /api/v1/campaigns/5/steps/101/adopt': () => {
            state.campaign = campaign({
              status: 'paused',
              missing: [],
              steps: [
                {
                  ...STEP_1,
                  template_version: 2,
                  template_adopted_at: '2030-06-18T13:00:00Z',
                  template_adopted_from_version: 1,
                },
                STEP_2,
              ],
            })
            return jsonResponse({
              from_version: 1,
              to_version: 2,
              released: 1,
              affected_total: 2,
              adopted_at: '2030-06-18T13:00:00Z',
              campaign: state.campaign,
            })
          },
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    fireEvent.click(await screen.findByRole('button', { name: 'Use v2 for step 1' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Use v2 for step 1?' })
    expect(await within(dialog).findByLabelText('Changes')).toHaveTextContent(
      '+Hello {{ first_name }}',
    )
    expect(within(dialog).getByText(/2 enrollments get the new version, including 1/)).toBeVisible()
    expect(within(dialog).getByText('Grace Hopper')).toBeVisible()
    expect(
      within(dialog).getByText(
        /4 existing messages keep the text .* including 1 still in progress/,
      ),
    ).toBeVisible()
    expect(within(dialog).getByText('Hello Ada')).toBeVisible()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Use this version' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(await screen.findByText(/Adopted from v1/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Use v2 for step 1' })).toBeNull()
    const adopted = calls.filter((c) => c.method === 'POST')
    expect(adopted.map((c) => [c.path, c.body])).toEqual([
      ['/api/v1/campaigns/5/steps/101/adopt', { fingerprint: 'adopt-fp', confirm: true }],
    ])
  })

  it('says why a version is refused and never posts', async () => {
    const calls: Call[] = []
    const state = {
      campaign: withNewer('active'),
      review: review({ status: 'active', missing: [] }),
    }
    mockFetch(
      campaignBackend(
        state,
        {
          'GET /api/v1/campaigns/5/steps/101/adoption': () =>
            jsonResponse(
              adoption({ refusal: 'v2 has lint errors (removed_field); fix the template first' }),
            ),
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    fireEvent.click(await screen.findByRole('button', { name: 'Use v2 for step 1' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Use v2 for step 1?' })
    expect(await within(dialog).findByText(/fix the template first/)).toBeVisible()
    const confirm = within(dialog).getByRole('button', { name: 'Use this version' })
    expect(confirm).toBeDisabled()
    fireEvent.click(confirm)
    expect(calls.filter((c) => c.method === 'POST')).toEqual([])
  })

  it('shows the preview again when the adoption is refused as stale', async () => {
    const calls: Call[] = []
    let shown = 0
    const state = {
      campaign: withNewer('active'),
      review: review({ status: 'active', missing: [] }),
    }
    mockFetch(
      campaignBackend(
        state,
        {
          'GET /api/v1/campaigns/5/steps/101/adoption': () => {
            shown += 1
            return jsonResponse(
              shown === 1
                ? adoption()
                : adoption({ newest: { ...adoption().newest, version: 3 }, fingerprint: 'fp-3' }),
            )
          },
          'POST /api/v1/campaigns/5/steps/101/adopt': () =>
            jsonResponse({ detail: 'step 1 or its template changed since it was shown' }, 409),
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    fireEvent.click(await screen.findByRole('button', { name: 'Use v2 for step 1' }))
    let dialog = await screen.findByRole('alertdialog', { name: 'Use v2 for step 1?' })
    await within(dialog).findByLabelText('Changes')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Use this version' }))
    dialog = await screen.findByRole('alertdialog', { name: 'Use v3 for step 1?' })
    expect(within(dialog).getByText(/changed since it was shown/)).toBeVisible()
    expect(shown).toBe(2)
  })

  it('offers nothing on a completed campaign', async () => {
    const state = {
      campaign: withNewer('completed'),
      review: review({ status: 'completed', missing: [] }),
    }
    mockFetch(campaignBackend(state))
    await renderApp('/campaigns/5')
    expect(await screen.findByText('Catching up')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Use v2 for step 1' })).toBeNull()
  })
})
