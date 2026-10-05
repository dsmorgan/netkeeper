/**
 * The inbox page (P3-11b). Everyone here is invented, with `.example`
 * addresses; timestamps are mid-day UTC mid-year, so the year read off one is
 * the same in every time zone (#285).
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { campaign, campaignBackend, review } from '@/features/campaigns/test-support'
import { backend, type Call } from '@/features/imports/test-support'
import { jsonResponse, mockFetch } from '@/test/fetch'
import { renderApp } from '@/test/render'

import type { InboxItem, InboxPage } from './api'

function item(overrides: Partial<InboxItem> = {}): InboxItem {
  return {
    id: 901,
    kind: 'reply',
    contact_id: 402,
    contact_name: 'Tobias Marrowbone',
    campaign_id: 7,
    campaign_name: 'Spring hello',
    enrollment_id: 302,
    enrollment_status: 'replied',
    channel: 'email',
    li_conversation_urn: null,
    subject: 'Re: Catching up',
    snippet: 'Good to hear <b>from</b> you',
    received_at: '2030-06-18T12:00:00Z',
    handled_at: null,
    ...overrides,
  }
}

const PAGE: InboxPage = {
  total: 3,
  unhandled: 3,
  items: [
    item(),
    item({
      id: 902,
      kind: 'unsubscribe',
      contact_id: 403,
      contact_name: 'Wren Halloway',
      enrollment_id: 303,
      enrollment_status: 'opted_out',
      snippet: 'Please remove me from this list',
    }),
    item({
      id: 903,
      kind: 'bounce',
      contact_id: 404,
      contact_name: 'Ines Corvell',
      enrollment_id: 304,
      enrollment_status: 'bounced',
      subject: 'Catching up',
      snippet: null,
    }),
  ],
}

function inbox(page: InboxPage | (() => Response), calls: Call[] = [], extra = {}) {
  return backend(
    {
      'GET /api/v1/inbox': () => (typeof page === 'function' ? page() : jsonResponse(page)),
      ...extra,
    },
    calls,
  )
}

function rowOf(name: RegExp): HTMLElement {
  return screen.getByRole('listitem', { name })
}

describe('inbox', () => {
  it('shows a loading note until the list arrives', async () => {
    mockFetch(backend({ 'GET /api/v1/inbox': () => new Promise<Response>(() => {}) }))
    await renderApp('/inbox')

    expect(await screen.findByText('Loading the inbox…')).toBeVisible()
  })

  it('says so when nothing is left to handle, and asks for the unhandled only', async () => {
    const calls: Call[] = []
    mockFetch(inbox({ total: 0, unhandled: 0, items: [] }, calls))
    await renderApp('/inbox')

    expect(await screen.findByText('Nothing to handle')).toBeVisible()
    const listed = calls.find((c) => c.path === '/api/v1/inbox')
    expect(listed?.query.get('handled')).toBe('false')
    expect(listed?.query.get('limit')).toBe('25')
  })

  it('reports an inbox that will not load', async () => {
    mockFetch(inbox(() => jsonResponse({ detail: 'database is locked' }, 500)))
    await renderApp('/inbox')

    expect(await screen.findByRole('alert')).toHaveTextContent('database is locked')
    expect(screen.getByText('The inbox is unavailable.')).toBeVisible()
  })

  it('badges each item by channel, and links a LinkedIn reply to its conversation (#383)', async () => {
    mockFetch(
      inbox({
        total: 2,
        unhandled: 2,
        items: [
          item(),
          item({
            id: 905,
            channel: 'linkedin',
            li_conversation_urn: 'urn:li:msg_conversation:2-INVENTEDTHREAD',
            contact_name: 'Ada Pemberton',
            subject: null,
            snippet: 'Sounds good, next week works',
          }),
        ],
      }),
    )
    await renderApp('/inbox')

    const linkedin = within(
      await screen.findByRole('listitem', { name: /^Reply from Ada Pemberton on LinkedIn$/ }),
    )
    expect(linkedin.getByText('LinkedIn')).toBeVisible()
    expect(linkedin.getByText('Sounds good, next week works')).toBeVisible()
    const thread = linkedin.getByRole('link', { name: 'Open the conversation on LinkedIn' })
    expect(thread).toHaveAttribute(
      'href',
      'https://www.linkedin.com/messaging/thread/2-INVENTEDTHREAD/',
    )
    expect(thread).toHaveAttribute('rel', 'noopener noreferrer')
    expect(linkedin.queryByText(/Gmail/)).toBeNull()
    expect(linkedin.queryByText('(no subject)')).toBeNull()
    expect(linkedin.getByText('LinkedIn message')).toBeVisible()

    const email = within(screen.getByRole('listitem', { name: /^Reply from Tobias/ }))
    expect(email.getByText('Email')).toBeVisible()
    expect(email.getByText('Open the thread in Gmail to read the rest.')).toBeVisible()
    expect(email.queryByRole('link', { name: /on LinkedIn/ })).toBeNull()
  })

  it('never links a LinkedIn reply whose conversation was not recorded', async () => {
    mockFetch(
      inbox({
        total: 1,
        unhandled: 1,
        items: [item({ id: 906, channel: 'linkedin', li_conversation_urn: null, subject: null })],
      }),
    )
    await renderApp('/inbox')

    const row = within(await screen.findByRole('listitem', { name: /on LinkedIn$/ }))
    expect(row.queryByRole('link', { name: /on LinkedIn/ })).toBeNull()
    expect(row.getByText(/the conversation was not recorded/)).toBeVisible()
  })

  it('lists each kind with its links, and the snippet as plain text', async () => {
    mockFetch(inbox(PAGE))
    await renderApp('/inbox')

    const reply = within(await screen.findByRole('listitem', { name: /^Reply from Tobias/ }))
    expect(reply.getByText('Reply')).toBeVisible()
    expect(reply.getByText('Replied')).toBeVisible()
    expect(reply.getByText(/2030/)).toBeVisible()
    expect(reply.getByRole('link', { name: 'Tobias Marrowbone' })).toHaveAttribute(
      'href',
      '/contacts/402',
    )
    expect(reply.getByRole('link', { name: 'Spring hello' })).toHaveAttribute(
      'href',
      '/campaigns/7',
    )
    const snippet = reply.getByText('Good to hear <b>from</b> you')
    expect(snippet.querySelector('b')).toBeNull()

    const unsubscribe = within(rowOf(/^Unsubscribe from Wren/))
    expect(unsubscribe.getByText('Opted out')).toBeVisible()
    const bounce = within(rowOf(/^Bounce from Ines/))
    expect(bounce.getByText('Bounced: Catching up')).toBeVisible()
    expect(screen.getByText('3 unhandled of every kind.')).toBeVisible()
  })

  it('names a LinkedIn reply, which has no subject (P4-02)', async () => {
    mockFetch(
      inbox({
        total: 1,
        unhandled: 1,
        items: [item({ channel: 'linkedin', subject: null, snippet: 'Happy to talk next week' })],
      }),
    )
    await renderApp('/inbox')

    const reply = within(await screen.findByRole('listitem', { name: /^Reply from Tobias/ }))
    expect(reply.getByText('LinkedIn message')).toBeVisible()
    expect(reply.getByText('Happy to talk next week')).toBeVisible()
    expect(reply.queryByText('(no subject)')).toBeNull()
  })

  it('filters by kind and by handled state', async () => {
    const calls: Call[] = []
    mockFetch(inbox(PAGE, calls))
    await renderApp('/inbox')
    await screen.findByRole('listitem', { name: /^Reply from Tobias/ })

    fireEvent.change(screen.getByLabelText('Kind'), { target: { value: 'bounce' } })
    fireEvent.change(screen.getByLabelText('Show'), { target: { value: 'all' } })

    await waitFor(() => {
      // Not the unfiltered count the filter summary asks for ("of N"), which has no kind.
      const last = calls.filter((c) => c.path === '/api/v1/inbox' && c.query.has('kind')).at(-1)
      expect(last?.query.get('kind')).toBe('bounce')
      expect(last?.query.has('handled')).toBe(false)
    })
  })

  it('shows the kind filter as a chip, with how many of how many', async () => {
    const calls: Call[] = []
    mockFetch(
      backend(
        {
          'GET /api/v1/inbox': (call) =>
            call.query.get('kind') === 'bounce'
              ? jsonResponse({ total: 1, unhandled: 3, items: [PAGE.items[2]] })
              : jsonResponse(PAGE),
        },
        calls,
      ),
    )
    await renderApp('/inbox')
    await screen.findByRole('listitem', { name: /^Reply from Tobias/ })
    expect(screen.queryByRole('list', { name: 'Active filters' })).toBeNull()

    fireEvent.change(screen.getByLabelText('Kind'), { target: { value: 'bounce' } })
    await waitFor(() => expect(screen.getByText(/Showing 1 of 3 · filtered by:/)).toBeVisible())
    // The whole is counted from one row of the unfiltered inbox.
    const whole = calls.filter((c) => c.path === '/api/v1/inbox' && !c.query.has('kind')).at(-1)
    expect(whole?.query.get('limit')).toBe('1')

    fireEvent.click(screen.getByRole('button', { name: 'Remove kind: bounce' }))
    await waitFor(() => expect(screen.queryByRole('list', { name: 'Active filters' })).toBeNull())
    expect(screen.getByLabelText('Kind')).toHaveValue('')
    // The last chip gone, the focus goes back to the filter it came from.
    await waitFor(() => expect(screen.getByLabelText('Kind')).toHaveFocus())
  })

  it('says nothing matches the filters, not that the inbox is empty', async () => {
    mockFetch(
      backend({
        'GET /api/v1/inbox': (call) =>
          call.query.get('kind') === 'unsubscribe'
            ? jsonResponse({ total: 0, unhandled: 3, items: [] })
            : jsonResponse(PAGE),
      }),
    )
    await renderApp('/inbox')
    await screen.findByRole('listitem', { name: /^Reply from Tobias/ })

    fireEvent.change(screen.getByLabelText('Kind'), { target: { value: 'unsubscribe' } })
    expect(await screen.findByText('Nothing matches these filters')).toBeVisible()
    expect(screen.queryByText('Nothing to handle')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(await screen.findByRole('listitem', { name: /^Reply from Tobias/ })).toBeVisible()
  })

  it('keeps the kind but goes back to Unhandled when the enrollment chip goes', async () => {
    const calls: Call[] = []
    mockFetch(inbox(PAGE, calls))
    const { router } = await renderApp('/inbox?enrollment=302')
    await screen.findByRole('listitem', { name: /^Reply from Tobias/ })
    // One enrollment's messages show handled and unhandled alike.
    expect(screen.getByLabelText('Show')).toHaveValue('all')
    const name = 'Remove enrollment: Tobias Marrowbone in Spring hello'
    expect(screen.getByRole('button', { name })).toBeVisible()

    fireEvent.change(screen.getByLabelText('Kind'), { target: { value: 'bounce' } })
    // The chip keeps its name while the next page loads; it never says "one enrollment".
    expect(screen.getByRole('button', { name })).toBeVisible()
    await waitFor(() =>
      expect(
        calls
          .filter((c) => c.path === '/api/v1/inbox')
          .at(-1)
          ?.query.get('kind'),
      ).toBe('bounce'),
    )

    fireEvent.click(screen.getByRole('button', { name }))
    await waitFor(() => expect(router.state.location.searchStr).toBe(''))
    expect(screen.getByLabelText('Kind')).toHaveValue('bounce')
    expect(screen.getByLabelText('Show')).toHaveValue('unhandled')
    expect(screen.getByRole('button', { name: 'Remove kind: bounce' })).toBeVisible()
    await waitFor(() => {
      const last = calls
        .filter((c) => c.path === '/api/v1/inbox' && c.query.get('kind') === 'bounce')
        .at(-1)
      expect(last?.query.has('enrollment_id')).toBe(false)
      expect(last?.query.get('handled')).toBe('false')
    })
  })

  it('starts afresh from the sidebar link, whatever the enrollment view had', async () => {
    mockFetch(inbox(PAGE))
    const { router } = await renderApp('/inbox?enrollment=302')
    await screen.findByRole('listitem', { name: /^Reply from Tobias/ })
    fireEvent.change(screen.getByLabelText('Kind'), { target: { value: 'bounce' } })

    const nav = screen.getByRole('navigation', { name: 'Primary' })
    fireEvent.click(within(nav).getByRole('link', { name: 'Inbox' }))
    await waitFor(() => expect(router.state.location.searchStr).toBe(''))
    expect(screen.getByLabelText('Kind')).toHaveValue('')
    expect(screen.getByLabelText('Show')).toHaveValue('unhandled')
    expect(screen.queryByRole('list', { name: 'Active filters' })).toBeNull()
  })

  it('still says how many match when the whole count fails', async () => {
    mockFetch(
      backend({
        'GET /api/v1/inbox': (call) =>
          call.query.get('kind') === 'bounce'
            ? jsonResponse({ total: 1, unhandled: 3, items: [PAGE.items[2]] })
            : call.query.get('limit') === '1'
              ? jsonResponse({ detail: 'the database is locked' }, 500)
              : jsonResponse(PAGE),
      }),
    )
    await renderApp('/inbox')
    await screen.findByRole('listitem', { name: /^Reply from Tobias/ })
    fireEvent.change(screen.getByLabelText('Kind'), { target: { value: 'bounce' } })
    await waitFor(() =>
      expect(screen.getByText(/filtered by:/, { selector: '[role="status"]' })).toHaveTextContent(
        /^Showing 1 · filtered by:/,
      ),
    )
  })

  it('still lets you drop the enrollment when its page fails', async () => {
    mockFetch(
      backend({
        'GET /api/v1/inbox': (call) =>
          call.query.has('enrollment_id')
            ? jsonResponse({ detail: 'the database is locked' }, 500)
            : jsonResponse(PAGE),
      }),
    )
    const { router } = await renderApp('/inbox?enrollment=302')
    fireEvent.click(await screen.findByRole('button', { name: 'Remove this enrollment' }))
    await waitFor(() => expect(router.state.location.searchStr).toBe(''))
    expect(await screen.findByRole('listitem', { name: /^Reply from Tobias/ })).toBeVisible()
  })

  it('marks an item handled, then reloads the list', async () => {
    const calls: Call[] = []
    let page = PAGE
    mockFetch(
      inbox(() => jsonResponse(page), calls, {
        'PUT /api/v1/inbox/901/handled': () => {
          const handled = item({ handled_at: '2030-06-19T12:00:00Z' })
          page = { ...PAGE, unhandled: 2, items: [handled, ...PAGE.items.slice(1)] }
          return jsonResponse(handled)
        },
      }),
    )
    await renderApp('/inbox')
    const reply = within(await screen.findByRole('listitem', { name: /^Reply from Tobias/ }))

    fireEvent.click(reply.getByRole('button', { name: 'Mark handled' }))

    expect(await reply.findByRole('button', { name: 'Mark unhandled' })).toBeVisible()
    expect(reply.getByText(/^Handled .*2030/)).toBeVisible()
    const put = calls.find((c) => c.method === 'PUT')
    expect(put?.body).toEqual({ handled: true })
  })

  it('adds a note to the contact', async () => {
    const calls: Call[] = []
    mockFetch(
      inbox(PAGE, calls, {
        'POST /api/v1/contacts/402/interactions': () => jsonResponse({ id: 1 }, 201),
      }),
    )
    await renderApp('/inbox')
    const reply = within(await screen.findByRole('listitem', { name: /^Reply from Tobias/ }))

    fireEvent.click(reply.getByRole('button', { name: 'Add note' }))
    const save = reply.getByRole('button', { name: 'Save note' })
    expect(save).toBeDisabled()
    fireEvent.change(reply.getByLabelText('Note on Tobias Marrowbone'), {
      target: { value: 'Coffee next week' },
    })
    fireEvent.click(save)

    expect(await reply.findByText('Note added to the contact.')).toBeVisible()
    const post = calls.find((c) => c.method === 'POST')
    expect(post?.body).toMatchObject({ kind: 'note', summary: 'Coffee next week' })
  })

  it("narrows to one enrollment's messages from the campaign page's link", async () => {
    const calls: Call[] = []
    mockFetch(inbox(PAGE, calls))
    const { router } = await renderApp('/inbox?enrollment=302')
    await screen.findByRole('listitem', { name: /^Reply from Tobias/ })

    const listed = calls.find((c) => c.path === '/api/v1/inbox')
    expect(listed?.query.get('enrollment_id')).toBe('302')
    expect(listed?.query.has('handled')).toBe(false)
    // The enrollment is a filter like any other: a chip that names whose it is.
    const chip = screen.getByRole('button', {
      name: 'Remove enrollment: Tobias Marrowbone in Spring hello',
    })
    fireEvent.click(chip)
    await waitFor(() => expect(router.state.location.searchStr).toBe(''))
    await waitFor(() =>
      expect(screen.queryByRole('list', { name: 'Active filters' })).not.toBeInTheDocument(),
    )
  })
})

describe('campaign page', () => {
  it('links a replied enrollment to the inbox', async () => {
    const state = { campaign: campaign(), review: review() }
    mockFetch(campaignBackend(state))
    await renderApp(`/campaigns/${state.campaign.id}`)

    const link = await screen.findByRole('link', { name: 'View in inbox' })
    expect(link).toHaveAttribute('href', '/inbox?enrollment=302')
    expect(screen.getAllByRole('link', { name: 'View in inbox' })).toHaveLength(1)
  })
})
