/**
 * Fixtures and a stand-in backend for the campaign tests.
 *
 * Only the test files import this. Everyone in it is invented, with `.example`
 * addresses (CLAUDE.md). Timestamps are mid-day UTC in the middle of a year, so
 * the year a test reads off one is the same in every time zone (#285).
 */
import { mailbox } from '@/features/mailboxes/test-support'
import type { ListOut } from '@/features/crm/types'
import type { TemplateOut } from '@/features/templates/api'
import type { SendingHours } from '@/features/settings/api'
import { backend, type Call } from '@/features/imports/test-support'
import { jsonResponse } from '@/test/fetch'

import type {
  Campaign,
  CampaignResults,
  CampaignSummary,
  EnrollmentPage,
  MessagePreview,
  Missing,
  Review,
  StartOptions,
  StepReview,
} from './api'

export type { Call }

export const MAILBOX = mailbox({ id: 3, email: 'me@sender.example', arm: 'send' })

function template(overrides: Partial<TemplateOut>): TemplateOut {
  return {
    id: 1,
    name: 'Catching up',
    channel: 'email',
    subject: 'Catching up',
    body: 'Hi {{ first_name }}',
    version: 1,
    previous_id: null,
    current: true,
    in_use: false,
    lint: [],
    created_at: '2030-06-15T12:00:00Z',
    updated_at: '2030-06-15T12:00:00Z',
    ...overrides,
  }
}

export const TEMPLATES: TemplateOut[] = [
  template({ id: 11, name: 'Catching up' }),
  template({ id: 12, name: 'Follow-up nudge' }),
  template({ id: 13, name: 'LinkedIn hello', channel: 'linkedin', subject: null }),
]

function list(overrides: Partial<ListOut>): ListOut {
  return {
    id: 1,
    name: 'Old colleagues',
    kind: 'static',
    filter: null,
    member_count: 3,
    builtin: false,
    created_at: '2030-06-15T12:00:00Z',
    updated_at: '2030-06-15T12:00:00Z',
    ...overrides,
  }
}

export const LISTS: ListOut[] = [
  list({ id: 21, name: 'Old colleagues' }),
  list({ id: 22, name: 'Conference friends', member_count: 5 }),
]

export const STEPS: Campaign['steps'] = [
  {
    id: 101,
    position: 1,
    channel: 'email',
    mode: 'draft',
    condition: 'always',
    delay_days: 0,
    send_time: null,
    same_thread: false,
    template_id: 11,
    template_name: 'Catching up',
    template_version: 1,
    fired: 0,
    sent: 0,
  },
  {
    id: 102,
    position: 2,
    channel: 'email',
    mode: 'send',
    condition: 'no_reply',
    delay_days: 7,
    send_time: null,
    same_thread: true,
    template_id: 12,
    template_name: 'Follow-up nudge',
    template_version: 2,
    fired: 0,
    sent: 0,
  },
]

export function campaign(overrides: Partial<Campaign> = {}): Campaign {
  return {
    id: 5,
    name: 'Autumn reconnect',
    status: 'draft',
    mailbox_id: 3,
    mailbox_email: 'me@sender.example',
    source_list_id: 21,
    filter: null,
    daily_cap: null,
    contacted_within_days_guard: 30,
    approved_at: null,
    starts_at: null,
    start_editable: false,
    created_at: '2030-06-15T12:00:00Z',
    steps: STEPS,
    enrollments: {},
    next_action_at: null,
    missing: [],
    ...overrides,
  }
}

export function summary(overrides: Partial<CampaignSummary> = {}): CampaignSummary {
  return {
    id: 5,
    name: 'Autumn reconnect',
    status: 'draft',
    mailbox_id: 3,
    steps: 2,
    enrollments: {},
    created_at: '2030-06-15T12:00:00Z',
    next_action_at: null,
    ...overrides,
  }
}

/** Everything the gate can report for a fresh reviewing campaign. */
export const ALL_MISSING: Missing[] = [
  {
    requirement: 'step_approvals',
    detail: 'steps not approved',
    enrollment_ids: [],
    step_positions: [1, 2],
  },
  {
    requirement: 'test_sends',
    detail: 'email steps with no current test send',
    enrollment_ids: [],
    step_positions: [1, 2],
  },
  {
    requirement: 'lint',
    detail: 'no lint result for the current steps and templates',
    enrollment_ids: [],
    step_positions: [],
  },
  {
    requirement: 'guards',
    detail: 'the guard summary for the current audience is not acknowledged',
    enrollment_ids: [],
    step_positions: [],
  },
]

export function review(overrides: Partial<Review> = {}): Review {
  return {
    campaign_id: 5,
    status: 'reviewing',
    content_fingerprint: 'c0ffee',
    audience_fingerprint: 'aud1ence',
    guard_summary: '3 in audience, 1 excluded: 1 do-not-contact',
    guards_acknowledged: null,
    missing: ALL_MISSING,
    ...overrides,
  }
}

export function message(overrides: Partial<MessagePreview> = {}): MessagePreview {
  return {
    enrollment_id: 301,
    contact_id: 401,
    contact_name: 'Rosalind Quillfeather',
    to_address: 'rosalind@nimbus-kettle.example',
    subject: 'Catching up',
    body: 'Hi Rosalind',
    issues: [],
    blocked: null,
    approved: false,
    fingerprint: 'fp-rosalind-000000',
    ...overrides,
  }
}

