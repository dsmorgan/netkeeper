import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import { mailbox } from '@/features/mailboxes/test-support'

import {
  ENROLLMENTS,
  MAILBOX,
  STEPS,
  campaign,
  campaignBackend,
  review,
  summary,
  type Call,
} from './test-support'

function rowOf(name: string): HTMLElement {
  const row = screen.getByRole('link', { name }).closest('tr')
  expect(row).not.toBeNull()
  return row as HTMLElement
}

describe('campaign list', () => {
  it('says so when there are no campaigns', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review() },
        { 'GET /api/v1/campaigns': () => jsonResponse([]) },
      ),
    )
    await renderApp('/campaigns')

    expect(await screen.findByText('No campaigns yet')).toBeVisible()
    expect(screen.getByRole('link', { name: 'New campaign' })).toHaveAttribute(
      'href',
      '/campaigns/new',
    )
  })

  it('shows each campaign with its status, counts and next send', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review() },
        {
          'GET /api/v1/campaigns': () =>
            jsonResponse([
              summary({
                id: 7,
                name: 'Spring hello',
                status: 'active',
                enrollments: { active: 4, replied: 1 },
                next_action_at: '2030-06-20T12:00:00Z',
              }),
              summary({ id: 5, name: 'Autumn reconnect', enrollments: { pending: 3 } }),
            ]),
        },
      ),
    )
    await renderApp('/campaigns')

    await screen.findByRole('link', { name: 'Spring hello' })
    const active = within(rowOf('Spring hello'))
    expect(active.getByText('Active')).toBeVisible()
    expect(active.getByText('4 active, 1 replied')).toBeVisible()
    expect(active.getByText(/2030/)).toBeVisible()
    const draft = within(rowOf('Autumn reconnect'))
    expect(draft.getByText('Draft')).toBeVisible()
    expect(draft.getByText('3 pending')).toBeVisible()
    expect(draft.getByText('—')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Autumn reconnect' })).toHaveAttribute(
      'href',
      '/campaigns/5',
    )
  })

  it('reports a list that will not load', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review() },
        { 'GET /api/v1/campaigns': () => jsonResponse({ detail: 'database is locked' }, 500) },
      ),
    )
    await renderApp('/campaigns')

    expect(await screen.findByRole('alert')).toHaveTextContent('database is locked')
  })
})

describe('campaign builder', () => {
  it('saves a draft with every step field and its audience, then opens it', async () => {
    const calls: Call[] = []
    const state = { campaign: campaign(), review: review({ status: 'draft' }) }
    mockFetch(
      campaignBackend(
        state,
        {
          'POST /api/v1/campaigns': () => jsonResponse(state.campaign, 201),
          'GET /api/v1/mailboxes': () =>
            jsonResponse([
              MAILBOX,
              mailbox({ id: 4, email: 'stale@sender.example', status: 'reauth_required' }),
              mailbox({ id: 6, email: 'off@sender.example', status: 'disabled' }),
            ]),
        },
        calls,
      ),
    )
    const { router } = await renderApp('/campaigns/new')

    const save = await screen.findByRole('button', { name: 'Save draft' })
    expect(save).toBeDisabled()
    expect(screen.getByText(/Give the campaign a name/)).toBeVisible()

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Autumn reconnect' } })
    await screen.findByRole('option', { name: 'me@sender.example' })
    expect(
      screen.getByRole('option', { name: 'stale@sender.example (needs reauth)' }),
    ).toBeDisabled()
    expect(screen.getByRole('option', { name: 'off@sender.example (disabled)' })).toBeDisabled()
    expect(screen.getByRole('option', { name: 'me@sender.example' })).toBeEnabled()
    fireEvent.change(screen.getByLabelText('Mailbox'), { target: { value: '3' } })
    await screen.findAllByRole('option', { name: 'Catching up (email)' })
    const first = within(screen.getByRole('listitem', { name: 'Step 1' }))
    fireEvent.change(first.getByLabelText('Template'), { target: { value: '11' } })

    fireEvent.click(screen.getByRole('button', { name: 'Add a step' }))
    const second = within(screen.getByRole('listitem', { name: 'Step 2' }))
    expect(second.getByLabelText('Delay (days)')).toHaveValue(7)
    expect(second.getByLabelText('Condition')).toHaveValue('no_reply')
    fireEvent.change(second.getByLabelText('Template'), { target: { value: '12' } })
    fireEvent.change(second.getByLabelText('Mode'), { target: { value: 'send' } })
    // An email follow-up after an email starts in its thread; the first step has no box.
    expect(second.getByRole('checkbox')).toBeChecked()
    expect(first.queryByRole('checkbox')).toBeNull()

    fireEvent.change(screen.getByLabelText('Audience from'), { target: { value: 'list' } })
    await screen.findByRole('option', { name: 'Old colleagues (3)' })
    fireEvent.change(screen.getByLabelText('List'), { target: { value: '21' } })

    expect(save).toBeEnabled()
    fireEvent.click(save)

    await waitFor(() => expect(router.state.location.pathname).toBe('/campaigns/5'))
    const post = calls.find((call) => call.method === 'POST' && call.path === '/api/v1/campaigns')
    expect(post?.body).toEqual({
      name: 'Autumn reconnect',
      mailbox_id: 3,
      steps: [
        { template_id: 11, delay_days: 0, mode: 'draft', condition: 'always', same_thread: false },
        { template_id: 12, delay_days: 7, mode: 'send', condition: 'no_reply', same_thread: true },
      ],
      list_id: 21,
    })
  })

  it('offers LinkedIn modes for a LinkedIn step and shows a refusal', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review() },
        {
          'POST /api/v1/campaigns': () =>
            jsonResponse({ detail: 'a campaign named Autumn reconnect exists' }, 409),
        },
      ),
    )
    await renderApp('/campaigns/new')

    fireEvent.change(await screen.findByLabelText('Name'), {
      target: { value: 'Autumn reconnect' },
    })
    await screen.findAllByRole('option', { name: 'LinkedIn hello (LinkedIn)' })
    const step = within(screen.getByRole('listitem', { name: 'Step 1' }))
    fireEvent.change(step.getByLabelText('Template'), { target: { value: '13' } })
    expect(
      within(step.getByLabelText('Mode'))
        .getAllByRole('option')
        .map((o) => o.textContent),
    ).toEqual(['Prefill (you send it)', 'Auto-send'])

    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'a campaign named Autumn reconnect exists',
    )
  })
})

