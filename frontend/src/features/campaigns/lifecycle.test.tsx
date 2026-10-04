/**
 * The campaign lifecycle (#345): end a running campaign, archive and unarchive a
 * concluded one, delete one never activated, and the archived campaigns behind a
 * button on the list.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import type { Campaign, DeletePlan } from './api'
import { campaign, campaignBackend, review, summary, type Call } from './test-support'

function posts(calls: Call[]): string[] {
  return calls.filter((c) => c.method !== 'GET').map((c) => `${c.method} ${c.path}`)
}

function plan(overrides: Partial<DeletePlan> = {}): DeletePlan {
  return {
    campaign_id: 5,
    name: 'Autumn reconnect',
    deletable: true,
    refusal: null,
    steps: 2,
    enrollments: 3,
    leftover_drafts: [],
    ...overrides,
  }
}

describe('campaign lifecycle', () => {
  it('ends a running campaign after a confirm, then archives and unarchives it', async () => {
    const calls: Call[] = []
    const state = {
      campaign: campaign({ status: 'active', enrollments: { active: 2 } }),
      review: review({ status: 'active', missing: [] }),
    }
    const move = (changes: Partial<Campaign>) => () => {
      state.campaign = { ...state.campaign, ...changes }
      return jsonResponse(state.campaign)
    }
    mockFetch(
      campaignBackend(
        state,
        {
          'POST /api/v1/campaigns/5/end': move({ status: 'completed', concluded: true }),
          'POST /api/v1/campaigns/5/archive': move({ status: 'archived' }),
          'POST /api/v1/campaigns/5/unarchive': move({ status: 'completed' }),
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    expect(screen.queryByRole('button', { name: 'Archive' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Delete campaign' })).toBeNull()

    // Cancel changes nothing.
    fireEvent.click(await screen.findByRole('button', { name: 'End campaign' }))
    let dialog = await screen.findByRole('alertdialog', { name: 'End Autumn reconnect?' })
    expect(within(dialog).getByText(/you cannot resume it/)).toBeVisible()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(posts(calls)).toEqual([])

    fireEvent.click(screen.getByRole('button', { name: 'End campaign' }))
    dialog = await screen.findByRole('alertdialog', { name: 'End Autumn reconnect?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'End campaign' }))
    expect(await screen.findByRole('button', { name: 'Archive' })).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Autumn reconnect Completed' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'End campaign' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Archive' }))
    expect(await screen.findByRole('button', { name: 'Unarchive' })).toBeVisible()
    expect(screen.getByText(/Hidden from the campaign list and the dashboard/)).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: 'Unarchive' }))
    expect(await screen.findByRole('button', { name: 'Archive' })).toBeVisible()
    expect(posts(calls)).toEqual([
      'POST /api/v1/campaigns/5/end',
      'POST /api/v1/campaigns/5/archive',
      'POST /api/v1/campaigns/5/unarchive',
    ])
  })

  it('shows a refused end in the dialog', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign({ status: 'paused' }), review: review() },
        {
          'POST /api/v1/campaigns/5/end': () =>
            jsonResponse({ detail: 'campaign 5 is completed; only an active or paused' }, 409),
        },
      ),
    )
    await renderApp('/campaigns/5')

    fireEvent.click(await screen.findByRole('button', { name: 'End campaign' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'End campaign' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('only an active or paused')
  })

  it('deletes a draft after a confirm that lists the Gmail drafts left behind', async () => {
    const calls: Call[] = []
    mockFetch(
      campaignBackend(
        { campaign: campaign({ deletable: true }), review: review() },
        {
          'GET /api/v1/campaigns/5/delete-plan': () =>
            jsonResponse(
              plan({
                leftover_drafts: [
                  {
                    step_position: 2,
                    to_address: 'me@sender.example',
                    drafted_at: '2030-06-16T12:00:00Z',
                    gmail_draft_id: 'draft-1',
                  },
                ],
              }),
            ),
          'DELETE /api/v1/campaigns/5': () => jsonResponse(plan()),
          'GET /api/v1/campaigns': () => jsonResponse([]),
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    fireEvent.click(await screen.findByRole('button', { name: 'Delete campaign' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Delete Autumn reconnect?' })
    expect(await within(dialog).findByText(/its 2 steps and its 3 enrollments/)).toBeVisible()
    const drafts = within(dialog).getByRole('list', { name: 'Gmail drafts left behind' })
    expect(drafts).toHaveTextContent('Step 2 test to me@sender.example')
    expect(within(dialog).getByText(/never deletes a Gmail draft/)).toBeVisible()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete campaign' }))
    expect(await screen.findByText('No campaigns yet')).toBeVisible()
    expect(posts(calls)).toEqual(['DELETE /api/v1/campaigns/5'])
  })

  it('does not delete when the check refuses, and says why', async () => {
    const calls: Call[] = []
    mockFetch(
      campaignBackend(
        { campaign: campaign({ deletable: true }), review: review() },
        {
          'GET /api/v1/campaigns/5/delete-plan': () =>
            jsonResponse(
              plan({ deletable: false, refusal: 'campaign 5 has 1 message, so it is kept' }),
            ),
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    fireEvent.click(await screen.findByRole('button', { name: 'Delete campaign' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('has 1 message')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete campaign' }))
    expect(posts(calls)).toEqual([])
  })
})

describe('archived campaigns on the list', () => {
  it('stay hidden until asked for', async () => {
    const calls: Call[] = []
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review() },
        {
          'GET /api/v1/campaigns': (call) =>
            jsonResponse(
              call.query.get('archived') === 'true'
                ? [summary({ id: 9, name: 'Last spring', status: 'archived' })]
                : [summary({ id: 5, name: 'Autumn reconnect' })],
            ),
        },
        calls,
      ),
    )
    await renderApp('/campaigns')

    await screen.findByRole('link', { name: 'Autumn reconnect' })
    expect(screen.queryByRole('link', { name: 'Last spring' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Show archived campaigns' }))
    const link = await screen.findByRole('link', { name: 'Last spring' })
    expect(link).toHaveAttribute('href', '/campaigns/9')
    expect(within(link.closest('tr') as HTMLElement).getByText('Archived')).toBeVisible()
    expect(calls.some((c) => c.query.get('archived') === 'true')).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Hide archived campaigns' }))
    await waitFor(() => expect(screen.queryByRole('link', { name: 'Last spring' })).toBeNull())
  })
})
