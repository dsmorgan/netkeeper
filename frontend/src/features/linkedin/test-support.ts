/**
 * Fixtures and a stand-in backend for the LinkedIn page's tests.
 *
 * Only the test files import this. Every name here is invented (CLAUDE.md);
 * `.example` addresses and no real LinkedIn URNs, in keeping with the rest of
 * the fixtures in this repository.
 */
import { jsonResponse } from '@/test/fetch'

import type {
  BrowserLaunch,
  BudgetStatus,
  Heat,
  LinkedInStatus,
  Pin,
  Run,
  RunPage,
  Schedule,
} from './types'

export const STATUS_CLEAR: LinkedInStatus = {
  session_flag: null,
  session_flagged_at: null,
  heat_tripped: false,
  armed: false,
  schedule_paused: false,
  running_run_id: null,
  can_start_runs: true,
}

export const STATUS_LOGGED_OUT: LinkedInStatus = {
  ...STATUS_CLEAR,
  session_flag: 'logged_out',
  session_flagged_at: '2026-09-20T10:00:00Z',
}

export const STATUS_CHECKPOINT: LinkedInStatus = {
  ...STATUS_CLEAR,
  session_flag: 'checkpoint',
  session_flagged_at: '2026-09-20T10:00:00Z',
}

export const BROWSER_LAUNCH: BrowserLaunch = {
  cdp_url: 'http://127.0.0.1:9222',
  profile_dir: '/tmp/netkeeper-data-test/chrome-profile',
  launch_command: [
    'open -na "Google Chrome" --args \\',
    '  --remote-debugging-port=9222 \\',
    '  --user-data-dir="/tmp/netkeeper-data-test/chrome-profile"',
  ],
  remote_host_note: null,
  check_command: 'netkeeper preflight',
}

export const SCHEDULE_DISARMED: Schedule = {
  armed: false,
  armed_at: null,
  paused: false,
  paused_at: null,
  scheduler_running: true,
  jobs: [
    { kind: 'connections_incremental', interval_hours: 24, next_due: '2026-09-24T08:00:00Z' },
    { kind: 'enrich', interval_hours: 6, next_due: '2026-09-23T14:00:00Z' },
  ],
}

export const SCHEDULE_ARMED: Schedule = {
  ...SCHEDULE_DISARMED,
  armed: true,
  armed_at: '2026-09-22T09:00:00Z',
}

export const BUDGET: BudgetStatus = {
  budgets: [
    { action: 'connection_pages', day: { count: 10, limit: 150, remaining: 140 }, week: null },
    {
      action: 'profile_visits',
      day: { count: 15, limit: 60, remaining: 45 },
      week: { count: 60, limit: 300, remaining: 240 },
    },
    { action: 'inbox_polls', day: { count: 0, limit: 8, remaining: 8 }, week: null },
    { action: 'li_messages_auto', day: { count: 0, limit: 15, remaining: 15 }, week: null },
  ],
  profile_visits_today: {
    ramp: 60,
    after_weekend: 60,
    after_heat: 60,
    spent_today: 15,
    week_left: 240,
    remaining: 45,
  },
  risk_warning: null,
  profile_view_notice:
    "Enrichment opens each contact's LinkedIn profile from your account, so they may see a" +
    ' visit in Who viewed your profile. Whether they see your name and headline, a partial' +
    ' description (such as someone at your company), or an anonymous viewer depends on the' +
    " Profile viewing options in LinkedIn's Visibility settings. netkeeper never changes that" +
    ' setting.',
}

/** What `GET /linkedin/budget` says about a daily profile-visit limit of 150 (#318). */
export const RISK_WARNING =
  'Profile visits are set to 150 a day, above the 100 a day netkeeper was designed around.' +
  ' More visits a day make it more likely that LinkedIn restricts your account or asks you to' +
  ' verify it. Heat still slows runs down after LinkedIn throttles a visit.'

