import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { EventStreamContext } from '@/features/events/event-stream-context'
import { FakeEventSource, resetFakeEventSource } from '@/test/fake-event-source'
import { jsonResponse, mockFetch } from '@/test/fetch'

import type { MailboxPoll, PollCheck, PollStatus } from './api'
import { pollStatusKeys, pollStatusQuery } from './api'
import {
  LATE_AFTER_MS,
  ago,
  checkSummary,
  everyText,
  nextText,
  repliesText,
  STATE_TEXT,
} from './format'
import { PollStatusHeader } from './poll-status-header'
import { RepliesChecked } from './replies-checked'

const NOW = Date.parse('2026-09-16T15:00:00Z')
const minutes = (n: number) => new Date(NOW + n * 60_000).toISOString()

function check(overrides: Partial<PollCheck> = {}): PollCheck {
  return {
    key: 'gmail_replies',
    group: 'gmail',
    label: 'Gmail replies',
    state: 'scheduled',
    interval_minutes: 10,
    last_at: minutes(-3),
    next_at: minutes(7),
    reason: null,
    requested: false,
    requested_at: null,
    ...overrides,
  }
}

function mailboxPoll(overrides: Partial<MailboxPoll> = {}): MailboxPoll {
  return {
    mailbox_id: 3,
    email: 'sender@example.com',
    armed: true,
    state: 'scheduled',
    replies_polled_at: minutes(-3),
    next_at: minutes(7),
    reason: null,
    ...overrides,
  }
}

function pollStatus(overrides: Partial<PollStatus> = {}): PollStatus {
  return {
    checked_at: minutes(0),
    background_running: true,
    items: [
      check(),
      check({
        key: 'gmail_drafts',
        label: 'Gmail drafts',
        state: 'idle',
        last_at: null,
        next_at: null,
        reason: 'No campaign draft is waiting to be sent',
      }),
      check({
        key: 'linkedin_inbox',
        group: 'linkedin',
        label: 'LinkedIn inbox',
        state: 'not_wired',
        interval_minutes: 180,
        last_at: null,
        next_at: null,
        reason: 'The LinkedIn inbox poll isn’t running yet',
      }),
      check({
        key: 'linkedin_enrich',
        group: 'linkedin',
        label: 'LinkedIn enrichment',
        state: 'paused',
        interval_minutes: 180,
        last_at: minutes(-120),
        next_at: null,
        reason: 'The LinkedIn schedule is paused',
      }),
    ],
    mailboxes: [mailboxPoll()],
    ...overrides,
  }
}

const CHECK_NOW = '/api/v1/poll-status/gmail-replies/check-now'

function renderWith(
  ui: React.ReactNode,
  current: () => PollStatus,
  checkNow: () => Response | Promise<Response> = () =>
    jsonResponse({ already_requested: false }, 202),
) {
  let fetches = 0
  mockFetch((request) => {
    const { pathname } = new URL(request.url)
    if (pathname === '/api/v1/poll-status') {
      fetches += 1
      return jsonResponse(current())
    }
    if (pathname === CHECK_NOW && request.method === 'POST') {
      return checkNow()
    }
    return jsonResponse({ detail: `unexpected ${pathname}` }, 500)
  })
  const source = new FakeEventSource('/api/v1/events')
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <EventStreamContext value={{ status: 'connected', source: source as unknown as EventSource }}>
        {ui}
      </EventStreamContext>
    </QueryClientProvider>,
  )
  return { source, fetches: () => fetches, queryClient }
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
})

afterEach(() => {
  vi.useRealTimers()
  resetFakeEventSource()
  vi.restoreAllMocks()
})

describe('format', () => {
  it('says how long ago', () => {
    expect(ago(minutes(0), NOW)).toBe('just now')
    expect(ago(minutes(-3), NOW)).toBe('3 min ago')
    expect(ago(minutes(-150), NOW)).toBe('2 h ago')
    expect(ago(minutes(-1440), NOW)).toBe('1 day ago')
    expect(ago(minutes(-3 * 1440), NOW)).toBe('3 days ago')
  })

  it('says when next: minutes within the hour, then a clock time', () => {
    expect(nextText(minutes(0.5), NOW)).toBe('in under a minute')
    expect(nextText(minutes(7), NOW)).toBe('in 7 min')
    const later = new Date(minutes(200))
    const time = later.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    expect(nextText(minutes(200), NOW)).toContain(time)
  })

  it('says each interval in its own unit', () => {
    expect(everyText(0)).toBe('every minute')
    expect(everyText(1)).toBe('every minute')
    expect(everyText(10)).toBe('every 10 min')
    expect(everyText(90)).toBe('every 90 min')
    expect(everyText(2880 + 60)).toBe('every 49 h')
    expect(everyText(180)).toBe('every 3 h')
    expect(everyText(1440)).toBe('every day')
    expect(everyText(10080)).toBe('every 7 days')
  })

  it('shows a next time only for a scheduled check', () => {
    expect(checkSummary(check(), NOW)).toBe('checked 3 min ago · next in 7 min')
    for (const state of Object.keys(STATE_TEXT) as (keyof typeof STATE_TEXT)[]) {
      // Even if a next time came with it, a check that cannot run never shows one.
      const summary = checkSummary(check({ state }), NOW)
      expect(summary).toBe(`checked 3 min ago · ${STATE_TEXT[state]}`)
      expect(summary).not.toContain('next')
    }
    expect(checkSummary(check({ state: 'paused', last_at: null }), NOW)).toBe('paused')
    expect(checkSummary(check({ state: 'outside_hours' }), NOW)).toContain('outside active hours')
  })

  it('words the campaign page’s replies line', () => {
    expect(repliesText(mailboxPoll(), NOW)).toBe('Replies checked 3 min ago · next in 7 min')
    expect(repliesText(mailboxPoll({ state: 'due', next_at: null }), NOW)).toBe(
      'Replies checked 3 min ago · next check within a minute',
    )
    expect(
      repliesText(
        mailboxPoll({
          state: 'off',
          next_at: null,
          replies_polled_at: null,
          reason: 'sender@example.com is disarmed, so its replies aren’t checked',
        }),
        NOW,
      ),
    ).toBe(
      'Replies not checked yet. sender@example.com is disarmed, so its replies aren’t checked.',
    )
  })
})

