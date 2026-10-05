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
      const last = calls.filter((c) => c.path === '/api/v1/inbox').at(-1)
      expect(last?.query.get('kind')).toBe('bounce')
      expect(last?.query.has('handled')).toBe(false)
    })
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
    await renderApp('/inbox?enrollment=302')
    await screen.findByRole('listitem', { name: /^Reply from Tobias/ })

    const listed = calls.find((c) => c.path === '/api/v1/inbox')
    expect(listed?.query.get('enrollment_id')).toBe('302')
    expect(listed?.query.has('handled')).toBe(false)
    expect(screen.getByRole('link', { name: 'Show every enrollment' })).toHaveAttribute(
      'href',
      '/inbox',
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
