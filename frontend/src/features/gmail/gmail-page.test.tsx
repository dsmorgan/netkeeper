import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { EventStreamContext } from '@/features/events/event-stream-context'
import { navigation } from '@/features/mailboxes/api'
import { mailbox, status } from '@/features/mailboxes/test-support'
import { FakeEventSource, resetFakeEventSource } from '@/test/fake-event-source'
import { jsonResponse, mockFetch } from '@/test/fetch'

import type { GmailActivity } from './api'
import { GmailPage } from './gmail-page'
import { capWarning } from './cap'

afterEach(() => {
  vi.restoreAllMocks()
  resetFakeEventSource()
})

const NOW = new Date().toISOString()

function activity(overrides: Partial<GmailActivity> = {}): GmailActivity {
  return {
    day_start: NOW,
    day_end: NOW,
    timezone: 'America/Chicago',
    mailboxes: [],
    recent: [],
    ...overrides,
  }
}

function pollStatus(overrides: Record<string, unknown> = {}) {
  return {
    checked_at: NOW,
    background_running: true,
    items: [
      {
        key: 'gmail_replies',
        group: 'gmail',
        label: 'Gmail replies',
        state: 'scheduled',
        interval_minutes: 10,
        last_at: new Date(Date.now() - 3 * 60_000).toISOString(),
        next_at: new Date(Date.now() + 7 * 60_000).toISOString(),
        reason: null,
        requested: false,
        requested_at: null,
      },
      {
        key: 'linkedin_inbox',
        group: 'linkedin',
        label: 'LinkedIn inbox',
        state: 'off',
        interval_minutes: 60,
        last_at: null,
        next_at: null,
        reason: 'LinkedIn only reason',
        requested: false,
        requested_at: null,
      },
    ],
    mailboxes: [],
    ...overrides,
  }
}

function protection(name: string, overrides: Record<string, unknown> = {}) {
  return {
    name,
    key: null,
    status: 'on',
    value: `${name} value`,
    summary: `${name} summary`,
    warnings: [],
    notes: [],
    ...overrides,
  }
}

function posture() {
  return {
    checked_at: NOW,
    timezone: 'America/Chicago',
    local_time: NOW,
    protections: [
      protection('attach-only browser'),
      protection('reply poll', { summary: 'every 10 min; 1 armed mailbox' }),
      protection('sending hours', {
        status: 'unknown',
        warnings: ['Sending hours are not set.'],
      }),
      protection('heat skip gate'),
    ],
    warnings: [],
    notes: [],
    gaps: [],
    ok: true,
    verdict: 'nothing is misconfigured',
  }
}

interface Backend {
  mailboxes?: ReturnType<typeof status>
  activity?: GmailActivity
  poll?: ReturnType<typeof pollStatus>
}

function renderPage(backend: Backend = {}) {
  const calls: { method: string; path: string }[] = []
  mockFetch((request) => {
    const { pathname } = new URL(request.url)
    calls.push({ method: request.method, path: pathname })
    if (pathname === '/api/v1/mailboxes/status') {
      return jsonResponse(
        backend.mailboxes ?? status({ client_configured: false, client_id: null }),
      )
    }
    if (pathname === '/api/v1/gmail/activity') return jsonResponse(backend.activity ?? activity())
    if (pathname === '/api/v1/poll-status') return jsonResponse(backend.poll ?? pollStatus())
    if (pathname === '/api/v1/posture') return jsonResponse(posture())
    if (request.method === 'POST' && pathname === '/api/v1/mailboxes/oauth/start') {
      return jsonResponse({ authorization_url: 'https://accounts.example.test/auth' })
    }
    return jsonResponse({ detail: `unexpected ${request.method} ${pathname}` }, 500)
  })
  const source = new FakeEventSource('/api/v1/events')
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({ component: () => <Outlet /> })
  const routeTree = root.addChildren([
    createRoute({ getParentRoute: () => root, path: '/', component: GmailPage }),
    createRoute({
      getParentRoute: () => root,
      path: '/settings',
      component: () => <p>Settings</p>,
    }),
  ])
  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: ['/'] }),
  })
  render(
    <QueryClientProvider client={queryClient}>
      <EventStreamContext value={{ status: 'connected', source: source as unknown as EventSource }}>
        <RouterProvider router={router} />
      </EventStreamContext>
    </QueryClientProvider>,
  )
  return { calls, source }
}