describe('PollStatusHeader', () => {
  it('refetches every minute, and every 15 seconds while a check now waits', () => {
    const interval = pollStatusQuery.refetchInterval as (query: unknown) => number
    const at = (data: PollStatus) => interval({ state: { data } })
    expect(at(pollStatus())).toBe(60_000)
    expect(
      at(
        pollStatus({ items: [check({ state: 'due', requested: true, requested_at: minutes(0) })] }),
      ),
    ).toBe(15_000)
    expect(interval({ state: { data: undefined } })).toBe(60_000)
  })

  it('shows Gmail and the LinkedIn inbox in the header', async () => {
    renderWith(<PollStatusHeader />, () => pollStatus())

    expect(await screen.findByText('Gmail: checked 3 min ago · next in 7 min')).toBeInTheDocument()
    expect(screen.getByText('LinkedIn inbox: not running yet')).toBeInTheDocument()
  })

  it('lists every check in the popover, with why one has no time', async () => {
    renderWith(<PollStatusHeader />, () => pollStatus())

    fireEvent.click(await screen.findByRole('button', { name: /Background checks/ }))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Background checks')).toBeInTheDocument()
    const linkedin = within(dialog).getByRole('region', { name: 'LinkedIn' })
    expect(within(linkedin).getByText('LinkedIn enrichment')).toBeInTheDocument()
    expect(within(linkedin).getByText('checked 2 h ago · paused')).toBeInTheDocument()
    expect(within(linkedin).getByText('The LinkedIn schedule is paused')).toBeInTheDocument()
    const gmail = within(dialog).getByRole('region', { name: 'Gmail' })
    expect(within(gmail).getByText('Gmail drafts')).toBeInTheDocument()
    expect(within(gmail).getAllByText('every 10 min')).toHaveLength(2)
  })

  it('says when netkeeper serve is not running', async () => {
    renderWith(<PollStatusHeader />, () =>
      pollStatus({
        background_running: false,
        items: [check({ state: 'not_running', next_at: null, reason: 'not serving' })],
      }),
    )

    expect(await screen.findByText('Gmail: checked 3 min ago · not running')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Background checks/ }))
    expect(await screen.findByText(/serve isn’t running/)).toBeInTheDocument()
  })

  it('updates when a LinkedIn run ends, without a reload', async () => {
    let current = pollStatus()
    const { source, fetches } = renderWith(<PollStatusHeader />, () => current)
    await screen.findByText('LinkedIn inbox: not running yet')
    const before = fetches()

    current = pollStatus({
      items: [check({ last_at: minutes(0), next_at: minutes(10) })],
    })
    act(() => source.emit('run.finished', { run_id: 1 }))

    expect(await screen.findByText('Gmail: checked just now · next in 10 min')).toBeInTheDocument()
    expect(fetches()).toBeGreaterThan(before)
  })
})

describe('RepliesChecked', () => {
  it('says when the campaign’s mailbox was last checked for replies', async () => {
    renderWith(<RepliesChecked mailboxId={3} />, () => pollStatus())

    expect(await screen.findByText('Replies checked 3 min ago · next in 7 min')).toBeInTheDocument()
  })

  it('shows nothing for a campaign with no mailbox or an unknown one', async () => {
    const { fetches } = renderWith(
      <>
        <RepliesChecked mailboxId={null} />
        <RepliesChecked mailboxId={99} />
      </>,
      () => pollStatus(),
    )
    await waitFor(() => expect(fetches()).toBeGreaterThan(0))
    expect(screen.queryByText(/Replies/)).not.toBeInTheDocument()
  })
})