export const MESSAGES: MessagePreview[] = [
  message(),
  message({
    enrollment_id: 302,
    contact_id: 402,
    contact_name: 'Tobias Marrowbone',
    to_address: 'tobias@orrery-works.example',
    body: 'Hi Tobias',
    fingerprint: 'fp-tobias-111111',
  }),
  message({
    enrollment_id: 303,
    contact_id: 403,
    contact_name: 'Wilhelmina Fernsby',
    to_address: 'wilhelmina@lantern-row.example',
    body: 'Hi Wilhelmina',
    fingerprint: 'fp-wilhelmina-222222',
  }),
]

export function stepReview(overrides: Partial<StepReview> = {}): StepReview {
  return {
    step_id: 101,
    position: 1,
    channel: 'email',
    template_name: 'Catching up v1',
    fingerprint: 'step-fp-101',
    per_message: false,
    approved: false,
    total: MESSAGES.length,
    offset: 0,
    messages: MESSAGES,
    blocked: [],
    unapproved: 0,
    ...overrides,
  }
}

export const ENROLLMENTS: EnrollmentPage = {
  total: 2,
  items: [
    {
      id: 301,
      contact_id: 401,
      contact_name: 'Rosalind Quillfeather',
      email: 'rosalind@nimbus-kettle.example',
      status: 'active',
      current_step: 1,
      next_action_at: '2030-06-20T12:00:00Z',
      exit_reason: null,
      replied_at: null,
    },
    {
      id: 302,
      contact_id: 402,
      contact_name: 'Tobias Marrowbone',
      email: 'tobias@orrery-works.example',
      status: 'replied',
      current_step: 1,
      next_action_at: null,
      exit_reason: 'replied',
      replied_at: '2030-06-18T12:00:00Z',
    },
  ],
}

/** A campaign's results: nothing sent yet unless a test says otherwise (#350). */
export function results(overrides: Partial<CampaignResults> = {}): CampaignResults {
  return {
    campaign_id: 5,
    timezone: 'America/New_York',
    sends_per_day: [],
    steps: STEPS.map((s) => ({
      step_id: s.id,
      position: s.position,
      sent: 0,
      replied: 0,
      bounced: 0,
      opted_out: 0,
    })),
    totals: { sent: 0, contacted: 0, replied: 0, reply_rate: null, bounced: 0, opted_out: 0 },
    ...overrides,
  }
}

type Handler = (call: Call) => Response | Promise<Response>

/** The default sending hours, as the backend answers them (#338). */
export const SENDING_HOURS: SendingHours = {
  enabled: true,
  days: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'],
  start: '09:00',
  end: '17:00',
  timezone: 'America/New_York',
  summary: 'Mon to Fri, 09:00 to 17:00',
  readable: true,
}

/** The next Tuesday at 09:00 in New York, as the backend answers it (#338). */
export const DEFAULT_START = '2030-06-18T13:00:00Z'

export function startOptions(overrides: Partial<StartOptions> = {}): StartOptions {
  return {
    timezone: 'America/New_York',
    default_start: DEFAULT_START,
    suggestion: 'Most effective: Tue–Thu mornings.',
    reminder:
      'netkeeper sends only while `serve` is running and this Mac is awake. Keep it running from the start time until the batch finishes.',
    sending_hours:
      'Sending hours: Mon to Fri, 09:00 to 17:00. The first batch starts at the start you choose; the rest of it, and every later step, sends only inside these hours.',
    at: null,
    warning: null,
    ...overrides,
  }
}

/**
 * A stand-in backend for one campaign. `state` is read on every call, so a
 * handler a test passes can change what the next fetch sees.
 */
export function campaignBackend(
  state: { campaign: Campaign; review: Review },
  overrides: Record<string, Handler> = {},
  calls: Call[] = [],
) {
  const id = state.campaign.id
  return backend(
    {
      'GET /api/v1/campaigns': () => jsonResponse([summary()]),
      [`GET /api/v1/campaigns/${id}`]: () => jsonResponse(state.campaign),
      [`GET /api/v1/campaigns/${id}/review`]: () => jsonResponse(state.review),
      [`GET /api/v1/campaigns/${id}/review/steps/101`]: () => jsonResponse(stepReview()),
      [`GET /api/v1/campaigns/${id}/review/steps/102`]: () =>
        jsonResponse(
          stepReview({
            step_id: 102,
            position: 2,
            template_name: 'Follow-up nudge v2',
            fingerprint: 'step-fp-102',
          }),
        ),
      [`GET /api/v1/campaigns/${id}/enrollments`]: () => jsonResponse(ENROLLMENTS),
      [`GET /api/v1/campaigns/${id}/results`]: () => jsonResponse(results({ campaign_id: id })),
      'GET /api/v1/settings/sending-hours': () => jsonResponse(SENDING_HOURS),
      [`GET /api/v1/campaigns/${id}/start-options`]: (call) =>
        jsonResponse(startOptions({ at: call.query.get('at') })),
      'GET /api/v1/lists': () => jsonResponse(LISTS),
      'GET /api/v1/templates': () => jsonResponse(TEMPLATES),
      'GET /api/v1/mailboxes': () => jsonResponse([MAILBOX]),
      'GET /api/v1/tags': () => jsonResponse([]),
      ...overrides,
    },
    calls,
  )
}
