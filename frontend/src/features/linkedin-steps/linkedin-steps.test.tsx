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

import type { ReadyItem, ReadyPage, TryAgainItem, WaitingItem, WaitingPage } from './api'
import { ONE_AT_A_TIME, REVIEW_AND_SEND, ageText, reasonText, threadUrl } from './format'
import { LinkedInStepsSection } from './linkedin-steps-section'
import reasons from './prefill-reasons.json'
import {
  CLOSE_BUBBLE,
  FIRST_POLL_NOTE,
  MAYBE_CLOSE_BUBBLE,
  DRAFT_IN_BUBBLE,
  PARTLY_TYPED,
  PREFILL_NOTE,
  TYPED_WHOLE,
  CONFIRM_BUBBLE_BODY,
  CONFIRM_BUBBLE_LABEL,
  TRY_AGAIN_NOTE,
  TRY_AGAIN_STEP,
  TYPING,
  TYPING_WARNING,
  budgetText,
  confirmBubbleTitle,
  prefillEnding,
  prefillReason,
  prefillsLeftText,
} from './prefill-copy'

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

function readyPage(items: ReadyItem[], tryAgain: TryAgainItem[] = []): ReadyPage {
  return { items, total: items.length, by_step: {}, try_again: tryAgain, prefills_left_today: 7 }
}

function tryAgainItem(
  overrides: Partial<TryAgainItem> = {},
  last: Partial<TryAgainItem['last_try']> = {},
): TryAgainItem {
  return {
    enrollment_id: 34,
    campaign_id: 5,
    campaign_name: 'Autumn reconnect',
    step_position: 1,
    contact_id: 44,
    contact_name: 'Wilhelmina Thorne',
    held_until: null,
    ...overrides,
    last_try: {
      reason: 'the Message control could not be clicked',
      tries: 1,
      run_id: 80,
      at: hoursAgo(1),
      click_attempted: true,
      budget_spent: true,
      counted_today: true,
      needs_confirmation: true,
      ...last,
    },
  }
}

