/**
 * The LinkedIn queue and what waits for you (#383). Everyone here is invented;
 * nothing reaches LinkedIn or the backend: every request goes to a stand-in.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { EventStreamContext } from '@/features/events/event-stream-context'
import {
  STATUS_CLEAR,
  backend,
  run,
  type Call,
  type Handler,
} from '@/features/linkedin/test-support'
import { FakeEventSource, resetFakeEventSource } from '@/test/fake-event-source'
import { jsonResponse, mockFetch } from '@/test/fetch'

import type { ReadyItem, ReadyPage, WaitingItem, WaitingPage } from './api'
import { ONE_AT_A_TIME, REVIEW_AND_SEND, TYPING, ageText, threadUrl } from './format'
import { LinkedInStepsSection } from './linkedin-steps-section'

const HOUR = 3_600_000

function hoursAgo(hours: number): string {
  return new Date(Date.now() - hours * HOUR).toISOString()
}

function readyItem(overrides: Partial<ReadyItem> = {}): ReadyItem {
  return {
    enrollment_id: 31,
    campaign_id: 5,
    campaign_name: 'Autumn reconnect',
    step_position: 1,
    contact_id: 41,
    contact_name: 'Rosalind Quillfeather',
    due: hoursAgo(1),
    held_until: null,
    ...overrides,
  }
}

function readyPage(items: ReadyItem[]): ReadyPage {
  return { items, total: items.length, by_step: {} }
}

function waitingItem(overrides: Partial<WaitingItem> = {}): WaitingItem {
  return {
    message_id: 71,
    status: 'prefilled',
    interrupted: false,
    enrollment_id: 32,
    campaign_id: 5,
    campaign_name: 'Autumn reconnect',
    contact_id: 42,
    contact_name: 'Tobias Marrowbone',
    prefilled_at: hoursAgo(2),
    ...overrides,
  }
}

function waitingPage(items: WaitingItem[]): WaitingPage {
  return { items, total: items.length }
}

const TWO_READY = readyPage([
  readyItem(),
  readyItem({ enrollment_id: 33, contact_id: 43, contact_name: 'Ada Pemberton' }),
])

function handlers(overrides: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    'GET /api/v1/linkedin/status': () => jsonResponse(STATUS_CLEAR),
    'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: false }),
    'GET /api/v1/campaigns/linkedin/ready': () => jsonResponse(TWO_READY),
    'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([])),
    ...overrides,
  }
}

function renderSection(over: Record<string, Handler> = {}, campaignId?: number) {
  const calls: Call[] = []
  mockFetch(backend(handlers(over), calls))
  const source = new FakeEventSource('/api/v1/events')
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({ component: () => <Outlet /> })
  const router = createRouter({
    routeTree: root.addChildren([
      createRoute({
        getParentRoute: () => root,
        path: '/',
        component: () => <LinkedInStepsSection campaignId={campaignId} />,
      }),
    ]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  })
  render(
    <EventStreamContext value={{ status: 'connected', source: source as unknown as EventSource }}>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </EventStreamContext>,
  )
  return { calls, source }
}

function posts(calls: Call[], path: string): Call[] {
  return calls.filter((call) => call.method === 'POST' && call.path === path)
}

const PREFILL = '/api/v1/campaigns/linkedin/prefill'

afterEach(() => {
  resetFakeEventSource()
})

describe('the LinkedIn queue', () => {
  it('lists due steps with a Prefill button each, and prefills nothing on its own', async () => {
    const { calls } = renderSection()

    const list = await screen.findByRole('list', { name: 'Ready to prefill' })
    const rows = within(list).getAllByRole('listitem')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('Rosalind Quillfeather')
    expect(rows[0]).toHaveTextContent('Autumn reconnect')
    expect(rows[0]).toHaveTextContent('step 1')
    expect(
      within(rows[0]!).getByRole('button', { name: 'Prefill Rosalind Quillfeather' }),
    ).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Prefill next' })).toBeEnabled()
    expect(screen.getByText('Auto-send off')).toBeVisible()
    expect(posts(calls, PREFILL)).toEqual([])
  })

  it('starts the prefill a person clicks, then says it is typing in Chrome with live progress', async () => {
    let status = STATUS_CLEAR
    const { calls, source } = renderSection({
      'GET /api/v1/linkedin/status': () => jsonResponse(status),
      [`POST ${PREFILL}`]: () => {
        status = { ...STATUS_CLEAR, running_run_id: 81 }
        return jsonResponse({ enrollment_id: 31, message_id: 71, run_id: 81, task_id: 't' }, 202)
      },
      'GET /api/v1/linkedin/runs/81': () =>
        jsonResponse(run({ id: 81, kind: 'message_send', status: 'running' })),
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))

    expect(await screen.findByText(TYPING)).toBeVisible()
    expect(posts(calls, PREFILL).map((call) => call.body)).toEqual([
      { enrollment_id: 31, next: false },
    ])
    // One at a time: every Prefill button is off while it types.
    for (const button of screen.getAllByRole('button', { name: /^Prefill/ })) {
      expect(button).toBeDisabled()
    }
    act(() => source.emit('run.progress', { run_id: 81, phase: 'typing', typed: 40 }))
    expect(await screen.findByText(/phase: typing/)).toBeVisible()
  })

  it('prefills the oldest with Prefill next', async () => {
    const { calls } = renderSection({
      [`POST ${PREFILL}`]: () =>
        jsonResponse({ enrollment_id: 31, message_id: 71, run_id: 81, task_id: 't' }, 202),
      'GET /api/v1/linkedin/runs/81': () =>
        jsonResponse(run({ id: 81, kind: 'message_send', status: 'running' })),
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Prefill next' }))

    await waitFor(() => expect(posts(calls, PREFILL).map((c) => c.body)).toEqual([{ next: true }]))
  })

  it('shows a refusal plainly, says nothing was typed, and offers no retry', async () => {
    renderSection({
      [`POST ${PREFILL}`]: () =>
        jsonResponse(
          {
            detail: {
              enrollment_id: 31,
              reasons: ['run_refused'],
              detail: 'no runner exists for message_send runs yet',
            },
          },
          409,
        ),
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))

    expect(await screen.findByText('Not prefilled. Nothing was typed in Chrome.')).toBeVisible()
    expect(screen.getByText(/netkeeper can't run a prefill yet/)).toBeVisible()
    expect(screen.getByText('no runner exists for message_send runs yet')).toBeVisible()
    expect(screen.queryByText(TYPING)).toBeNull()
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
    // Still nothing open: the buttons stay on for a person to try another.
    expect(screen.getByRole('button', { name: 'Prefill Ada Pemberton' })).toBeEnabled()
  })

  it('a refusal is an alert', async () => {
    renderSection({
      [`POST ${PREFILL}`]: () =>
        jsonResponse(
          { detail: { enrollment_id: 31, reasons: ['prefill_open'], detail: null } },
          409,
        ),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Not prefilled. Nothing was typed in Chrome.',
    )
  })

  it('never says nothing was typed when the request itself failed (a 5xx)', async () => {
    renderSection({
      [`POST ${PREFILL}`]: () => jsonResponse({ detail: 'internal error' }, 500),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(
      'The prefill request failed. Check Waiting for you and the LinkedIn page before you try again.',
    )
    expect(alert).not.toHaveTextContent('Nothing was typed')
  })

  it('never says nothing was typed when the network failed', async () => {
    renderSection({
      [`POST ${PREFILL}`]: () => {
        throw new TypeError('Failed to fetch')
      },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The prefill request failed.')
    expect(alert).not.toHaveTextContent('Nothing was typed')
  })

  it('turns every Prefill button off while the prefill request is pending', async () => {
    const { calls } = renderSection({
      [`POST ${PREFILL}`]: () => new Promise<Response>(() => {}),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))

    await waitFor(() => {
      for (const button of screen.getAllByRole('button', { name: /^Prefill/ })) {
        expect(button).toBeDisabled()
      }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Prefill Ada Pemberton' }))
    expect(posts(calls, PREFILL)).toHaveLength(1)
  })

  it('keeps Prefill off when what waits for you cannot be read', async () => {
    const { calls } = renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse({ detail: 'boom' }, 500),
    })
    expect(await screen.findByText('What waits for you is unavailable.')).toBeVisible()
    for (const button of screen.getAllByRole('button', { name: /^Prefill/ })) {
      expect(button).toBeDisabled()
    }
    fireEvent.click(screen.getByRole('button', { name: 'Prefill Rosalind Quillfeather' }))
    expect(posts(calls, PREFILL)).toEqual([])
  })

  it('allows one prefill at a time: an open one turns every Prefill button off', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
    })

    expect(await screen.findByText(ONE_AT_A_TIME)).toBeVisible()
    await screen.findByRole('list', { name: 'Ready to prefill' })
    for (const button of screen.getAllByRole('button', { name: /^Prefill/ })) {
      expect(button).toBeDisabled()
    }
  })

  it('a stale message does not hold the slot', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () =>
        jsonResponse(waitingPage([waitingItem({ status: 'stale', prefilled_at: hoursAgo(80) })])),
    })

    await screen.findByRole('list', { name: 'Waiting for you' })
    expect(screen.queryByText(ONE_AT_A_TIME)).toBeNull()
    expect(screen.getByRole('button', { name: 'Prefill Rosalind Quillfeather' })).toBeEnabled()
  })

  it('says how a prefill run stopped, with no retry', async () => {
    let status = STATUS_CLEAR
    let current = run({ id: 81, kind: 'message_send', status: 'running' })
    const { source } = renderSection({
      'GET /api/v1/linkedin/status': () => jsonResponse(status),
      [`POST ${PREFILL}`]: () => {
        status = { ...STATUS_CLEAR, running_run_id: 81 }
        return jsonResponse({ enrollment_id: 31, message_id: 71, run_id: 81, task_id: 't' }, 202)
      },
      'GET /api/v1/linkedin/runs/81': () => jsonResponse(current),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))
    await screen.findByText(TYPING)

    status = STATUS_CLEAR
    current = run({
      id: 81,
      kind: 'message_send',
      status: 'failed',
      stop_reason: 'composer_not_found',
      stop_reason_text: 'the message box did not open',
    })
    act(() => source.emit('run.finished', { run_id: 81, status: 'failed' }))

    expect(await screen.findByText('The prefill stopped.')).toBeVisible()
    expect(screen.getByText('the message box did not open.')).toBeVisible()
    expect(screen.getByText(/clear the composer in Chrome yourself/)).toBeVisible()
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
  })

  it("keeps a campaign's queue to that campaign, without Prefill next", async () => {
    const { calls } = renderSection({}, 5)

    await screen.findByRole('list', { name: 'Ready to prefill' })
    const ready = calls.find((call) => call.path === '/api/v1/campaigns/linkedin/ready')
    expect(ready?.query.get('campaign_id')).toBe('5')
    expect(screen.queryByRole('button', { name: 'Prefill next' })).toBeNull()
  })

  it('shows auto-send on as a highlighted note, not an alert, when the flag is on', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ auto_send: true }),
    })
    const badge = await screen.findByText('Auto-send on')
    expect(badge.closest('[role="note"]')).not.toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('says auto-send is unknown when the setting cannot be read', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/options': () => jsonResponse({ detail: 'boom' }, 500),
    })
    expect(await screen.findByText('Auto-send: unknown')).toBeVisible()
    expect(screen.queryByText('Auto-send off')).toBeNull()
  })
})

describe('waiting for you', () => {
  it('asks you to send a prefilled message yourself, with its age and both actions', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
    })

    const row = await screen.findByRole('listitem', { name: 'Tobias Marrowbone, prefilled' })
    expect(row).toHaveTextContent(REVIEW_AND_SEND)
    expect(row).toHaveTextContent('Prefilled 2 hours ago')
    expect(within(row).getByRole('button', { name: 'I sent it, check now' })).toBeEnabled()
    expect(within(row).getByRole('button', { name: 'Discard' })).toBeEnabled()
    expect(row).not.toHaveAttribute('data-state', 'stale')
  })

  it('highlights a stale message', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () =>
        jsonResponse(waitingPage([waitingItem({ status: 'stale', prefilled_at: hoursAgo(80) })])),
    })

    const row = await screen.findByRole('listitem', { name: 'Tobias Marrowbone, stale' })
    expect(row).toHaveAttribute('data-state', 'stale')
    expect(row.className).toMatch(/amber/)
    expect(within(row).getByText('Stale')).toBeVisible()
    expect(row).toHaveTextContent('Prefilled 3 days ago')
  })

  it('treats a prefill over three days old as stale before the tick marks it', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () =>
        jsonResponse(waitingPage([waitingItem({ prefilled_at: hoursAgo(73) })])),
    })
    expect(await screen.findByRole('listitem', { name: 'Tobias Marrowbone, stale' })).toBeVisible()
  })

  it('offers only Discard for a prefill that stopped before it said what it typed', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () =>
        jsonResponse(
          waitingPage([
            waitingItem({ status: 'scheduled', interrupted: true, prefilled_at: null }),
          ]),
        ),
    })

    const row = await screen.findByRole('listitem', { name: 'Tobias Marrowbone, interrupted' })
    expect(row).toHaveTextContent(/clear it, then discard this/)
    expect(within(row).queryByRole('button', { name: 'I sent it, check now' })).toBeNull()
    expect(screen.getByText(ONE_AT_A_TIME)).toBeVisible()
  })

  it('"I sent it, check now" asks for an inbox poll and says what it does', async () => {
    const { calls } = renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
      'POST /api/v1/campaigns/linkedin/messages/71/check': () =>
        jsonResponse({ run_id: 91, task_id: 't' }, 202),
      'GET /api/v1/linkedin/runs/91': () =>
        jsonResponse(run({ id: 91, kind: 'inbox', status: 'running' })),
    })

    fireEvent.click(await screen.findByRole('button', { name: 'I sent it, check now' }))

    expect(await screen.findByText(/Checking your LinkedIn inbox/)).toBeVisible()
    expect(posts(calls, '/api/v1/campaigns/linkedin/messages/71/check')).toHaveLength(1)
  })

  it('shows a refused check', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
      'POST /api/v1/campaigns/linkedin/messages/71/check': () =>
        jsonResponse({ detail: 'outside active hours' }, 409),
    })

    fireEvent.click(await screen.findByRole('button', { name: 'I sent it, check now' }))

    expect(await screen.findByText('The inbox check did not start.')).toBeVisible()
    expect(screen.getByText('outside active hours')).toBeVisible()
  })

  it('discards only after you confirm, and refreshes the lists', async () => {
    let waiting = waitingPage([waitingItem()])
    const { calls } = renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waiting),
      'POST /api/v1/campaigns/linkedin/messages/71/discard': () => {
        waiting = waitingPage([])
        return jsonResponse({
          message_id: 71,
          status: 'discarded',
          enrollment_id: 32,
          enrollment_status: 'completed',
        })
      },
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Discard' }))
    expect(posts(calls, '/api/v1/campaigns/linkedin/messages/71/discard')).toEqual([])
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('The step counts as fired')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Discard' }))

    expect(await screen.findByText('Nothing waits for you.')).toBeVisible()
    expect(posts(calls, '/api/v1/campaigns/linkedin/messages/71/discard')).toHaveLength(1)
    // The slot is free again.
    expect(screen.queryByText(ONE_AT_A_TIME)).toBeNull()
  })

  it("asks the server for one campaign's waiting list, and the queue checks every campaign's", async () => {
    const { calls } = renderSection(
      {
        'GET /api/v1/campaigns/linkedin/waiting': (call) =>
          jsonResponse(
            waitingPage(
              call.query.get('campaign_id') === '5'
                ? []
                : [waitingItem({ campaign_id: 6, contact_name: 'Other Person' })],
            ),
          ),
      },
      5,
    )
    expect(await screen.findByText('Nothing waits for you.')).toBeVisible()
    const waiting = calls.filter((c) => c.path === '/api/v1/campaigns/linkedin/waiting')
    expect(new Set(waiting.map((c) => c.query.get('campaign_id')))).toEqual(new Set(['5', null]))
    // An open prefill in another campaign still holds the one slot.
    expect(await screen.findByText(ONE_AT_A_TIME)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Prefill Rosalind Quillfeather' })).toBeDisabled()
  })

  it('keeps "I sent it, check now" off while the check it started runs', async () => {
    let checkRun = run({ id: 91, kind: 'inbox', status: 'running' })
    const { calls, source } = renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
      'POST /api/v1/campaigns/linkedin/messages/71/check': () =>
        jsonResponse({ run_id: 91, task_id: 't' }, 202),
      'GET /api/v1/linkedin/runs/91': () => jsonResponse(checkRun),
    })

    const button = await screen.findByRole('button', { name: 'I sent it, check now' })
    fireEvent.click(button)
    await screen.findByText(/Checking your LinkedIn inbox/)
    expect(button).toBeDisabled()
    fireEvent.click(button)
    expect(posts(calls, '/api/v1/campaigns/linkedin/messages/71/check')).toHaveLength(1)

    checkRun = run({ id: 91, kind: 'inbox', status: 'completed' })
    act(() => source.emit('run.finished', { run_id: 91, status: 'completed' }))
    await waitFor(() => expect(button).toBeEnabled())
  })
})

describe('format', () => {
  it('builds the plain thread link from a stored conversation URN', () => {
    expect(threadUrl('urn:li:msg_conversation:2-INVENTED==')).toBe(
      'https://www.linkedin.com/messaging/thread/2-INVENTED%3D%3D/',
    )
    expect(threadUrl('urn:li:msg_conversation:(urn:li:fsd_profile:INVENTEDP,2-INVENTEDT)')).toBe(
      'https://www.linkedin.com/messaging/thread/2-INVENTEDT/',
    )
    expect(threadUrl(null)).toBeNull()
    expect(threadUrl('javascript:alert(1)')).toBeNull()
    expect(threadUrl('urn:li:member:123')).toBeNull()
  })

  it('says an age in plain words', () => {
    const now = new Date('2030-06-15T12:00:00Z')
    expect(ageText('2030-06-15T11:30:00Z', now)).toBe('30 minutes ago')
    expect(ageText('2030-06-15T09:00:00Z', now)).toBe('3 hours ago')
    expect(ageText('2030-06-12T12:00:00Z', now)).toBe('3 days ago')
    expect(ageText(null, now)).toBe('not recorded')
  })
})