describe('Gmail page', () => {
  it('says what to do when Gmail was never connected, and shows no send or poll history', async () => {
    renderPage()

    expect(await screen.findByText(/No Gmail account is connected yet/)).toBeInTheDocument()
    expect(screen.getByText('not set')).toBeInTheDocument()
    const connect = screen.getByRole('region', { name: 'Connect or reconnect' })
    expect(within(connect).getByText('netkeeper gmail client <file>')).toBeInTheDocument()
    expect(within(connect).getByText('netkeeper gmail login')).toBeInTheDocument()
    expect(await screen.findByText(/nothing is sent/)).toBeInTheDocument()
    expect(await screen.findByText('No email yet.')).toBeInTheDocument()
  })

  it('shows the account, the client, and how it is armed', async () => {
    renderPage({
      mailboxes: status({
        mailboxes: [mailbox({ email: 'sender@example.com', arm: 'draft', armed_at: NOW })],
      }),
    })

    expect(await screen.findByText('sender@example.com')).toBeInTheDocument()
    expect(screen.getByText('configured')).toBeInTheDocument()
    expect(screen.getByText('1234-fake.apps.googleusercontent.com')).toBeInTheDocument()
    expect(screen.getByText('connected')).toBeInTheDocument()
    expect(screen.getByText('armed: drafts only')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Re-authorize' })).not.toBeInTheDocument()
  })

  it('re-authorizes a mailbox that needs it', async () => {
    const assign = vi.spyOn(navigation, 'assign').mockImplementation(() => undefined)
    renderPage({
      mailboxes: status({
        mailboxes: [mailbox({ status: 'reauth_required', status_reason: 'invalid_grant' })],
      }),
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Re-authorize' }))

    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://accounts.example.test/auth'))
  })

  it('shows today’s sends against the cap, warning near and at it', async () => {
    renderPage({
      mailboxes: status({ mailboxes: [mailbox({ id: 3 })] }),
      activity: activity({
        mailboxes: [
          { mailbox_id: 3, email: 'sender@example.com', sent_today: 80, daily_cap: 80 },
          { mailbox_id: 4, email: 'other@example.com', sent_today: 10, daily_cap: 80 },
        ],
      }),
    })

    const meter = await screen.findByRole('meter', { name: 'sender@example.com sends today' })
    expect(meter).toHaveAttribute('aria-valuenow', '80')
    expect(await screen.findByText(/sender@example.com is at today’s cap/)).toBeInTheDocument()
    expect(screen.getAllByRole('note', { name: 'Daily cap' })).toHaveLength(1)
  })

  it('warns before the cap is reached, and not when well under it', () => {
    const base = { mailbox_id: 1, email: 'a@example.com', daily_cap: 10 }
    expect(capWarning({ ...base, sent_today: 3 })).toBeNull()
    expect(capWarning({ ...base, sent_today: 8 })).toMatch(/close to today’s cap: 2 left/)
    expect(capWarning({ ...base, sent_today: 12 })).toMatch(/at today’s cap/)
  })

  it('shows the Gmail checks only, and the reply poll’s last and next time', async () => {
    renderPage({
      mailboxes: status({ mailboxes: [mailbox()] }),
      poll: pollStatus({
        mailboxes: [
          {
            mailbox_id: 3,
            email: 'sender@example.com',
            armed: true,
            state: 'scheduled',
            replies_polled_at: new Date(Date.now() - 3 * 60_000).toISOString(),
            next_at: new Date(Date.now() + 7 * 60_000).toISOString(),
            reason: null,
          },
        ],
      }),
    })

    const poll = await screen.findByRole('region', { name: 'Gmail replies' })
    expect(within(poll).getByText('every 10 min')).toBeInTheDocument()
    expect(within(poll).getByText(/checked 3 min ago · next in 7 min/)).toBeInTheDocument()
    expect(within(poll).getByRole('button', { name: 'Check now' })).toBeEnabled()
    expect(screen.queryByText('LinkedIn inbox')).not.toBeInTheDocument()
    expect(await screen.findByText(/Replies checked 3 min ago/)).toBeInTheDocument()
  })

  it('says why the reply poll is not running', async () => {
    renderPage({
      poll: pollStatus({
        background_running: false,
        items: [
          {
            ...pollStatus().items[0],
            state: 'blocked',
            last_at: null,
            next_at: null,
            reason: 'The Keychain refused.',
          },
        ],
      }),
    })

    expect(await screen.findByText(/netkeeper serve isn’t running/)).toBeInTheDocument()
    expect(screen.getByText('The Keychain refused.')).toBeInTheDocument()
    expect(screen.getByText('needs attention')).toBeInTheDocument()
  })

  it('lists recent email, errors included, and no subject or body', async () => {
    renderPage({
      activity: activity({
        recent: [
          {
            id: 2,
            direction: 'in',
            status: 'received',
            at: NOW,
            campaign_id: 1,
            campaign_name: 'Autumn hello',
            step_position: 1,
            contact_id: 9,
            contact_name: 'Fictional Person',
            error: null,
          },
          {
            id: 1,
            direction: 'out',
            status: 'failed',
            at: NOW,
            campaign_id: 1,
            campaign_name: 'Autumn hello',
            step_position: 2,
            contact_id: 9,
            contact_name: 'Fictional Person',
            error: 'Gmail refused',
          },
        ],
      }),
    })

    const rows = await screen.findAllByRole('row')
    expect(rows).toHaveLength(3)
    expect(within(rows[1]!).getByText('Reply')).toBeInTheDocument()
    expect(within(rows[2]!).getByText('failed')).toBeInTheDocument()
    expect(within(rows[2]!).getByText('Gmail refused')).toBeInTheDocument()
    expect(within(rows[2]!).getByText('Autumn hello, step 2')).toBeInTheDocument()
  })

  it('shows only the posture rows that concern Gmail', async () => {
    renderPage()

    const list = await screen.findByRole('list', { name: 'Gmail posture' })
    const names = Array.from(list.children).map((item) => item.querySelector('span')?.textContent)
    expect(names).toEqual(['reply poll', 'sending hours'])
    expect(within(list).getByText('Sending hours are not set.')).toBeInTheDocument()
  })

  it('never posts to anything that calls Gmail', async () => {
    const { calls } = renderPage({ mailboxes: status({ mailboxes: [mailbox()] }) })
    await screen.findByText('sender@example.com')

    expect(calls.filter((call) => call.method !== 'GET')).toEqual([])
  })
})