describe('campaign detail', () => {
  it('enrolls the audience and shows the skip summary with its reasons', async () => {
    const calls: Call[] = []
    const state = { campaign: campaign(), review: review({ status: 'draft' }) }
    mockFetch(
      campaignBackend(
        state,
        {
          'POST /api/v1/campaigns/5/enroll': () =>
            jsonResponse({
              campaign_id: 5,
              enrolled: 2,
              already: 0,
              excluded: 1,
              removed: 0,
              pending: 2,
              summary: '2 will start, 1 skipped (1 do-not-contact)',
            }),
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    expect(await screen.findByText(/From the list Old colleagues/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Enroll the audience' }))

    const outcome = await screen.findByText('2 will start, 1 skipped (1 do-not-contact)', {
      selector: 'p',
    })
    expect(outcome).toBeVisible()
    expect(screen.getByText(/2 enrolled, 0 already in, 1 skipped/)).toBeVisible()
    const post = calls.find((call) => call.path === '/api/v1/campaigns/5/enroll')
    expect(post?.body).toEqual({})
  })

  it('warns that a new source replaces the audience, and sends it', async () => {
    const calls: Call[] = []
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review({ status: 'draft' }) },
        {
          'POST /api/v1/campaigns/5/enroll': () =>
            jsonResponse({
              campaign_id: 5,
              enrolled: 4,
              already: 1,
              excluded: 0,
              removed: 2,
              pending: 5,
              summary: '5 will start, none skipped',
            }),
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    fireEvent.click(await screen.findByRole('button', { name: 'Change the source' }))
    expect(screen.getByText('Changing the source replaces the audience.')).toBeVisible()
    await screen.findByRole('option', { name: 'Conference friends (5)' })
    fireEvent.change(screen.getByLabelText('List'), { target: { value: '22' } })
    fireEvent.click(screen.getByRole('button', { name: 'Replace the audience and enroll' }))

    expect(await screen.findByText(/2 removed by the new source/)).toBeVisible()
    const post = calls.find((call) => call.path === '/api/v1/campaigns/5/enroll')
    expect(post?.body).toEqual({ list_id: 22 })
  })

  it('shows progress per step, the enrollments, and pauses an active campaign', async () => {
    const calls: Call[] = []
    const active = campaign({
      status: 'active',
      enrollments: { active: 1, replied: 1 },
      steps: STEPS.map((step) => (step.position === 1 ? { ...step, fired: 2, sent: 1 } : step)),
      next_action_at: '2030-06-20T12:00:00Z',
    })
    const state = { campaign: active, review: review({ status: 'active', missing: [] }) }
    mockFetch(
      campaignBackend(
        state,
        {
          'POST /api/v1/campaigns/5/pause': () => {
            state.campaign = { ...state.campaign, status: 'paused' }
            return jsonResponse(state.campaign)
          },
          'POST /api/v1/campaigns/5/resume': () => {
            state.campaign = { ...state.campaign, status: 'active' }
            return jsonResponse(state.campaign)
          },
        },
        calls,
      ),
    )
    await renderApp('/campaigns/5')

    const stepRow = (await screen.findByRole('rowheader', { name: '1' })).closest('tr')
    // Fired, sent, replied, bounced, opted out (#350), and the timing editor's cell (#338).
    await waitFor(() => {
      const cells = within(stepRow as HTMLElement).getAllByRole('cell')
      expect(cells.slice(-6).map((c) => c.textContent)).toEqual(['2', '1', '0', '0', '0', 'Timing'])
    })
    expect(screen.queryByRole('heading', { name: 'Review' })).toBeNull()

    const enrollment = (await screen.findByRole('link', { name: 'Tobias Marrowbone' })).closest(
      'tr',
    )
    expect(within(enrollment as HTMLElement).getByText('Replied')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Rosalind Quillfeather' })).toHaveAttribute(
      'href',
      '/contacts/401',
    )

    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    expect(await screen.findByRole('button', { name: 'Resume' })).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Autumn reconnect Paused' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    expect(await screen.findByRole('button', { name: 'Pause' })).toBeVisible()
    expect(calls.filter((c) => c.method === 'POST').map((c) => c.path)).toEqual([
      '/api/v1/campaigns/5/pause',
      '/api/v1/campaigns/5/resume',
    ])
  })

  it('searches the enrollments by name and status', async () => {
    const calls: Call[] = []
    mockFetch(
      campaignBackend({ campaign: campaign({ status: 'active' }), review: review() }, {}, calls),
    )
    await renderApp('/campaigns/5')

    fireEvent.change(await screen.findByLabelText('Find an enrollment'), {
      target: { value: 'tobias' },
    })
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'replied' } })
    fireEvent.click(screen.getByRole('button', { name: 'Find' }))

    await waitFor(() => {
      // Not the one-row count the filter summary asks for ("of N").
      const last = calls
        .filter((c) => c.path === '/api/v1/campaigns/5/enrollments' && c.query.get('limit') !== '1')
        .at(-1)
      expect(last?.query.get('q')).toBe('tobias')
      expect(last?.query.get('status')).toBe('replied')
    })
  })

  it('shows what narrows the enrollments, how many of how many, and clears it', async () => {
    const enrollments = (call: Call) => {
      const q = call.query.get('q') ?? ''
      const status = call.query.get('status')
      if (q === 'nobody') return jsonResponse({ total: 0, items: [] })
      if (q !== '' || status !== null) {
        return jsonResponse({ total: 1, items: [ENROLLMENTS.items[1]] })
      }
      return jsonResponse({ ...ENROLLMENTS, total: 87 })
    }
    mockFetch(
      campaignBackend(
        { campaign: campaign({ status: 'active' }), review: review() },
        { 'GET /api/v1/campaigns/5/enrollments': enrollments },
      ),
    )
    await renderApp('/campaigns/5')
    await screen.findByRole('link', { name: 'Tobias Marrowbone' })
    // Nothing narrows the list yet, so nothing says it does.
    expect(screen.queryByRole('list', { name: 'Active filters' })).toBeNull()

    fireEvent.change(screen.getByLabelText('Find an enrollment'), { target: { value: 'acme' } })
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'replied' } })
    fireEvent.click(screen.getByRole('button', { name: 'Find' }))

    await waitFor(() => expect(screen.getByText(/Showing 1 of 87 · filtered by:/)).toBeVisible())
    const chips = within(screen.getByRole('list', { name: 'Active filters' }))
    expect(chips.getAllByRole('button').map((chip) => chip.textContent)).toEqual([
      '“acme”',
      'status: replied',
    ])

    // One chip removes one filter.
    fireEvent.click(chips.getByRole('button', { name: 'Remove status: replied' }))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Remove status: replied' })).toBeNull(),
    )
    expect(screen.getByLabelText('Status')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Remove “acme”' })).toBeVisible()

    // Clear takes the rest, and the box with it.
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(screen.queryByRole('list', { name: 'Active filters' })).toBeNull())
    expect(screen.getByLabelText('Find an enrollment')).toHaveValue('')
  })

  it('says no enrollment matches the filters, not that there are none', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign({ status: 'active' }), review: review() },
        {
          'GET /api/v1/campaigns/5/enrollments': (call) =>
            call.query.get('q') === 'nobody'
              ? jsonResponse({ total: 0, items: [] })
              : jsonResponse(ENROLLMENTS),
        },
      ),
    )
    await renderApp('/campaigns/5')
    fireEvent.change(await screen.findByLabelText('Find an enrollment'), {
      target: { value: 'nobody' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Find' }))

    expect(await screen.findByText('No enrollments match these filters')).toBeVisible()
    expect(screen.queryByText('No enrollments.')).toBeNull()
    await waitFor(() => expect(screen.getByText(/Showing 0 of 2 · filtered by:/)).toBeVisible())

    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(await screen.findByRole('link', { name: 'Tobias Marrowbone' })).toBeVisible()
    expect(screen.getByLabelText('Find an enrollment')).toHaveFocus()
  })

  it('says so for a campaign that does not exist', async () => {
    mockFetch(
      campaignBackend(
        { campaign: campaign(), review: review() },
        { 'GET /api/v1/campaigns/5': () => jsonResponse({ detail: 'no campaign 5' }, 404) },
      ),
    )
    await renderApp('/campaigns/5')

    expect(await screen.findByText('No such campaign')).toBeVisible()
  })
})
