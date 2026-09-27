import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { EventStreamContext } from '@/features/events/event-stream-context'
import { navigation, type MailboxStatus } from '@/features/mailboxes/api'
import { mailbox, status } from '@/features/mailboxes/test-support'
import { FakeEventSource, resetFakeEventSource } from '@/test/fake-event-source'
import { jsonResponse, mockFetch } from '@/test/fetch'

import { MailboxCard } from './mailbox-card'

type Handler = (body: unknown) => Response

/**
 * `MailboxCard` renders a `Link`, so it needs a router beneath it — a
 * minimal one, with a dummy `/settings` to land on, rather than the whole
 * app tree. Otherwise the same fake backend and event source as the
 * mailboxes feature's own `renderWithBackend`.
 */
function renderCard(current: () => MailboxStatus, routes: Record<string, Handler> = {}) {
  const calls: { method: string; path: string; body: unknown }[] = []
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url)
    const text = request.method === 'GET' ? '' : await request.text()
    const body: unknown = text === '' ? null : JSON.parse(text)
    calls.push({ method: request.method, path: pathname, body })
    const route = routes[`${request.method} ${pathname}`]
    if (route !== undefined) return route(body)
    if (pathname === '/api/v1/mailboxes/status') return jsonResponse(current())
    if (pathname === '/api/v1/mailboxes') return jsonResponse(current().mailboxes)
    return jsonResponse({ detail: `unexpected ${request.method} ${pathname}` }, 500)
  })
  const source = new FakeEventSource('/api/v1/events')
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  const root = createRootRoute({ component: () => <Outlet /> })
  const routeTree = root.addChildren([
    createRoute({ getParentRoute: () => root, path: '/', component: MailboxCard }),
    createRoute({
      getParentRoute: () => root,
      path: '/settings',
      component: () => <p>Settings page</p>,
    }),
  ])
  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: ['/'] }),
  })

  const utils = render(
    <QueryClientProvider client={queryClient}>
      <EventStreamContext value={{ status: 'connected', source: source as unknown as EventSource }}>
        <RouterProvider router={router} />
      </EventStreamContext>
    </QueryClientProvider>,
  )
  return { ...utils, calls, source, router }
}

afterEach(() => {
  resetFakeEventSource()
  vi.restoreAllMocks()
})

describe('MailboxCard', () => {
  it('shows the connected address and when its token was last refreshed', async () => {
    renderCard(() => status({ mailboxes: [mailbox()] }))
    expect(await screen.findByText('sender@example.com')).toBeInTheDocument()
    expect(screen.getByText('connected')).toBeInTheDocument()
    expect(screen.getByText(/Token last refreshed/)).toHaveTextContent('2026')
    expect(screen.getByRole('link', { name: 'Settings' })).toBeInTheDocument()
  })

  it('says a mailbox needs re-authorizing and offers to fix it', async () => {
    renderCard(() =>
      status({
        mailboxes: [mailbox({ status: 'reauth_required', status_reason: 'invalid_grant' })],
      }),
    )
    expect(await screen.findByText('needs re-authorizing')).toBeInTheDocument()
    expect(
      screen.getByText(/revoked, or the consent screen is still in Testing/),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Re-authorize' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Settings' })).not.toBeInTheDocument()
  })

  it('re-authorizes on Google’s page', async () => {
    const assign = vi.spyOn(navigation, 'assign').mockImplementation(() => {})
    const { calls } = renderCard(
      () => status({ mailboxes: [mailbox({ status: 'reauth_required' })] }),
      {
        'POST /api/v1/mailboxes/oauth/start': () =>
          jsonResponse({ authorization_url: 'https://accounts.example/auth?x=1' }),
      },
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Re-authorize' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://accounts.example/auth?x=1'))
    expect(calls).toContainEqual({
      method: 'POST',
      path: '/api/v1/mailboxes/oauth/start',
      body: { mailbox_id: 3 },
    })
  })

  it('shows a disconnected mailbox as disconnected, pointing at Settings', async () => {
    renderCard(() => status({ mailboxes: [mailbox({ status: 'disabled' })] }))
    expect(await screen.findByText('disconnected')).toBeInTheDocument()
    expect(screen.getByText('sender@example.com')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Settings' })).toBeInTheDocument()
  })

  it('points to Gmail setup when no mailbox has ever been connected', async () => {
    renderCard(() => status({ mailboxes: [] }))
    expect(await screen.findByText('No Gmail account is connected yet.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Set up Gmail' })).toBeInTheDocument()
  })

  it('shows while the Keychain is locked, never asking for the status', async () => {
    const { calls } = renderCard(
      () => status({ mailboxes: [mailbox({ status: 'reauth_required' })] }),
      {
        'GET /api/v1/mailboxes/status': () =>
          jsonResponse({ detail: 'the Keychain is locked' }, 503),
      },
    )
    expect(await screen.findByText('needs re-authorizing')).toBeInTheDocument()
    expect(calls.map((call) => `${call.method} ${call.path}`)).toEqual(['GET /api/v1/mailboxes'])
  })

  it('shows the live mailbox, not a disabled one, when both are in the list', async () => {
    renderCard(() =>
      status({
        mailboxes: [
          mailbox({ id: 1, email: 'old@example.com', status: 'disabled' }),
          mailbox({ id: 2, email: 'sender@example.com', status: 'ok' }),
        ],
      }),
    )
    expect(await screen.findByText('sender@example.com')).toBeInTheDocument()
    expect(screen.getByText('connected')).toBeInTheDocument()
    expect(screen.queryByText('old@example.com')).not.toBeInTheDocument()
  })

  it('shows an alert when the mailbox list could not be fetched', async () => {
    renderCard(() => status({ mailboxes: [] }), {
      'GET /api/v1/mailboxes': () => jsonResponse({ detail: 'boom' }, 500),
    })
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Mailbox status could not be checked.',
    )
  })

  it('formats the token refresh time for a person, not as a raw UTC timestamp', async () => {
    const checkedAt = '2026-03-14T09:30:00Z'
    renderCard(() => status({ mailboxes: [mailbox({ checked_at: checkedAt })] }))
    const expected = new Date(checkedAt).toLocaleString()
    expect(await screen.findByText(`Token last refreshed ${expected}`)).toBeInTheDocument()
  })

  it('shows a checking state while the mailbox list is still loading', () => {
    mockFetch(() => new Promise<Response>(() => {}))
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <EventStreamContext value={{ status: 'disconnected', source: null }}>
          <MailboxCard />
        </EventStreamContext>
      </QueryClientProvider>,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Checking…')
  })

  it('shows an error when re-authorizing fails', async () => {
    renderCard(() => status({ mailboxes: [mailbox({ status: 'reauth_required' })] }), {
      'POST /api/v1/mailboxes/oauth/start': () => jsonResponse({ detail: 'Google refused' }, 500),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Re-authorize' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Google refused')
  })

  it('changes status on the mailbox.status server event, without a reload', async () => {
    let current = status({ mailboxes: [mailbox()] })
    const { calls, source } = renderCard(() => current)
    await waitFor(() => expect(calls).toHaveLength(1))
    expect(await screen.findByText('connected')).toBeInTheDocument()

    current = status({
      mailboxes: [mailbox({ status: 'reauth_required', status_reason: 'invalid_grant' })],
    })
    act(() => {
      source.emit('mailbox.status', {
        mailbox_id: 3,
        status: 'reauth_required',
        reason: 'invalid_grant',
      })
    })
    expect(await screen.findByText('needs re-authorizing')).toBeInTheDocument()
  })
})