function waitingItem(overrides: Partial<WaitingItem> = {}): WaitingItem {
  return {
    message_id: 71,
    status: 'prefilled',
    interrupted: false,
    partly_typed: false,
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
      { enrollment_id: 31, next: false, retry: false, no_bubble_open: false },
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

    await waitFor(() =>
      expect(posts(calls, PREFILL).map((c) => c.body)).toEqual([
        { next: true, retry: false, no_bubble_open: false },
      ]),
    )
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
    expect(screen.getByText(/netkeeper couldn't start the prefill run/)).toBeVisible()
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

  it('frees "I sent it, check now" when the check run cannot be read', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
      'POST /api/v1/campaigns/linkedin/messages/71/check': () =>
        jsonResponse({ run_id: 91, task_id: 't' }, 202),
      'GET /api/v1/linkedin/runs/91': () => jsonResponse({ detail: 'boom' }, 500),
    })

    const button = await screen.findByRole('button', { name: 'I sent it, check now' })
    fireEvent.click(button)

    await screen.findByText(/Checking your LinkedIn inbox/) // the check started
    await waitFor(() => expect(button).toBeEnabled())
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

/** Clicks Prefill for Rosalind, then ends the run the way a prefill run records it. */
async function finishPrefill(stop_reason: string, error: string | null, status = 'aborted') {
  let state = STATUS_CLEAR
  let current = run({ id: 81, kind: 'message_send', status: 'running' })
  const { source } = renderSection({
    'GET /api/v1/linkedin/status': () => jsonResponse(state),
    [`POST ${PREFILL}`]: () => {
      state = { ...STATUS_CLEAR, running_run_id: 81 }
      return jsonResponse({ enrollment_id: 31, message_id: 71, run_id: 81, task_id: 't' }, 202)
    },
    'GET /api/v1/linkedin/runs/81': () => jsonResponse(current),
  })
  fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))
  await screen.findByText(TYPING)
  state = STATUS_CLEAR
  current = run({
    id: 81,
    kind: 'message_send',
    status: status as 'aborted',
    stop_reason,
    stop_reason_text: stop_reason,
    error,
  })
  act(() => source.emit('run.finished', { run_id: 81, status }))
  return screen.findByRole('alert')
}

describe('the prefill notes (#383, ADR 0007)', () => {
  it('tells you before the click that the recipient may see "typing…"', async () => {
    renderSection()
    await screen.findByRole('list', { name: 'Ready to prefill' })
    expect(screen.getByText(PREFILL_NOTE)).toBeVisible()
    expect(PREFILL_NOTE).toContain('may see "typing…"')
    expect(PREFILL_NOTE).toContain("Don't type or click in Chrome")
  })

  it('tells you again while it types, and asks you to leave Chrome alone', async () => {
    renderSection({
      [`POST ${PREFILL}`]: () =>
        jsonResponse({ enrollment_id: 31, message_id: 71, run_id: 81, task_id: 't' }, 202),
      'GET /api/v1/linkedin/runs/81': () =>
        jsonResponse(run({ id: 81, kind: 'message_send', status: 'running' })),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))

    expect(await screen.findByText(TYPING_WARNING)).toBeVisible()
    expect(TYPING_WARNING).toContain('"typing…"')
    expect(screen.queryByText(PREFILL_NOTE)).toBeNull()
  })

  it('says the inbox is held before the first poll, and to run it by hand', async () => {
    renderSection({
      'GET /api/v1/poll-status': () =>
        jsonResponse({
          items: [
            {
              key: 'linkedin_inbox',
              group: 'linkedin',
              label: 'LinkedIn inbox',
              state: 'blocked',
              interval: 'PT2H',
              last_at: null,
              next_at: null,
              reason: 'The first LinkedIn inbox poll is run by hand',
              requested: false,
            },
          ],
          mailboxes: [],
        }),
    })

    const note = await screen.findByRole('note')
    expect(note).toHaveTextContent('held until it has')
    expect(note).toHaveTextContent('Run netkeeper linkedin inbox by hand')
    expect(within(note).getByText('netkeeper linkedin inbox').tagName).toBe('CODE')
    expect(note).not.toHaveTextContent('`')
    expect(note).toHaveTextContent(FIRST_POLL_NOTE)
  })

  it('shows no first-poll note once a poll has completed', async () => {
    renderSection({
      'GET /api/v1/poll-status': () =>
        jsonResponse({
          items: [
            {
              key: 'linkedin_inbox',
              group: 'linkedin',
              label: 'LinkedIn inbox',
              state: 'scheduled',
              interval: 'PT2H',
              last_at: hoursAgo(1),
              next_at: null,
              reason: null,
              requested: false,
            },
          ],
          mailboxes: [],
        }),
    })
    await screen.findByRole('list', { name: 'Ready to prefill' })
    expect(screen.queryByRole('note', { name: /./ })).toBeNull()
    expect(screen.queryByText(/has not read your LinkedIn inbox yet/)).toBeNull()
  })

  it('shows the hold as a refusal: held until the inbox is read, with the backend detail', async () => {
    renderSection({
      [`POST ${PREFILL}`]: () =>
        jsonResponse(
          {
            detail: {
              enrollment_id: 31,
              reasons: ['linkedin_inbox_stale'],
              detail:
                'the LinkedIn inbox has not been read: no poll has completed yet; run `netkeeper linkedin inbox` by hand',
            },
          },
          409,
        ),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Not prefilled. Nothing was typed in Chrome.')
    expect(alert).toHaveTextContent('held until the inbox is read')
    expect(alert).toHaveTextContent('run `netkeeper linkedin inbox` by hand')
    expect(reasonText('linkedin_inbox_stale')).toMatch(/^held until the inbox is read/)
  })
})

describe('how a prefill that typed nothing ended', () => {
  it('after a refusal that followed the click, tells you to close the empty bubble', async () => {
    const alert = await finishPrefill('not_typed', 'recipient_name_mismatch')

    expect(alert).toHaveTextContent('Not prefilled. Nothing was typed in Chrome.')
    expect(alert).toHaveTextContent(
      "The name in the message bubble doesn't match the name on the profile.",
    )
    expect(alert).toHaveTextContent(CLOSE_BUBBLE)
    expect(alert).not.toHaveTextContent('recipient_name_mismatch')
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
  })

  it('does not ask you to close a bubble when the click never happened', async () => {
    const alert = await finishPrefill('not_typed', 'the contact has no public profile id to open')

    expect(alert).toHaveTextContent('no public LinkedIn profile address')
    expect(alert).not.toHaveTextContent('bubble')
  })

  it('says a refusal the run cannot place may have left a bubble open', async () => {
    const alert = await finishPrefill('not_typed', 'cancelled')
    expect(alert).toHaveTextContent('You cancelled the run.')
    expect(alert).toHaveTextContent(MAYBE_CLOSE_BUBBLE)
  })

  it('says a body that is too long typed nothing and parked the enrollment', async () => {
    const alert = await finishPrefill('too_long', 'the body is over the typing ceiling')
    expect(alert).toHaveTextContent('Nothing was typed in Chrome.')
    expect(alert).toHaveTextContent('too long to type')
    expect(alert).toHaveTextContent('parked the enrollment')
    expect(alert).not.toHaveTextContent('bubble')
  })

  it('shows a phrase it has no words for exactly as the backend wrote it', async () => {
    const alert = await finishPrefill('not_typed', 'a phrase from a newer netkeeper')
    expect(alert).toHaveTextContent('a phrase from a newer netkeeper')
    expect(alert).toHaveTextContent(MAYBE_CLOSE_BUBBLE)
  })
})

describe('the click recorded in the run (S3)', () => {
  async function finishWith(counts: Record<string, unknown> | null, error: string) {
    let state = STATUS_CLEAR
    let current = run({ id: 81, kind: 'message_send', status: 'running' })
    const { source } = renderSection({
      'GET /api/v1/linkedin/status': () => jsonResponse(state),
      [`POST ${PREFILL}`]: () => {
        state = { ...STATUS_CLEAR, running_run_id: 81 }
        return jsonResponse({ enrollment_id: 31, message_id: 71, run_id: 81, task_id: 't' }, 202)
      },
      'GET /api/v1/linkedin/runs/81': () => jsonResponse(current),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))
    await screen.findByText(TYPING)
    state = STATUS_CLEAR
    current = run({
      id: 81,
      kind: 'message_send',
      status: 'aborted',
      stop_reason: 'not_typed',
      stop_reason_text: 'not_typed',
      error,
      counts,
    })
    act(() => source.emit('run.finished', { run_id: 81, status: 'aborted' }))
    return screen.findByRole('alert')
  }

  it('says a click that landed left a bubble open, whatever the phrase says', async () => {
    const alert = await finishWith(
      { message_click_attempted: true, message_clicked: true },
      'cancelled',
    )
    expect(alert).toHaveTextContent(CLOSE_BUBBLE)
  })

  it('says no bubble when no click was sent, even for a phrase that could mean one', async () => {
    const alert = await finishWith(
      { message_click_attempted: false, message_clicked: false },
      'the Message control could not be clicked',
    )
    expect(alert).not.toHaveTextContent('bubble')
  })

  it('says a bubble may be open when the click was sent but did not land', async () => {
    const alert = await finishWith(
      { message_click_attempted: true, message_clicked: false },
      'the profile opened somewhere else',
    )
    expect(alert).toHaveTextContent(MAYBE_CLOSE_BUBBLE)
  })

  it('falls back to the phrase table for an older run with no click counts', async () => {
    const alert = await finishWith({ typed_chars: 0 }, 'recipient_name_mismatch')
    expect(alert).toHaveTextContent(CLOSE_BUBBLE)
  })

  it('does not say the bubble is empty when it already held text, and gives that its own step', async () => {
    const alert = await finishWith(
      { message_click_attempted: true, message_clicked: true },
      'the composer is not empty',
    )
    expect(alert).toHaveTextContent(DRAFT_IN_BUBBLE)
    expect(alert).not.toHaveTextContent('It is empty')
    expect(alert).not.toHaveTextContent(CLOSE_BUBBLE)
  })
})

describe('a prefill that typed the whole message', () => {
  it('says plainly that it was typed', async () => {
    let state = STATUS_CLEAR
    let current = run({ id: 81, kind: 'message_send', status: 'running' })
    const { source } = renderSection({
      'GET /api/v1/linkedin/status': () => jsonResponse(state),
      [`POST ${PREFILL}`]: () => {
        state = { ...STATUS_CLEAR, running_run_id: 81 }
        return jsonResponse({ enrollment_id: 31, message_id: 71, run_id: 81, task_id: 't' }, 202)
      },
      'GET /api/v1/linkedin/runs/81': () => jsonResponse(current),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Prefill Rosalind Quillfeather' }))
    await screen.findByText(TYPING)
    state = STATUS_CLEAR
    current = run({ id: 81, kind: 'message_send', status: 'completed', stop_reason: 'prefilled' })
    act(() => source.emit('run.finished', { run_id: 81, status: 'completed' }))
    expect(await screen.findByText(TYPED_WHOLE)).toBeVisible()
    expect(TYPED_WHOLE).toMatch(/typed the message/)
  })
})

describe('a partly typed prefill in Waiting for you (B1)', () => {
  const PARTLY = waitingItem({
    status: 'failed',
    partly_typed: true,
    prefilled_at: null,
    message_id: 72,
  })

  it('shows a red row from the API, so it survives a reload, with only Discard', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([PARTLY])),
    })

    const row = await screen.findByRole('listitem', { name: /Tobias Marrowbone, partly_typed/ })
    expect(row).toHaveAttribute('data-state', 'partly_typed')
    expect(row.className).toMatch(/destructive/)
    expect(row).toHaveTextContent(PARTLY_TYPED)
    expect(row).toHaveTextContent("Don't click Send")
    expect(row).toHaveTextContent('If you finished and sent it yourself, discard this.')
    expect(within(row).queryByRole('button', { name: 'I sent it, check now' })).toBeNull()
    expect(within(row).getByRole('button', { name: 'I cleared it, discard' })).toBeVisible()
    expect(within(row).getAllByRole('button')).toHaveLength(1)
  })

  it('holds the one-prefill slot: every Prefill button is off', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([PARTLY])),
    })
    expect(await screen.findByText(ONE_AT_A_TIME)).toBeVisible()
    for (const button of screen.getAllByRole('button', { name: /^Prefill/ })) {
      expect(button).toBeDisabled()
    }
  })

  it('discards after you confirm that you cleared it', async () => {
    let waiting = waitingPage([PARTLY])
    const { calls } = renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waiting),
      'POST /api/v1/campaigns/linkedin/messages/72/discard': () => {
        waiting = waitingPage([])
        return jsonResponse({ message_id: 72, status: 'discarded' })
      },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'I cleared it, discard' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'I cleared it, discard' }))

    await waitFor(() =>
      expect(posts(calls, '/api/v1/campaigns/linkedin/messages/72/discard')).toHaveLength(1),
    )
    expect(await screen.findByText('Nothing waits for you.')).toBeVisible()
  })
})