describe('Check now', () => {
  async function openPopover() {
    fireEvent.click(await screen.findByRole('button', { name: /Background checks/ }))
    const dialog = await screen.findByRole('dialog')
    return within(dialog).getByRole('region', { name: 'Gmail' })
  }

  it('asks for the reply poll, then waits for the tick and the new last-checked time', async () => {
    let current = pollStatus()
    let release: () => void = () => {}
    let posts = 0
    const { queryClient } = renderWith(
      <PollStatusHeader />,
      () => current,
      () => {
        posts += 1
        return new Promise((resolve) => {
          release = () => resolve(jsonResponse({ already_requested: false }, 202))
        })
      },
    )
    const gmail = await openPopover()
    const button = within(gmail).getByRole('button', { name: 'Check now' })
    expect(button).toBeEnabled()

    fireEvent.click(button)
    await waitFor(() => expect(button).toBeDisabled())
    fireEvent.click(button)
    expect(posts).toBe(1)

    current = pollStatus({
      items: [check({ state: 'due', next_at: null, requested: true, requested_at: minutes(0) })],
    })
    act(() => release())
    expect(await within(gmail).findByText('Checking within a minute')).toBeInTheDocument()
    expect(button).toBeDisabled()

    // The tick polled: the next refresh shows the new time, and the button is back.
    current = pollStatus({ items: [check({ last_at: minutes(0), next_at: minutes(10) })] })
    await act(() => queryClient.invalidateQueries({ queryKey: pollStatusKeys.all }))
    await waitFor(() => expect(button).toBeEnabled())
    expect(within(gmail).getByText('checked just now · next in 10 min')).toBeInTheDocument()
  })

  it('stays pending until the poll status refetch settles, even when it fails', async () => {
    let refetch: (() => void) | null = null
    let refetches = 0
    mockFetch((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/poll-status') {
        refetches += 1
        if (refetches === 1) return jsonResponse(pollStatus())
        return new Promise((resolve) => {
          refetch = () => resolve(jsonResponse({ detail: 'down' }, 500))
        })
      }
      if (pathname === CHECK_NOW) return jsonResponse({ already_requested: false }, 202)
      return jsonResponse({ detail: `unexpected ${pathname}` }, 500)
    })
    const source = new FakeEventSource('/api/v1/events')
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <EventStreamContext
          value={{ status: 'connected', source: source as unknown as EventSource }}
        >
          <RepliesChecked mailboxId={3} />
        </EventStreamContext>
      </QueryClientProvider>,
    )
    const button = await screen.findByRole('button', { name: 'Check now' })

    fireEvent.click(button)
    await waitFor(() => expect(refetch).not.toBeNull())
    expect(button).toBeDisabled() // the POST answered; the refetch has not

    act(() => refetch?.())
    await waitFor(() => expect(button).toBeEnabled()) // a failed refetch does not stick
  })

  it('comes back, saying so, when the check still has not run after three minutes', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
    vi.setSystemTime(NOW)
    renderWith(<PollStatusHeader />, () =>
      pollStatus({
        items: [check({ state: 'due', next_at: null, requested: true, requested_at: minutes(0) })],
      }),
    )
    const gmail = await openPopover()
    const button = within(gmail).getByRole('button', { name: 'Check now' })
    expect(button).toBeDisabled()
    expect(within(gmail).getByText('Checking within a minute')).toBeInTheDocument()

    act(() => vi.advanceTimersByTime(LATE_AFTER_MS))

    expect(button).toBeEnabled()
    expect(within(gmail).getByText('The check hasn’t run yet')).toBeInTheDocument()
  })

  it('shows a refusal with its reason and does not get stuck', async () => {
    renderWith(
      <PollStatusHeader />,
      () => pollStatus(),
      () => jsonResponse({ detail: 'No mailbox is armed, so netkeeper doesn’t read Gmail' }, 409),
    )
    const gmail = await openPopover()
    const button = within(gmail).getByRole('button', { name: 'Check now' })

    fireEvent.click(button)

    expect(await within(gmail).findByRole('alert')).toHaveTextContent(
      'No mailbox is armed, so netkeeper doesn’t read Gmail',
    )
    await waitFor(() => expect(button).toBeEnabled())
  })

  it('is disabled while the check cannot run', async () => {
    renderWith(<PollStatusHeader />, () =>
      pollStatus({
        items: [check({ state: 'off', next_at: null, reason: 'Gmail isn’t connected' })],
      }),
    )
    const gmail = await openPopover()
    expect(within(gmail).getByRole('button', { name: 'Check now' })).toBeDisabled()
  })

  it('sits beside the campaign’s replies line while its mailbox is polled', async () => {
    let posts = 0
    renderWith(
      <RepliesChecked mailboxId={3} />,
      () => pollStatus(),
      () => {
        posts += 1
        return jsonResponse({ already_requested: false }, 202)
      },
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Check now' }))
    await waitFor(() => expect(posts).toBe(1))
  })

  it('is not offered beside a mailbox whose replies are not polled', async () => {
    const { fetches } = renderWith(<RepliesChecked mailboxId={3} />, () =>
      pollStatus({
        mailboxes: [mailboxPoll({ state: 'off', next_at: null, reason: 'disarmed' })],
      }),
    )
    await waitFor(() => expect(fetches()).toBeGreaterThan(0))
    expect(await screen.findByText(/Replies checked/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Check now' })).not.toBeInTheDocument()
  })
})
