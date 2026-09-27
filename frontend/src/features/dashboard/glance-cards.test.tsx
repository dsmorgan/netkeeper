import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { render, screen, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import {
  BrowserHealthCard,
  BudgetHeatCard,
  ChangedJobsCard,
  NextLinkedInRunCard,
  NextSendsCard,
  RepliesCard,
} from './glance-cards'

type Route = () => Response | Promise<Response>

/** A promise the test never resolves, to hold a query in its loading state. */
const never: Route = () => new Promise<Response>(() => {})
const broken: Route = () => jsonResponse({ detail: 'boom' }, 500)
const json =
  (body: unknown): Route =>
  () =>
    jsonResponse(body)

/**
 * One card under a minimal router (its `Link`s need one), against a fake
 * backend keyed by path. A path the test did not route answers 500, so a card
 * reading something unexpected shows up as an error state, not a pass.
 */
function renderCard(Card: () => ReactNode, routes: Record<string, Route>) {
  const seen: string[] = []
  mockFetch((request) => {
    const { pathname } = new URL(request.url)
    seen.push(`${pathname}${new URL(request.url).search}`)
    const route = routes[pathname]
    return route === undefined ? jsonResponse({ detail: `unexpected ${pathname}` }, 500) : route()
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({ component: () => <Outlet /> })
  const router = createRouter({
    routeTree: root.addChildren([
      createRoute({ getParentRoute: () => root, path: '/', component: Card }),
      createRoute({
        getParentRoute: () => root,
        path: '/contacts/$contactId',
        component: () => <p>Contact page</p>,
      }),
      createRoute({
        getParentRoute: () => root,
        path: '/linkedin',
        component: () => <p>LinkedIn page</p>,
      }),
    ]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  })
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { seen }
}

/** The card that holds `title`, to read inside it alone. */
async function card(title: string) {
  const heading = await screen.findByRole('heading', { level: 2, name: title })
  const node = heading.closest('[data-slot="card"]')
  if (!(node instanceof HTMLElement)) throw new Error(`no card around ${title}`)
  return within(node)
}

afterEach(() => {
  vi.restoreAllMocks()
})

// --- fixtures (fake people only) ---------------------------------------------------------

const FUTURE = '2099-01-02T15:00:00Z'
const PAST = '2020-01-02T15:00:00Z'

function fire(overrides: Record<string, unknown> = {}) {
  return {
    enrollment_id: 1,
    due: FUTURE,
    campaign_id: 7,
    campaign_name: 'Autumn hello',
    step_position: 2,
    channel: 'email',
    contact_id: 42,
    contact_name: 'Fic Person',
    ...overrides,
  }
}

const SCHEDULE = '/api/v1/linkedin/schedule'
const STATUS = '/api/v1/linkedin/status'
const RUNS = '/api/v1/linkedin/runs'
const BUDGET = '/api/v1/linkedin/budget'
const HEAT = '/api/v1/linkedin/heat'

function linkedinStatus(overrides: Record<string, unknown> = {}) {
  return {
    session_flag: null,
    session_flagged_at: null,
    heat_tripped: false,
    armed: true,
    running_run_id: null,
    can_start_runs: true,
    ...overrides,
  }
}

function run(overrides: Record<string, unknown> = {}) {
  return {
    id: 5,
    kind: 'connections_incremental',
    status: 'completed',
    trigger: 'scheduled',
    started_at: '2026-09-26T10:00:00Z',
    completed_at: '2026-09-26T10:05:00Z',
    stop_reason: null,
    cancel_requested_at: null,
    max_visits: null,
    resume_of_id: null,
    browser_mode: 'attach',
    progress: null,
    counts: null,
    notes: null,
    ...overrides,
  }
}

function budget(overrides: Record<string, unknown> = {}) {
  return {
    budgets: [],
    profile_visits_today: {
      ramp: 20,
      after_weekend: 20,
      after_heat: 16,
      spent_today: 4,
      week_left: 80,
      remaining: 12,
      ...overrides,
    },
  }
}

function heat(overrides: Record<string, unknown> = {}) {
  return {
    score: 0.5,
    threshold: 5,
    multiplier: 1,
    tripped: false,
    last_raised_at: null,
    cleared_at: null,
    resumes_at: null,
    ...overrides,
  }
}

// --- next campaign sends -----------------------------------------------------------------

describe('NextSendsCard', () => {
  const PATH = '/api/v1/dashboard/next-fires'

  it('announces that it is checking', async () => {
    renderCard(NextSendsCard, { [PATH]: never })
    expect((await card('Next campaign sends')).getByRole('status')).toHaveTextContent('Checking')
  })

  it('says so when nothing is scheduled', async () => {
    renderCard(NextSendsCard, { [PATH]: json({ items: [], total: 0 }) })
    const body = await card('Next campaign sends')
    expect(await body.findByText('Nothing is scheduled to send.')).toBeInTheDocument()
  })

  it('shows a failure as an alert, not a crash', async () => {
    renderCard(NextSendsCard, { [PATH]: broken })
    const body = await card('Next campaign sends')
    expect(await body.findByRole('alert')).toHaveTextContent('Upcoming sends could not be loaded.')
  })

  it('lists each send with its time, contact link, campaign, and step', async () => {
    renderCard(NextSendsCard, {
      [PATH]: json({
        items: [
          fire({ enrollment_id: 1, due: PAST, contact_id: 41, contact_name: 'Ada Example' }),
          fire({ enrollment_id: 2, due: FUTURE, step_position: 3 }),
        ],
        total: 5,
      }),
    })
    const body = await card('Next campaign sends')
    const items = await body.findAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('due now')
    expect(items[0]).toHaveTextContent('Autumn hello · step 2')
    expect(within(items[0]!).getByRole('link', { name: 'Ada Example' })).toHaveAttribute(
      'href',
      '/contacts/41',
    )
    expect(items[1]).not.toHaveTextContent('due now')
    expect(items[1]).toHaveTextContent('2099')
    expect(items[1]).toHaveTextContent('step 3')
    expect(body.getByText('and 3 more')).toBeInTheDocument()
  })
})

// --- next LinkedIn run -------------------------------------------------------------------

describe('NextLinkedInRunCard', () => {
  function schedule(overrides: Record<string, unknown> = {}) {
    return {
      armed: true,
      armed_at: '2026-09-20T12:00:00Z',
      scheduler_running: true,
      jobs: [
        { kind: 'connections_full', interval_hours: 168, next_due: '2099-03-01T09:00:00Z' },
        { kind: 'connections_incremental', interval_hours: 24, next_due: '2099-01-01T09:00:00Z' },
        { kind: 'enrich', interval_hours: 24, next_due: null },
      ],
      ...overrides,
    }
  }

  it('announces that it is checking', async () => {
    renderCard(NextLinkedInRunCard, { [SCHEDULE]: never })
    expect((await card('Next LinkedIn run')).getByRole('status')).toHaveTextContent('Checking')
  })

  it('says scheduled runs are off rather than showing a time that will not fire', async () => {
    renderCard(NextLinkedInRunCard, { [SCHEDULE]: json(schedule({ armed: false })) })
    const body = await card('Next LinkedIn run')
    expect(await body.findByText(/Scheduled runs are off/)).toBeInTheDocument()
    expect(body.queryByText(/2099/)).not.toBeInTheDocument()
    expect(body.getByRole('link', { name: 'Open LinkedIn' })).toHaveAttribute('href', '/linkedin')
  })

  it('says so when armed but nothing is scheduled yet', async () => {
    renderCard(NextLinkedInRunCard, {
      [SCHEDULE]: json(
        schedule({ jobs: [{ kind: 'enrich', interval_hours: 24, next_due: null }] }),
      ),
    })
    const body = await card('Next LinkedIn run')
    expect(await body.findByText('No run is scheduled yet.')).toBeInTheDocument()
  })

  it('shows a failure as an alert', async () => {
    renderCard(NextLinkedInRunCard, { [SCHEDULE]: broken })
    const body = await card('Next LinkedIn run')
    expect(await body.findByRole('alert')).toHaveTextContent(
      'The LinkedIn schedule could not be loaded.',
    )
  })

  it('lists the due runs soonest first, leaving out a kind with no due time', async () => {
    renderCard(NextLinkedInRunCard, { [SCHEDULE]: json(schedule()) })
    const body = await card('Next LinkedIn run')
    const items = await body.findAllByRole('listitem')
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringContaining('Incremental connections sync'),
      expect.stringContaining('Full connections sync'),
    ])
    expect(body.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('warns when no scheduler runs in this process', async () => {
    renderCard(NextLinkedInRunCard, {
      [SCHEDULE]: json(schedule({ scheduler_running: false })),
    })
    const body = await card('Next LinkedIn run')
    expect(await body.findByRole('alert')).toHaveTextContent('No scheduler is running')
  })
})

// --- browser health ----------------------------------------------------------------------

describe('BrowserHealthCard', () => {
  it('announces that it is checking', async () => {
    renderCard(BrowserHealthCard, { [STATUS]: never, [RUNS]: never })
    const body = await card('LinkedIn browser')
    expect(body.getAllByRole('status')).toHaveLength(2)
  })

  it('says only what it knows when there is no flag and no run yet', async () => {
    const { seen } = renderCard(BrowserHealthCard, {
      [STATUS]: json(linkedinStatus()),
      [RUNS]: json({ items: [], total: 0 }),
    })
    const body = await card('LinkedIn browser')
    expect(await body.findByText('No session flag is raised.')).toBeInTheDocument()
    expect(await body.findByText('No LinkedIn run yet.')).toBeInTheDocument()
    // "No flag" is never dressed up as "healthy": nothing here checked the live session (#282).
    expect(body.queryByText(/healthy/i)).not.toBeInTheDocument()
    expect(seen).toContain('/api/v1/linkedin/runs?limit=1&offset=0')
  })

  it('shows each failure as an alert', async () => {
    renderCard(BrowserHealthCard, { [STATUS]: broken, [RUNS]: broken })
    const body = await card('LinkedIn browser')
    expect(await body.findByText('The session flag could not be loaded.')).toHaveAttribute(
      'role',
      'alert',
    )
    expect(await body.findByText('The last run could not be loaded.')).toHaveAttribute(
      'role',
      'alert',
    )
  })

  it('shows how the last run ended', async () => {
    renderCard(BrowserHealthCard, {
      [STATUS]: json(linkedinStatus()),
      [RUNS]: json({
        items: [run({ kind: 'enrich', status: 'aborted', stop_reason: 'budget spent' })],
        total: 9,
      }),
    })
    const body = await card('LinkedIn browser')
    expect(await body.findByText('Last run: Enrichment')).toBeInTheDocument()
    expect(body.getByText('Aborted')).toBeInTheDocument()
    expect(body.getByText(/Ended .* · budget spent/)).toBeInTheDocument()
  })

  it('raises a checkpoint flag as an alert with the way to the LinkedIn page', async () => {
    renderCard(BrowserHealthCard, {
      [STATUS]: json(
        linkedinStatus({ session_flag: 'checkpoint', session_flagged_at: '2026-09-26T09:00:00Z' }),
      ),
      [RUNS]: json({ items: [run({ status: 'failed' })], total: 1 }),
    })
    const body = await card('LinkedIn browser')
    expect(await body.findByRole('alert')).toHaveTextContent('LinkedIn asked for a checkpoint')
    expect(body.getByRole('link', { name: 'Open LinkedIn' })).toHaveAttribute('href', '/linkedin')
  })

  it('raises a logged-out flag with its own advice', async () => {
    renderCard(BrowserHealthCard, {
      [STATUS]: json(linkedinStatus({ session_flag: 'logged_out' })),
      [RUNS]: json({ items: [], total: 0 }),
    })
    const body = await card('LinkedIn browser')
    expect(await body.findByRole('alert')).toHaveTextContent('Logged out of LinkedIn')
    expect(body.getByRole('alert')).toHaveTextContent('netkeeper preflight')
  })
})

// --- budget and heat ---------------------------------------------------------------------

describe('BudgetHeatCard', () => {
  it('announces that it is checking', async () => {
    renderCard(BudgetHeatCard, { [BUDGET]: never, [HEAT]: never })
    expect((await card('Budget and heat')).getAllByRole('status')).toHaveLength(2)
  })

  it('reads plainly with nothing spent and no heat', async () => {
    renderCard(BudgetHeatCard, {
      [BUDGET]: json(budget({ spent_today: 0, remaining: 16 })),
      [HEAT]: json(heat({ score: 0 })),
    })
    const body = await card('Budget and heat')
    expect(await body.findByText(/profile visits left today/)).toHaveTextContent(
      '16 profile visits left today (0 spent of 16)',
    )
    expect(await body.findByText(/Heat 0.00 of 5.00/)).toBeInTheDocument()
    expect(body.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows each failure as an alert', async () => {
    renderCard(BudgetHeatCard, { [BUDGET]: broken, [HEAT]: broken })
    const body = await card('Budget and heat')
    expect(await body.findByText("Today's budget could not be loaded.")).toBeInTheDocument()
    expect(await body.findByText('Heat could not be loaded.')).toBeInTheDocument()
  })

  it('shows what is left, and a slowed pace while warm', async () => {
    renderCard(BudgetHeatCard, {
      [BUDGET]: json(budget()),
      [HEAT]: json(heat({ score: 2.5, multiplier: 1.5 })),
    })
    const body = await card('Budget and heat')
    expect(await body.findByText(/profile visits left today/)).toHaveTextContent(
      '12 profile visits left today (4 spent of 16)',
    )
    expect(await body.findByText(/pacing slowed 1.50×/)).toBeInTheDocument()
  })

  it('raises tripped heat as an alert with when runs resume', async () => {
    renderCard(BudgetHeatCard, {
      [BUDGET]: json(budget()),
      [HEAT]: json(heat({ score: 6, tripped: true, resumes_at: '2099-01-01T00:00:00Z' })),
    })
    const body = await card('Budget and heat')
    expect(await body.findByRole('alert')).toHaveTextContent(
      /over its threshold, so runs are skipped until .*2099/,
    )
  })
})

// --- replies this week -------------------------------------------------------------------

describe('RepliesCard', () => {
  const PATH = '/api/v1/dashboard/inbound'
  const inbound = (count: number) => ({
    count,
    since: '2026-09-20T12:00:00Z',
    reply_detection: false,
  })

  it('announces that it is checking', async () => {
    renderCard(RepliesCard, { [PATH]: never })
    expect((await card('Replies this week')).getByRole('status')).toHaveTextContent('Checking')
  })

  it('says reply detection is not set up, and that nothing came in', async () => {
    renderCard(RepliesCard, { [PATH]: json(inbound(0)) })
    const body = await card('Replies this week')
    expect(await body.findByText(/Reply detection is not set up yet/)).toBeInTheDocument()
    expect(body.getByText('No inbound messages logged in the last 7 days.')).toBeInTheDocument()
  })

  it('shows a failure as an alert', async () => {
    renderCard(RepliesCard, { [PATH]: broken })
    const body = await card('Replies this week')
    expect(await body.findByRole('alert')).toHaveTextContent(
      'Inbound messages could not be loaded.',
    )
  })

  it('counts inbound messages under their own name, never as replies', async () => {
    renderCard(RepliesCard, { [PATH]: json(inbound(3)) })
    const body = await card('Replies this week')
    expect(await body.findByText(/inbound messages \(email and LinkedIn\)/)).toHaveTextContent(
      '3 inbound messages (email and LinkedIn) logged in the last 7 days.',
    )
    expect(body.queryByText(/\d+ repl/)).not.toBeInTheDocument()
  })
})

// --- changed jobs ------------------------------------------------------------------------

describe('ChangedJobsCard', () => {
  const PATH = '/api/v1/dashboard/changed-jobs'

  it('announces that it is checking', async () => {
    renderCard(ChangedJobsCard, { [PATH]: never })
    expect((await card('Changed jobs')).getByRole('status')).toHaveTextContent('Checking')
  })

  it('says so when nobody changed jobs', async () => {
    renderCard(ChangedJobsCard, { [PATH]: json({ items: [], total: 0, days: 30 }) })
    const body = await card('Changed jobs')
    expect(
      await body.findByText("Nobody's position changed in the last 30 days."),
    ).toBeInTheDocument()
  })

  it('shows a failure as an alert', async () => {
    renderCard(ChangedJobsCard, { [PATH]: broken })
    const body = await card('Changed jobs')
    expect(await body.findByRole('alert')).toHaveTextContent('Changed jobs could not be loaded.')
  })

  it('links each person to their contact, with the new role and the date', async () => {
    renderCard(ChangedJobsCard, {
      [PATH]: json({
        items: [
          {
            contact_id: 12,
            contact_name: 'Fictional Mover',
            current_title: 'Head of Tea',
            current_company: 'Kettle Ltd',
            changed_on: '2026-09-01',
          },
          {
            contact_id: 13,
            contact_name: 'Quiet Leaver',
            current_title: null,
            current_company: null,
            changed_on: '2026-08-30',
          },
        ],
        total: 4,
        days: 30,
      }),
    })
    const body = await card('Changed jobs')
    const items = await body.findAllByRole('listitem')
    expect(within(items[0]!).getByRole('link', { name: 'Fictional Mover' })).toHaveAttribute(
      'href',
      '/contacts/12',
    )
    // A calendar date stays the same day wherever the reader is.
    expect(items[0]).toHaveTextContent('Sep 1, 2026')
    expect(items[0]).toHaveTextContent('Head of Tea at Kettle Ltd')
    expect(within(items[1]!).getByRole('link', { name: 'Quiet Leaver' })).toHaveAttribute(
      'href',
      '/contacts/13',
    )
    expect(body.getByText('and 2 more')).toBeInTheDocument()
    expect(body.getByText('Started or left a position in the last 30 days.')).toBeInTheDocument()
  })
})