describe('every recorded phrase has words (S1 drift guard)', () => {
  // prefill-reasons.json is checked against the backend's source by
  // tests/test_prefill_reason_phrases.py.
  it.each(reasons)('%s', (phrase) => {
    const sample = phrase.replace(
      '{}',
      phrase.startsWith('after typing') ? 'the composer does not hold focus' : 'x',
    )
    const words = prefillReason(sample)
    expect(words.text).not.toBe(sample)
    expect(words.text).toMatch(/^(netkeeper |[A-Z])/)
    expect(words.text).not.toMatch(/_/)
  })

  it('places each click-stage refusal', () => {
    for (const phrase of [
      "the tab is not on the contact's profile",
      'the tab left the profile before the click',
      'the Message control could not be read',
      'no Message control is visible',
      'no Message control is on screen with nothing over it; close or move what covers it',
      'whether a message bubble is open could not be read',
      'whether a message bubble is open could not be read',
      'a message bubble is already open in Chrome, minimized ones included; close it, then try again',
    ]) {
      expect(prefillReason(phrase).bubble).toBe('closed')
    }
    expect(prefillReason('the Message control could not be clicked').bubble).toBe('maybe')
    expect(prefillReason('the Message control was already clicked').bubble).toBe('maybe')
  })

  it('has words for the engine reasons that had none', () => {
    for (const reason of ['disconnected', 'campaign_at_cap', 'campaign_blocked']) {
      expect(reasonText(reason)).not.toBe(reason.replace(/_/g, ' '))
    }
  })
})

