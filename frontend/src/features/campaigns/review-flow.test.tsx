import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import type { Campaign, Review } from './api'
import {
  ALL_MISSING,
  ENROLLMENTS,
  campaign,
  campaignBackend,
  preview,
  review,
  type Call,
} from './test-support'

function reviewing(overrides: Partial<Review> = {}) {
  return {
    campaign: campaign({ status: 'reviewing', enrollments: { pending: 2 } }) as Campaign,
    review: review(overrides),
  }
}

function section(name: string) {
  return within(screen.getByRole('region', { name }))
}

describe('review flow', () => {
  it('lists every requirement, with what the gate says is missing', async () => {
    mockFetch(campaignBackend(reviewing()))
    await renderApp('/campaigns/5')

    const checklist = within(await screen.findByRole('list', { name: 'Review checklist' }))
    const items = checklist.getAllByRole('listitem').map((li) => li.textContent)
    expect(items).toEqual([
      'Review started: done',
      'Audience enrolled: done',
      'Sampled previews approved: missing, no sample was drawn for the current audience',
      'Searched previews approved: done',
      'Test send of each email step: missing, email steps with no current test send: steps 1, 2',
      'Lint clean: missing, no lint result for the current steps and templates',
      'Guard summary acknowledged: missing, the guard summary for the current audience is not acknowledged',
    ])
    expect(section('Activate').getByText(/4 requirements are still missing/)).toBeVisible()
  })

  it('starts the review of a draft, and shows why it will not', async () => {
    const calls: Call[] = []
    mockFetch(
      campaignBackend(
        {
          campaign: campaign(),
          review: review({
            status: 'draft',
            missing: [
              {
                requirement: 'reviewing',
                detail: 'the campaign is draft, not reviewing',
                enrollment_ids: [],
                step_positions: [],
              },
              {
                requirement: 'audience',
                detail: 'nobody is enrolled',
                enrollment_ids: [],
                step_positions: [],
              },
            ],
          }),
        },
        {
          'POST /api/v1/campaigns/5/review/start': () =>
            jsonResponse({ detail: 'campaign 5 has no audience: nobody is enrolled' }, 409),
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    const checklist = within(await screen.findByRole('list', { name: 'Review checklist' }))
    expect(checklist.getByText(/nobody is enrolled/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Start review' }))

    expect(await screen.findByText(/campaign 5 has no audience/)).toBeVisible()
    expect(screen.queryByRole('region', { name: 'Test sends' })).toBeNull()
  })

  it('approves sampled previews for the fingerprint each was shown with', async () => {
    const calls: Call[] = []
    const state = reviewing()
    mockFetch(
      campaignBackend(
        state,
        {
          'POST /api/v1/campaigns/5/review/sample': () =>
            jsonResponse({
              content_fingerprint: 'c0ffee',
              enrollments: [
                preview(),
                preview({
                  enrollment_id: 302,
                  contact_name: 'Tobias Marrowbone',
                  fingerprint: 'fp-tobias-111111',
                }),
              ],
            }),
          'POST /api/v1/campaigns/5/review/approve': () => {
            state.review = { ...state.review, missing: ALL_MISSING.slice(1) }
            return jsonResponse(state.review)
          },
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    await screen.findByRole('list', { name: 'Review checklist' })
    fireEvent.click(section('Sampled previews').getByRole('button', { name: 'Show the sample' }))

    const card = within(
      await screen.findByRole('listitem', { name: 'Preview for Rosalind Quillfeather' }),
    )
    expect(card.getByText('Hi Rosalind')).toBeVisible()
    expect(card.getByText(/to rosalind@nimbus-kettle.example/)).toBeVisible()
    expect(card.getByText('fingerprint fp-rosalind-')).toBeVisible()

    fireEvent.click(
      section('Sampled previews').getByRole('button', { name: 'Approve all 2 shown' }),
    )

    await waitFor(() => expect(card.getByText('Approved')).toBeVisible())
    const approve = calls.find((c) => c.path === '/api/v1/campaigns/5/review/approve')
    expect(approve?.body).toEqual({
      previews: [
        { enrollment_id: 301, fingerprint: 'fp-rosalind-000000' },
        { enrollment_id: 302, fingerprint: 'fp-tobias-111111' },
      ],
    })
    const checklist = within(screen.getByRole('list', { name: 'Review checklist' }))
    await waitFor(() =>
      expect(checklist.getAllByRole('listitem')[2]).toHaveTextContent(
        'Sampled previews approved: done',
      ),
    )
  })

  it('finds any enrollment, previews it, and approves it', async () => {
    const calls: Call[] = []
    mockFetch(
      campaignBackend(
        reviewing(),
        {
          'GET /api/v1/campaigns/5/enrollments': (call) =>
            jsonResponse(
              call.query.get('q') === 'tobias'
                ? { total: 1, items: [{ ...ENROLLMENTS.items[1], status: 'pending' }] }
                : ENROLLMENTS,
            ),
          'POST /api/v1/campaigns/5/review/previews': () =>
            jsonResponse({
              content_fingerprint: 'c0ffee',
              enrollments: [
                preview({
                  enrollment_id: 302,
                  contact_name: 'Tobias Marrowbone',
                  sampled: false,
                  fingerprint: 'fp-tobias-111111',
                }),
              ],
            }),
          'POST /api/v1/campaigns/5/review/approve': () => jsonResponse(review()),
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    fireEvent.change(await screen.findByLabelText('Search enrollments by name or address'), {
      target: { value: 'tobias' },
    })
    const search = section('Search for anyone')
    fireEvent.click(search.getByRole('button', { name: 'Search' }))
    const results = within(await screen.findByRole('list', { name: 'Search results' }))
    fireEvent.click(results.getByRole('button', { name: 'Preview' }))

    const card = within(
      await screen.findByRole('listitem', { name: 'Preview for Tobias Marrowbone' }),
    )
    fireEvent.click(card.getByRole('button', { name: 'Approve' }))

    await waitFor(() => expect(card.getByText('Approved')).toBeVisible())
    const lookup = calls.find(
      (c) => c.path === '/api/v1/campaigns/5/enrollments' && c.query.get('q') === 'tobias',
    )
    expect(lookup?.query.get('status')).toBe('pending')
    expect(calls.find((c) => c.path === '/api/v1/campaigns/5/review/previews')?.body).toEqual({
      enrollment_ids: [302],
    })
    expect(calls.find((c) => c.path === '/api/v1/campaigns/5/review/approve')?.body).toEqual({
      previews: [{ enrollment_id: 302, fingerprint: 'fp-tobias-111111' }],
    })
  })

  it('shows lint errors per step', async () => {
    mockFetch(
      campaignBackend(reviewing(), {
        'POST /api/v1/campaigns/5/review/lint': () =>
          jsonResponse({
            clean: false,
            steps: [
              {
                position: 2,
                errors: [
                  {
                    rule: 'undefined_variable',
                    severity: 'error',
                    part: 'body',
                    field: 'nickname',
                    message: '`nickname` is not a merge field',
                  },
                ],
              },
            ],
          }),
      }),
    )
    await renderApp('/campaigns/5')

    fireEvent.click(await screen.findByRole('button', { name: 'Run lint' }))

    expect(await section('Lint').findByRole('alert')).toHaveTextContent(
      'Step 2, body: `nickname` is not a merge field',
    )
  })

  it('says a test send goes only to your own mailbox, and shows why one was refused', async () => {
    const calls: Call[] = []
    mockFetch(
      campaignBackend(
        reviewing(),
        {
          'POST /api/v1/campaigns/5/review/test-send': (call) =>
            (call.body as { step_id: number }).step_id === 101
              ? jsonResponse(
                  {
                    detail:
                      'me@sender.example is not armed for send; a test send needs `gmail arm --send`',
                  },
                  409,
                )
              : jsonResponse({
                  step_id: 102,
                  to_address: 'me@sender.example',
                  sent_at: '2030-06-15T12:00:00Z',
                }),
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    await screen.findByRole('list', { name: 'Review checklist' })
    const sends = section('Test sends')
    expect(sends.getByText(/your own address, me@sender.example/)).toBeVisible()
    expect(sends.getByText(/never goes to a contact/)).toBeVisible()
    expect(sends.getByText(/armed for send/)).toBeVisible()

    fireEvent.click(sends.getByRole('button', { name: 'Send a test of step 1' }))
    const refusal = await sends.findByRole('alert')
    expect(refusal).toHaveTextContent("Step 1's test was not sent.")
    expect(refusal).toHaveTextContent('is not armed for send')

    fireEvent.click(sends.getByRole('button', { name: 'Send a test of step 2' }))
    expect(await sends.findByText(/Sent to me@sender.example at/)).toBeVisible()
    expect(calls.filter((c) => c.path.endsWith('/test-send')).map((c) => c.body)).toEqual([
      { step_id: 101 },
      { step_id: 102 },
    ])
  })

  it('acknowledges the guard summary exactly as shown', async () => {
    const calls: Call[] = []
    const state = reviewing()
    mockFetch(
      campaignBackend(
        state,
        {
          'POST /api/v1/campaigns/5/review/guards/acknowledge': () => {
            state.review = {
              ...state.review,
              guards_acknowledged: '2030-06-15T12:00:00Z',
              missing: ALL_MISSING.filter((m) => m.requirement !== 'guards'),
            }
            return jsonResponse(state.review)
          },
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    const guards = await screen.findByRole('region', { name: 'Guard summary' })
    expect(within(guards).getByText('3 in audience, 1 excluded: 1 do-not-contact')).toBeVisible()
    fireEvent.click(within(guards).getByRole('button', { name: 'Acknowledge this summary' }))

    expect(await within(guards).findByText('Acknowledged.')).toBeVisible()
    expect(calls.find((c) => c.path.endsWith('/guards/acknowledge'))?.body).toEqual({
      summary: '3 in audience, 1 excluded: 1 do-not-contact',
      audience_fingerprint: 'aud1ence',
    })
  })

  it('shows what is missing when activation is refused', async () => {
    const calls: Call[] = []
    mockFetch(
      campaignBackend(
        reviewing(),
        {
          'POST /api/v1/campaigns/5/activate': () =>
            jsonResponse(
              {
                detail: 'the review is not complete: test_sends, guards',
                missing: [ALL_MISSING[1], ALL_MISSING[3]],
              },
              409,
            ),
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    fireEvent.click(await screen.findByRole('button', { name: 'Activate' }))
    const dialog = within(await screen.findByRole('alertdialog'))
    expect(dialog.getByText(/2 pending enrollments become active/)).toBeVisible()
    expect(calls.some((c) => c.path.endsWith('/activate'))).toBe(false)

    fireEvent.click(dialog.getByRole('button', { name: 'Activate campaign' }))

    expect(await dialog.findByRole('alert')).toHaveTextContent(
      'the review is not complete: test_sends, guards',
    )
    const still = within(dialog.getByRole('list', { name: 'Still missing' }))
    expect(still.getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Test send of each email step: email steps with no current test send: steps 1, 2',
      'Guard summary acknowledged: the guard summary for the current audience is not acknowledged',
    ])
  })

  it('activates once confirmed', async () => {
    const calls: Call[] = []
    const state = reviewing({ missing: [] })
    mockFetch(
      campaignBackend(
        state,
        {
          'POST /api/v1/campaigns/5/activate': () => {
            state.campaign = {
              ...state.campaign,
              status: 'active',
              enrollments: { active: 2 },
              approved_at: '2030-06-15T12:00:00Z',
            }
            state.review = { ...state.review, status: 'active' }
            return jsonResponse(state.review)
          },
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    expect(await screen.findByText('Every requirement is met.')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Activate' }))
    const dialog = within(await screen.findByRole('alertdialog'))
    fireEvent.click(dialog.getByRole('button', { name: 'Activate campaign' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(await screen.findByRole('button', { name: 'Pause' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'Review' })).toBeNull()
    expect(calls.filter((c) => c.path.endsWith('/activate'))).toHaveLength(1)
  })
})
