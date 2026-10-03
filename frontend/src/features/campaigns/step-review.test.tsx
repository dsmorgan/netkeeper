import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import type { Campaign, Review, StepReview } from './api'
import {
  ALL_MISSING,
  MESSAGES,
  campaign,
  campaignBackend,
  message,
  review,
  stepReview,
  type Call,
} from './test-support'

/** One approval per step, paging through its messages (#339). */

function reviewing(overrides: Partial<Review> = {}) {
  return {
    campaign: campaign({ status: 'reviewing', enrollments: { pending: 3 } }) as Campaign,
    review: review(overrides),
  }
}

async function stepOne() {
  return within(await screen.findByRole('region', { name: 'Step 1' }))
}

function shownMessage(step: ReturnType<typeof within>) {
  return step.getByRole('article')
}

describe('step review', () => {
  it('pages through every message with the buttons and the arrow keys', async () => {
    mockFetch(campaignBackend(reviewing()))
    await renderApp('/campaigns/5')

    const step = await stepOne()
    expect(await step.findByText('1 of 3')).toBeVisible()
    expect(shownMessage(step)).toHaveTextContent('Hi Rosalind')
    expect(shownMessage(step)).toHaveTextContent('rosalind@nimbus-kettle.example')
    expect(step.getByRole('button', { name: 'Previous message' })).toBeDisabled()

    fireEvent.click(step.getByRole('button', { name: 'Next message' }))
    expect(step.getByText('2 of 3')).toBeVisible()
    expect(shownMessage(step)).toHaveTextContent('Hi Tobias')

    const pager = step.getByRole('group', { name: 'Messages of step 1' })
    fireEvent.keyDown(pager, { key: 'ArrowRight' })
    expect(step.getByText('3 of 3')).toBeVisible()
    expect(shownMessage(step)).toHaveTextContent('Hi Wilhelmina')
    expect(step.getByRole('button', { name: 'Next message' })).toBeDisabled()
    fireEvent.keyDown(pager, { key: 'ArrowRight' }) // stays on the last
    expect(step.getByText('3 of 3')).toBeVisible()

    fireEvent.keyDown(pager, { key: 'ArrowLeft' })
    fireEvent.keyDown(pager, { key: 'ArrowLeft' })
    expect(step.getByText('1 of 3')).toBeVisible()
    expect(shownMessage(step)).toHaveTextContent('Hi Rosalind')
  })

  it('fetches the next page when paging past the first', async () => {
    const calls: Call[] = []
    const many = Array.from({ length: 21 }, (_, n) =>
      message({ enrollment_id: 500 + n, contact_name: `Person ${n + 1}`, body: `Hi ${n + 1}` }),
    )
    mockFetch(
      campaignBackend(
        reviewing(),
        {
          'GET /api/v1/campaigns/5/review/steps/101': (call) => {
            const offset = Number(call.query.get('offset'))
            return jsonResponse(
              stepReview({ total: 21, offset, messages: many.slice(offset, offset + 20) }),
            )
          },
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    const step = await stepOne()
    expect(await step.findByText('1 of 21')).toBeVisible()
    const pager = step.getByRole('group', { name: 'Messages of step 1' })
    for (let n = 0; n < 20; n += 1) fireEvent.keyDown(pager, { key: 'ArrowRight' })

    expect(await step.findByText('Hi 21')).toBeVisible()
    expect(step.getByText('21 of 21')).toBeVisible()
    const offsets = calls
      .filter((c) => c.path === '/api/v1/campaigns/5/review/steps/101')
      .map((c) => [c.query.get('offset'), c.query.get('limit')])
    expect(offsets).toContainEqual(['0', '20'])
    expect(offsets).toContainEqual(['20', '20'])
  })

  it('approves the step once, for the fingerprint it was shown with', async () => {
    const calls: Call[] = []
    const state = reviewing()
    let approved: StepReview = stepReview()
    mockFetch(
      campaignBackend(
        state,
        {
          'GET /api/v1/campaigns/5/review/steps/101': () => jsonResponse(approved),
          'POST /api/v1/campaigns/5/review/steps/101/approve': () => {
            approved = stepReview({
              approved: true,
              messages: MESSAGES.map((m) => ({ ...m, approved: true })),
            })
            state.review = {
              ...state.review,
              missing: [
                {
                  requirement: 'step_approvals',
                  detail: 'steps not approved',
                  enrollment_ids: [],
                  step_positions: [2],
                },
                ...ALL_MISSING.slice(1),
              ],
            }
            return jsonResponse(state.review)
          },
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    const step = await stepOne()
    expect(await step.findByText('Not approved')).toBeVisible()
    fireEvent.click(step.getByRole('button', { name: 'Approve step 1' }))

    expect(await step.findByText('Approved')).toBeVisible()
    expect(step.getByText(/Approved for these 3 messages and any rendered later/)).toBeVisible()
    expect(step.queryByRole('button', { name: 'Approve step 1' })).toBeNull()
    const posted = calls.filter((c) => c.path.endsWith('/review/steps/101/approve'))
    expect(posted.map((c) => c.body)).toEqual([{ fingerprint: 'step-fp-101' }])
    const checklist = within(screen.getByRole('list', { name: 'Review checklist' }))
    await waitFor(() =>
      expect(checklist.getAllByRole('listitem')[2]).toHaveTextContent(
        'Each step approved: missing, steps not approved: steps 2',
      ),
    )
  })

  it('lists blocked messages apart from the pager', async () => {
    mockFetch(
      campaignBackend(reviewing(), {
        'GET /api/v1/campaigns/5/review/steps/101': () =>
          jsonResponse(
            stepReview({
              total: 2,
              messages: MESSAGES.slice(0, 2),
              blocked: [
                message({
                  enrollment_id: 309,
                  contact_name: 'Ottoline Brack',
                  subject: null,
                  body: null,
                  blocked: 'excluded by a guard: do-not-contact',
                }),
              ],
            }),
          ),
      }),
    )
    await renderApp('/campaigns/5')

    const step = await stepOne()
    expect(await step.findByText('1 of 2')).toBeVisible()
    expect(step.getByText("1 message can't be sent and stays blocked")).toBeVisible()
    const blocked = within(step.getByRole('list', { name: 'Blocked messages of step 1' }))
    expect(blocked.getByRole('listitem')).toHaveTextContent(
      'Ottoline Brack: excluded by a guard: do-not-contact',
    )
    expect(step.getByRole('button', { name: 'Approve step 1' })).toBeEnabled()
  })

  it('asks for each message of a personal_line step on its own', async () => {
    const calls: Call[] = []
    let current = stepReview({ per_message: true, unapproved: 3 })
    mockFetch(
      campaignBackend(
        reviewing(),
        {
          'GET /api/v1/campaigns/5/review/steps/101': () => jsonResponse(current),
          'POST /api/v1/campaigns/5/review/steps/101/messages/approve': () => {
            current = {
              ...current,
              unapproved: 2,
              messages: current.messages.map((m, i) => (i === 0 ? { ...m, approved: true } : m)),
            }
            return jsonResponse(review())
          },
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    const step = await stepOne()
    expect(await step.findByText(/uses \{\{ personal_line \}\}/)).toBeVisible()
    expect(step.getByText('3 messages to approve')).toBeVisible()
    expect(step.queryByRole('button', { name: 'Approve step 1' })).toBeNull()

    fireEvent.click(step.getByRole('button', { name: 'Approve this message' }))

    expect(await step.findByText('2 messages to approve')).toBeVisible()
    expect(within(shownMessage(step)).getByText('Approved')).toBeVisible()
    const posted = calls.filter((c) => c.path.endsWith('/review/steps/101/messages/approve'))
    expect(posted.map((c) => c.body)).toEqual([
      { messages: [{ enrollment_id: 301, fingerprint: 'fp-rosalind-000000' }] },
    ])
  })

  it('refreshes a step whose fingerprint went stale, then approves the new one', async () => {
    const calls: Call[] = []
    let fingerprint = 'step-fp-old'
    mockFetch(
      campaignBackend(
        reviewing(),
        {
          'GET /api/v1/campaigns/5/review/steps/101': () =>
            jsonResponse(stepReview({ fingerprint })),
          'POST /api/v1/campaigns/5/review/steps/101/approve': (call) => {
            if ((call.body as { fingerprint: string }).fingerprint === 'step-fp-old') {
              fingerprint = 'step-fp-new' // the template was edited meanwhile
              return jsonResponse(
                { detail: 'step 1 changed since it was shown', code: 'stale' },
                409,
              )
            }
            return jsonResponse(review())
          },
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    const step = await stepOne()
    fireEvent.click(await step.findByRole('button', { name: 'Approve step 1' }))
    expect(await step.findByRole('status')).toHaveTextContent(
      'Something changed since this step was shown',
    )
    expect(step.queryByRole('alert')).toBeNull()

    await waitFor(() =>
      expect(
        calls.filter((c) => c.method === 'GET' && c.path.endsWith('/review/steps/101')).length,
      ).toBeGreaterThan(1),
    )
    fireEvent.click(step.getByRole('button', { name: 'Approve step 1' }))
    await waitFor(() =>
      expect(
        calls.filter((c) => c.path.endsWith('/review/steps/101/approve')).map((c) => c.body),
      ).toEqual([{ fingerprint: 'step-fp-old' }, { fingerprint: 'step-fp-new' }]),
    )
  })

  it('shows a real refusal of an approval as an error', async () => {
    mockFetch(
      campaignBackend(reviewing(), {
        'POST /api/v1/campaigns/5/review/steps/101/approve': () =>
          jsonResponse({ detail: 'campaign 5 is draft, not reviewing' }, 409),
      }),
    )
    await renderApp('/campaigns/5')

    const step = await stepOne()
    fireEvent.click(await step.findByRole('button', { name: 'Approve step 1' }))

    expect(await step.findByRole('alert')).toHaveTextContent('campaign 5 is draft, not reviewing')
    expect(step.queryByRole('status')).toBeNull()
  })
})