describe('how a prefill that typed part of the message ended', () => {
  it.each([
    ['partially_typed', 'The prefill stopped part of the way through typing.'],
    ['unknown', "netkeeper can't tell how much it typed"],
  ])('%s says part of the message may be in the composer', async (outcome, title) => {
    const alert = await finishPrefill(outcome, 'the composer does not hold focus', 'failed')

    expect(alert).toHaveTextContent(title)
    expect(alert).toHaveTextContent('The message box lost focus, so netkeeper stopped.')
    expect(alert).toHaveTextContent(PARTLY_TYPED)
    expect(alert).toHaveTextContent('never clears the composer and never retries')
    expect(alert).not.toHaveTextContent('Nothing was typed')
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
  })

  it('says it checked after typing when the final check failed', async () => {
    const alert = await finishPrefill(
      'partially_typed',
      "after typing: the composer's text changed",
      'failed',
    )
    expect(alert).toHaveTextContent(
      'After typing, the text in the message box changed while netkeeper typed.',
    )
  })
})

describe('prefill refusal reasons in plain words', () => {
  // Every phrase the prefill records (browser.py, page_messaging.py, messaging.py,
  // message_send.py): none reaches the person raw.
  const PHRASES: readonly string[] = [
    'no claim',
    'the claim lapsed',
    'the message has no body',
    'the contact has no usable LinkedIn URN',
    'the typing plan refused the body (MultilineRefused)',
    'the body is over the typing ceiling',
    'the LinkedIn session is flagged',
    'heat is too high',
    "today's LinkedIn budget is spent",
    'the contact has no public profile id to open',
    'the page answered logged_out',
    'the profile opened somewhere else',
    'no Message control on the page',
    "a Message control opens something other than this contact's compose",
    'the Message control was not clicked',
    'no compose option was loaded after the click',
    'the compose option could not be read',
    'more than one compose option was loaded',
    'the compose option was not answered',
    'the compose option came from another path',
    "the compose option's URN has an unexpected shape",
    'the compose option names another profile',
    "the compose option's answer could not be read",
    "the compose option's answer is not JSON",
    "the compose option's answer has an unexpected shape",
    'the compose option names another recipient, or more than one',
    'a reply compose option names no conversation',
    'a new-message compose option names a conversation',
    "the compose option's type is not one the prefill knows",
    'another_compose',
    'recipient_name_mismatch',
    'recipient_name_unreadable',
    'no message composer is on the page',
    'a message bubble is already open in Chrome, minimized ones included; close it, then try again',
    'another message composer is on the page, in an open or minimized bubble; close the other bubbles',
    'another message bubble is on the page, open or minimized; close the others',
    'no Message control is on screen with nothing over it; close or move what covers it',
    "the conversation's bubble is not open",
    'the page shows a new-message bubble, not the conversation',
    "the composer is not in the conversation's bubble",
    "the bubble's header does not name one person",
    'the bubble is for someone else',
    'the contact has no public profile id to check the new-message bubble by',
    'the new-message bubble is not open, or more than one is',
    "the page shows the conversation's bubble, not a new message",
    'the composer is not in the new-message bubble',
    'the new-message bubble does not name exactly one recipient',
    "the new-message bubble's recipient field is missing",
    'the new-message bubble is for someone else',
    "the tab's url changed",
    'the tab or the browser went away',
    'the composer is not empty',
    "the composer's text changed",
    'the composer does not hold focus',
    'the composer could not be read',
    'the bubble was not drawn',
    'a key call failed',
    'cancelled',
    'interrupted',
    'the prefill failed (RuntimeError)',
    'no tab with a clicked Message control',
    'the plan holds a control character',
    'the plan is empty',
  ]

  it.each(PHRASES)('says %j in plain words', (phrase) => {
    const reason = prefillReason(phrase)
    expect(reason.text).not.toBe(phrase)
    expect(reason.text).toMatch(/^(netkeeper |[A-Z])/)
    expect(reason.text).not.toMatch(/_/)
  })

  it('names each of the three codes the way the maintainer would say it', () => {
    expect(prefillReason('recipient_name_mismatch').text).toBe(
      "The name in the message bubble doesn't match the name on the profile.",
    )
    expect(prefillReason('recipient_name_unreadable').text).toBe(
      "netkeeper couldn't read the recipient's name in the message bubble, so it couldn't check it against the profile.",
    )
    expect(prefillReason('another_compose').text).toMatch(/second message composer/)
  })

  it('says only the refusals after the click leave a bubble open', () => {
    expect(prefillReason('the profile opened somewhere else').bubble).toBe('closed')
    expect(prefillReason('the claim lapsed').bubble).toBe('closed')
    expect(prefillReason('another_compose').bubble).toBe('open')
    expect(prefillReason("the tab's url changed").bubble).toBe('open')
    expect(prefillReason('cancelled').bubble).toBe('maybe')
  })

  it('has no ending for a prefill, or a stop that is not a prefill outcome', () => {
    expect(prefillEnding('prefilled', null)).toBeNull()
    expect(prefillEnding('session_flagged', null)).toBeNull()
    expect(prefillEnding(null, null)).toBeNull()
  })

  it('shows a prefill outcome in the runs list as words, not a code', async () => {
    const { stopReasonLabel } = await import('@/features/linkedin/fields')
    expect(
      stopReasonLabel({ stop_reason: 'partially_typed', stop_reason_text: 'partially_typed' }),
    ).toBe('part of the message was typed')
    expect(
      stopReasonLabel({ stop_reason: 'throttled', stop_reason_text: 'LinkedIn throttled it' }),
    ).toBe('LinkedIn throttled it')
  })
})

