import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import type { Campaign, Review } from './api'
import {
  ALL_MISSING,
  campaign,
  campaignBackend,
  guardDetails,
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
      'Each step approved: missing, steps not approved: steps 1, 2',
      'Personal-line messages approved one by one: done',
      'Test send of each email step: missing, email steps with no current test send: steps 1, 2',
      'Mailbox ok: done',
      'Lint clean: missing, no lint result for the current steps and templates',
    ])
    expect(section('Activate').getByText(/3 requirements are still missing/)).toBeVisible()
    expect(section('Activate').getByRole('button', { name: 'Activate' })).toBeDisabled()
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

  it('says a test goes only to your own mailbox, and shows why one was refused', async () => {
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
                      'me@sender.example is not armed; arm it for drafts (`gmail arm`) to make the test a draft in your Drafts, or to send (`gmail arm --send`) to send it to you',
                  },
                  409,
                )
              : jsonResponse({
                  step_id: 102,
                  to_address: 'me@sender.example',
                  sent_at: '2030-06-15T12:00:00Z',
                  drafted: false,
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
    expect(sends.getByText(/Armed for drafts, the test is a draft in your Drafts/)).toBeVisible()
    expect(sends.queryByText(/must be armed for send/)).toBeNull()
    expect(sends.getByText(/reply from a different Gmail account/)).toBeVisible()

    fireEvent.click(sends.getByRole('button', { name: 'Send a test of step 1' }))
    const refusal = await sends.findByRole('alert')
    expect(refusal).toHaveTextContent("Step 1's test was not sent.")
    expect(refusal).toHaveTextContent('is not armed')

    fireEvent.click(sends.getByRole('button', { name: 'Send a test of step 2' }))
    expect(await sends.findByText(/Sent to me@sender.example at/)).toBeVisible()
    expect(calls.filter((c) => c.path.endsWith('/test-send')).map((c) => c.body)).toEqual([
      { step_id: 101 },
      { step_id: 102 },
    ])
  })

  it('says a test on a mailbox armed for drafts is a draft in your Drafts', async () => {
    mockFetch(
      campaignBackend(reviewing(), {
        'POST /api/v1/campaigns/5/review/test-send': () =>
          jsonResponse({
            step_id: 101,
            to_address: 'me@sender.example',
            sent_at: '2030-06-15T12:00:00Z',
            drafted: true,
          }),
      }),
    )
    await renderApp('/campaigns/5')

    await screen.findByRole('list', { name: 'Review checklist' })
    const sends = section('Test sends')
    fireEvent.click(sends.getByRole('button', { name: 'Send a test of step 1' }))
    expect(
      await sends.findByText(/Test draft to me@sender.example created in your Drafts at/),
    ).toBeVisible()
    expect(sends.queryByText(/Sent to/)).toBeNull()
  })

  it('shows the guard summary as information, with nothing to acknowledge', async () => {
    const calls: Call[] = []
    mockFetch(campaignBackend(reviewing({ missing: [] }), {}, calls))
    await renderApp('/campaigns/5')

    const guards = within(await screen.findByRole('region', { name: 'Guard summary' }))
    expect(guards.getByText('2 will start, 1 skipped (1 do-not-contact)')).toBeVisible()
    expect(guards.getByText(/activation doesn.t wait on it/)).toBeVisible()
    expect(guards.queryByRole('button', { name: /acknowledge/i })).toBeNull()
    // Every requirement met, and the guard summary is not among them (#346).
    expect(await screen.findByText('Every requirement is met.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Activate' })).toBeEnabled()
    expect(calls.some((c) => c.path.endsWith('/review/guards'))).toBe(false)
  })

  it('shows each skipped contact with every reason on demand', async () => {
    const calls: Call[] = []
    mockFetch(campaignBackend(reviewing(), {}, calls))
    await renderApp('/campaigns/5')

    const guards = within(await screen.findByRole('region', { name: 'Guard summary' }))
    const show = guards.getByRole('button', { name: 'Show skipped contacts' })
    expect(show).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(show)

    const skipped = within(await guards.findByRole('list', { name: 'Skipped contacts' }))
    expect(skipped.getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Tobias Wrenfield: do-not-contact, no email',
    ])
    expect(calls.filter((c) => c.path.endsWith('/review/guards'))).toHaveLength(1)
    expect(guards.queryByText(/Showing the first/)).toBeNull()

    fireEvent.click(guards.getByRole('button', { name: 'Hide skipped contacts' }))
    expect(guards.queryByRole('list', { name: 'Skipped contacts' })).toBeNull()
  })

  it('notes who the old tool already emailed', async () => {
    mockFetch(
      campaignBackend(
        reviewing({ prior_contact_note: '12 were emailed by the old tool; last on 2026-05-01' }),
      ),
    )
    await renderApp('/campaigns/5')

    const guards = within(await screen.findByRole('region', { name: 'Guard summary' }))
    expect(guards.getByText('12 were emailed by the old tool; last on 2026-05-01.')).toBeVisible()
  })

  it('says when the list of skipped contacts is cut short', async () => {
    mockFetch(
      campaignBackend(reviewing(), {
        'GET /api/v1/campaigns/5/review/guards': () =>
          jsonResponse(guardDetails({ skipped_total: 612 })),
      }),
    )
    await renderApp('/campaigns/5')

    const guards = within(await screen.findByRole('region', { name: 'Guard summary' }))
    fireEvent.click(guards.getByRole('button', { name: 'Show skipped contacts' }))
    expect(await guards.findByText('Showing the first 1 of 612 skipped contacts.')).toBeVisible()
  })

  it('says so when the guards skip nobody', async () => {
    mockFetch(
      campaignBackend(reviewing({ guard_summary: '3 will start, none skipped' }), {
        'GET /api/v1/campaigns/5/review/guards': () =>
          jsonResponse({
            summary: '3 will start, none skipped',
            will_start: 3,
            not_enrolled: 0,
            skipped: [],
            skipped_total: 0,
            prior_contact_note: null,
          }),
      }),
    )
    await renderApp('/campaigns/5')

    const guards = within(await screen.findByRole('region', { name: 'Guard summary' }))
    expect(guards.queryByText(/old tool/)).toBeNull()
    fireEvent.click(guards.getByRole('button', { name: 'Show skipped contacts' }))
    expect(await guards.findByText('Nobody is skipped.')).toBeVisible()
  })

  it('keeps Activate disabled while anything is missing, and lists what', async () => {
    mockFetch(campaignBackend(reviewing()))
    await renderApp('/campaigns/5')

    const activate = within(await screen.findByRole('region', { name: 'Activate' }))
    expect(activate.getByRole('button', { name: 'Activate' })).toBeDisabled()
    const listed = within(activate.getByRole('list', { name: 'Missing before activation' }))
    expect(listed.getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Each step approved: steps not approved: steps 1, 2',
      'Test send of each email step: email steps with no current test send: steps 1, 2',
      'Lint clean: no lint result for the current steps and templates',
    ])
  })

  it('shows what is missing when activation is refused', async () => {
    const calls: Call[] = []
    mockFetch(
      campaignBackend(
        // The page last saw nothing missing; the gate, checking again, disagrees.
        reviewing({ missing: [] }),
        {
          'POST /api/v1/campaigns/5/activate': () =>
            jsonResponse(
              {
                detail: 'the review is not complete: test_sends, lint',
                missing: [ALL_MISSING[1], ALL_MISSING[2]],
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
      'the review is not complete: test_sends, lint',
    )
    const still = within(dialog.getByRole('list', { name: 'Still missing' }))
    expect(still.getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Test send of each email step: email steps with no current test send: steps 1, 2',
      'Lint clean: no lint result for the current steps and templates',
    ])

    // The same refusal again: the confirm button is not left dead (#181).
    fireEvent.click(dialog.getByRole('button', { name: 'Activate campaign' }))
    await waitFor(() => expect(calls.filter((c) => c.path.endsWith('/activate'))).toHaveLength(2))
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