export const HEAT: Heat = {
  score: 0,
  threshold: 2.5,
  multiplier: 1,
  tripped: false,
  last_raised_at: null,
  cleared_at: null,
  resumes_at: null,
}

export const PINS: Pin[] = [
  { contact_id: 1, first_name: 'Rosalind', last_name: 'Quillfeather' },
  { contact_id: 2, first_name: 'Tobias', last_name: 'Marrowbone' },
]

export const FIVE_PINS: Pin[] = [
  { contact_id: 1, first_name: 'Rosalind', last_name: 'Quillfeather' },
  { contact_id: 2, first_name: 'Tobias', last_name: 'Marrowbone' },
  { contact_id: 3, first_name: 'Imogen', last_name: 'Pallisade' },
  { contact_id: 4, first_name: 'Hortensia', last_name: 'Blennerhassett' },
  { contact_id: 5, first_name: 'Petronella', last_name: 'Quill' },
]

export function run(overrides: Partial<Run> = {}): Run {
  return {
    id: 1,
    kind: 'connections_incremental',
    status: 'running',
    trigger: 'manual',
    started_at: '2026-09-23T10:00:00Z',
    completed_at: null,
    stop_reason: null,
    stop_reason_text: null,
    cancel_requested_at: null,
    max_visits: null,
    resume_of_id: null,
    browser_mode: 'attach',
    progress: null,
    counts: null,
    notes: null,
    error: null,
    planned: null,
    completed: null,
    aging_refused: null,
    resumed_by: null,
    pause_requested: false,
    ...overrides,
  }
}

export function runPage(items: Run[], total = items.length): RunPage {
  return { items, total }
}

export interface Call {
  method: string
  path: string
  query: URLSearchParams
  body: unknown
}

export type Handler = (call: Call) => Response | Promise<Response>

/**
 * Routes `METHOD /path` to a handler, answers `/health`, `/me`, and an empty
 * `/tags` for the app shell, and records every call so a test can assert on
 * what was sent — the same shape `imports/test-support.ts`'s `backend` uses.
 */
export function backend(handlers: Record<string, Handler>, calls: Call[] = []) {
  return async (request: Request): Promise<Response> => {
    const url = new URL(request.url)
    let body: unknown
    if (request.method !== 'GET') {
      const raw = await request.text()
      body = raw === '' ? undefined : JSON.parse(raw)
    }
    const call: Call = { method: request.method, path: url.pathname, query: url.searchParams, body }
    calls.push(call)
    const handler = handlers[`${request.method} ${url.pathname}`]
    if (handler !== undefined) return handler(call)
    if (url.pathname === '/api/v1/health') {
      return jsonResponse({ status: 'ok', version: '0.0.1-test' })
    }
    if (url.pathname === '/api/v1/me') {
      return jsonResponse({
        id: 1,
        kind: 'local',
        display_name: 'Test User',
        email: null,
        timezone: 'UTC',
      })
    }
    if (url.pathname === '/api/v1/contacts/query') {
      return jsonResponse({ items: [], total: 0, describe: 'no matches' })
    }
    return jsonResponse({ detail: `no fake for ${request.method} ${url.pathname}` }, 404)
  }
}

/** The default set of `GET` answers a page render needs, so a test only overrides what it cares about. */
export function defaultHandlers(overrides: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    'GET /api/v1/linkedin/status': () => jsonResponse(STATUS_CLEAR),
    'GET /api/v1/linkedin/browser': () => jsonResponse(BROWSER_LAUNCH),
    'GET /api/v1/linkedin/schedule': () => jsonResponse(SCHEDULE_DISARMED),
    'GET /api/v1/linkedin/budget': () => jsonResponse(BUDGET),
    'GET /api/v1/linkedin/heat': () => jsonResponse(HEAT),
    'GET /api/v1/linkedin/pins': () => jsonResponse(PINS),
    'GET /api/v1/linkedin/runs': () => jsonResponse(runPage([run()])),
    ...overrides,
  }
}