describe('the inbox check states (#433)', () => {
  it('says the first poll is run by hand when "I sent it, check now" is refused for it', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
      'POST /api/v1/campaigns/linkedin/messages/71/check': () =>
        jsonResponse({ run_id: 91, task_id: 't' }, 202),
      'GET /api/v1/linkedin/runs/91': () =>
        jsonResponse(
          run({
            id: 91,
            kind: 'inbox',
            status: 'failed',
            stop_reason: 'first_inbox_poll',
            stop_reason_text: 'refused: the first LinkedIn inbox poll is run by hand',
          }),
        ),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'I sent it, check now' }))
    expect(
      await screen.findByText(
        /The inbox check stopped: refused: the first LinkedIn inbox poll is run by hand/,
      ),
    ).toBeVisible()
  })

  it('says to run inbox-forget-owner when the check stops on another mailbox', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
      'POST /api/v1/campaigns/linkedin/messages/71/check': () =>
        jsonResponse({ run_id: 91, task_id: 't' }, 202),
      'GET /api/v1/linkedin/runs/91': () =>
        jsonResponse(
          run({
            id: 91,
            kind: 'inbox',
            status: 'failed',
            stop_reason: 'owner_mismatch',
            stop_reason_text:
              "the page showed another LinkedIn mailbox than this account's; if Chrome is signed in to another LinkedIn account, sign back in to yours; otherwise run `netkeeper linkedin inbox-forget-owner` (or, if your own contact has a LinkedIn ID, it does not match this mailbox and netkeeper cannot edit it yet)",
          }),
        ),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'I sent it, check now' }))
    expect(await screen.findByText(/netkeeper linkedin inbox-forget-owner/)).toBeVisible()
  })
})

describe('Try again (#445)', () => {
  const RETRY_ACCEPTED = () =>
    jsonResponse({ enrollment_id: 34, message_id: 72, run_id: 82, task_id: 't' }, 202)

  it('lists a step that typed nothing apart, with why, its tries and the budget', async () => {
    const { calls } = renderSection({
      'GET /api/v1/campaigns/linkedin/ready': () =>
        jsonResponse(readyPage([readyItem()], [tryAgainItem({}, { tries: 2 })])),
    })
    const list = await screen.findByRole('list', { name: 'Try again' })
    const row = within(list).getByRole('listitem')
    expect(row).toHaveTextContent('Wilhelmina Thorne')
    expect(row).toHaveTextContent("Chrome didn't take netkeeper's click on Message.")
    expect(row).toHaveTextContent('Tried 2 times in a row.')
    expect(row).toHaveTextContent("That try used one of today's LinkedIn prefills.")
    expect(screen.getByText(TRY_AGAIN_NOTE)).toBeVisible()
    expect(screen.getByText(prefillsLeftText(7))).toBeVisible()
    // Only Try again claims it: no Prefill button for it, and nothing posted on its own.
    expect(screen.queryByRole('button', { name: 'Prefill Wilhelmina Thorne' })).toBeNull()
    expect(
      within(row).getByRole('button', { name: 'Try again for Wilhelmina Thorne' }),
    ).toBeEnabled()
    expect(posts(calls, PREFILL)).toEqual([])
  })

  it('retries at once when the last try never clicked Message', async () => {
    const { calls } = renderSection({
      'GET /api/v1/campaigns/linkedin/ready': () =>
        jsonResponse(
          readyPage(
            [],
            [
              tryAgainItem(
                {},
                {
                  reason: 'the browser was busy',
                  click_attempted: null,
                  budget_spent: false,
                  counted_today: false,
                  needs_confirmation: false,
                },
              ),
            ],
          ),
        ),
      [`POST ${PREFILL}`]: RETRY_ACCEPTED,
      'GET /api/v1/linkedin/runs/82': () =>
        jsonResponse(run({ id: 82, kind: 'message_send', status: 'running' })),
    })
    const row = within(await screen.findByRole('list', { name: 'Try again' })).getByRole('listitem')
    expect(row).toHaveTextContent("didn't use one of today's LinkedIn prefills")
    fireEvent.click(within(row).getByRole('button', { name: 'Try again for Wilhelmina Thorne' }))
    await waitFor(() =>
      expect(posts(calls, PREFILL).map((c) => c.body)).toEqual([
        { enrollment_id: 34, next: false, retry: true, no_bubble_open: false },
      ]),
    )
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(await screen.findByText(TYPING)).toBeVisible()
  })

  it('asks before a retry whose last try clicked Message, and posts nothing until you confirm', async () => {
    const { calls } = renderSection({
      'GET /api/v1/campaigns/linkedin/ready': () => jsonResponse(readyPage([], [tryAgainItem()])),
      [`POST ${PREFILL}`]: RETRY_ACCEPTED,
      'GET /api/v1/linkedin/runs/82': () =>
        jsonResponse(run({ id: 82, kind: 'message_send', status: 'running' })),
    })
    const button = await screen.findByRole('button', { name: 'Try again for Wilhelmina Thorne' })

    fireEvent.click(button)
    let dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(confirmBubbleTitle('Wilhelmina Thorne'))
    expect(dialog).toHaveTextContent(CONFIRM_BUBBLE_BODY)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(posts(calls, PREFILL)).toEqual([])

    fireEvent.click(button)
    dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: CONFIRM_BUBBLE_LABEL }))
    await waitFor(() =>
      expect(posts(calls, PREFILL).map((c) => c.body)).toEqual([
        { enrollment_id: 34, next: false, retry: true, no_bubble_open: true },
      ]),
    )
  })

  it('keeps Try again off while another prefill is open, or the sending hours hold it', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/ready': () =>
        jsonResponse(
          readyPage(
            [],
            [
              tryAgainItem(),
              tryAgainItem({
                enrollment_id: 35,
                contact_id: 45,
                contact_name: 'Ada Pemberton',
                held_until: new Date(Date.now() + HOUR).toISOString(),
              }),
            ],
          ),
        ),
      'GET /api/v1/campaigns/linkedin/waiting': () => jsonResponse(waitingPage([waitingItem()])),
    })
    expect(
      await screen.findByRole('button', { name: 'Try again for Wilhelmina Thorne' }),
    ).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Try again for Ada Pemberton' })).toBeDisabled()
    expect(screen.getByText(ONE_AT_A_TIME)).toBeVisible()
  })

  it('shows a refused retry like any refusal', async () => {
    renderSection({
      'GET /api/v1/campaigns/linkedin/ready': () =>
        jsonResponse(readyPage([], [tryAgainItem({}, { needs_confirmation: false })])),
      [`POST ${PREFILL}`]: () =>
        jsonResponse(
          { detail: { enrollment_id: 34, reasons: ['browser_out_of_budget'], detail: null } },
          409,
        ),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Try again for Wilhelmina Thorne' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Not prefilled. Nothing was typed in Chrome.')
    expect(alert).toHaveTextContent("today's LinkedIn prefill budget is spent")
  })

  it('says in words whether the last try counted against today', () => {
    expect(budgetText({ budget_spent: true, counted_today: true })).toBe(
      "That try used one of today's LinkedIn prefills.",
    )
    expect(budgetText({ budget_spent: true, counted_today: false })).toMatch(/earlier day/)
    expect(budgetText({ budget_spent: false, counted_today: false })).toMatch(/didn't use/)
    expect(budgetText({ budget_spent: null, counted_today: null })).toMatch(/can't tell/)
    expect(prefillsLeftText(0)).toBe("Today's LinkedIn prefill budget is spent.")
    expect(prefillsLeftText(1)).toBe("1 of today's LinkedIn prefills is left.")
  })

  it('points a not_typed ending at Try again, and never a part-typed or too long one', () => {
    expect(prefillEnding('not_typed', 'the browser was busy')?.steps).toContain(TRY_AGAIN_STEP)
    for (const reason of ['partially_typed', 'unknown', 'too_long']) {
      expect(prefillEnding(reason, 'x')?.steps).not.toContain(TRY_AGAIN_STEP)
    }
  })

  it('says each new refusal in plain words', () => {
    for (const reason of ['try_again_needed', 'nothing_to_retry', 'confirm_no_bubble']) {
      expect(reasonText(reason)).not.toBe(reason.replace(/_/g, ' '))
    }
  })
})
