/**
 * Every call the campaign screens make, through the generated client (P3-11a).
 *
 * `/campaigns` is P3-13's (create, enroll, list, status, pause, resume) and the
 * review gate under `/campaigns/{id}/review/...` plus `POST .../activate` is
 * P3-09's (spec 11.8). Nothing here activates a campaign except `activate`,
 * which is the gate itself: it answers 409 with `missing` unless every
 * requirement is recorded and current.
 */
import { keepPreviousData, queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import { detailMessage } from '@/api/errors'
import type { components } from '@/api/schema'

type Schemas = components['schemas']

export type CampaignSummary = Schemas['CampaignSummaryOut']
export type Campaign = Schemas['CampaignOut']
export type CampaignStatus = Schemas['CampaignStatus']
export type CampaignCreate = Schemas['CampaignCreate']
export type Step = Schemas['StepOut']
export type StepIn = Schemas['StepIn']
export type StepMode = Schemas['StepMode']
export type StepCondition = Schemas['StepCondition']
export type EnrollmentStatus = Schemas['EnrollmentStatus']
export type EnrollIn = Schemas['EnrollIn']
export type EnrollOut = Schemas['EnrollOut']
export type Missing = Schemas['MissingOut']
export type Review = Schemas['ReviewOut']
export type GuardDetails = Schemas['GuardsOut']
export type SkippedContact = Schemas['SkippedContactOut']
export type StepReview = Schemas['StepReviewOut']
export type MessagePreview = Schemas['MessagePreviewOut']
export type LintResult = Schemas['LintOut']
export type TestSend = Schemas['TestSendOut']
export type Enrollment = Schemas['EnrollmentOut']
export type EnrollmentPage = Schemas['EnrollmentPageOut']
export type StartOptions = Schemas['StartOptionsOut']
export type CampaignResults = Schemas['CampaignResultsOut']
export type StepResults = Schemas['StepResultsOut']
export type DaySends = Schemas['DaySendsOut']
export type DeletePlan = Schemas['DeletePlanOut']
export type LeftoverDraft = Schemas['LeftoverDraftOut']

/** A request the backend refused: its status, its sentence, and `missing` when it sent one. */
export class CampaignApiError extends Error {
  readonly status: number
  readonly missing: Missing[] | null
  /** `stale` when only what was shown went out of date (show it again, then retry). */
  readonly code: string | null

  constructor(status: number, body: unknown, fallback: string) {
    super(detailMessage(body) ?? `${fallback} (HTTP ${status})`)
    this.name = 'CampaignApiError'
    this.status = status
    this.missing =
      body !== null &&
      typeof body === 'object' &&
      'missing' in body &&
      Array.isArray((body as { missing: unknown }).missing)
        ? (body as { missing: Missing[] }).missing
        : null
    this.code =
      body !== null &&
      typeof body === 'object' &&
      'code' in body &&
      typeof (body as { code: unknown }).code === 'string'
        ? (body as { code: string }).code
        : null
  }
}

function fail(status: number, body: unknown, fallback: string): never {
  throw new CampaignApiError(status, body, fallback)
}

/** The message to show for any failure a campaign call throws. */
export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong.'
}

export const campaignKeys = {
  all: ['campaigns'] as const,
  list: () => [...campaignKeys.all, 'list'] as const,
  archived: () => [...campaignKeys.list(), 'archived'] as const,
  one: (id: number) => [...campaignKeys.all, 'one', id] as const,
  review: (id: number) => [...campaignKeys.all, 'review', id] as const,
  guards: (id: number) => [...campaignKeys.review(id), 'guards'] as const,
  steps: (id: number) => [...campaignKeys.all, 'step', id] as const,
  step: (id: number, stepId: number, offset: number) =>
    [...campaignKeys.steps(id), stepId, offset] as const,
  enrollments: (id: number, q: string, status: string, offset: number) =>
    [...campaignKeys.all, 'enrollments', id, q, status, offset] as const,
  startOptions: (id: number, at: string | null) =>
    [...campaignKeys.all, 'start-options', id, at] as const,
  results: (id: number) => [...campaignKeys.all, 'results', id] as const,
  deletePlan: (id: number) => [...campaignKeys.all, 'delete-plan', id] as const,
}

/**
 * The default start (the next Tuesday at 09:00 in your time zone), the suggestion and
 * the reminder, and a warning, never a refusal, when `at` is outside the suggested
 * slots (#338). `at` is an ISO time with its zone, or null for none.
 */
export function startOptionsQuery(id: number, at: string | null) {
  return queryOptions({
    queryKey: campaignKeys.startOptions(id, at),
    queryFn: async ({ signal }): Promise<StartOptions> => {
      const { data, error, response } = await api.GET(
        '/api/v1/campaigns/{campaign_id}/start-options',
        {
          params: { path: { campaign_id: id }, query: at === null ? {} : { at } },
          signal,
        },
      )
      if (data === undefined) fail(response.status, error, 'could not load the start options')
      return data
    },
  })
}

/** Every campaign that is not archived, newest first. */
export const campaignsQuery = queryOptions({
  queryKey: campaignKeys.list(),
  queryFn: async ({ signal }): Promise<CampaignSummary[]> => {
    const { data, error, response } = await api.GET('/api/v1/campaigns', { signal })
    if (data === undefined) fail(response.status, error, 'could not load the campaigns')
    return data
  },
})

/** Only the archived campaigns, newest first (#345). */
export const archivedCampaignsQuery = queryOptions({
  queryKey: campaignKeys.archived(),
  queryFn: async ({ signal }): Promise<CampaignSummary[]> => {
    const { data, error, response } = await api.GET('/api/v1/campaigns', {
      params: { query: { archived: true } },
      signal,
    })
    if (data === undefined) fail(response.status, error, 'could not load the archived campaigns')
    return data
  },
})

/**
 * What deleting the campaign would remove, the Gmail drafts it would leave, or why it
 * is refused (#345). Changes nothing.
 */
export function deletePlanQuery(id: number) {
  return queryOptions({
    queryKey: campaignKeys.deletePlan(id),
    queryFn: async ({ signal }): Promise<DeletePlan> => {
      const { data, error, response } = await api.GET(
        '/api/v1/campaigns/{campaign_id}/delete-plan',
        { params: { path: { campaign_id: id } }, signal },
      )
      if (data === undefined) fail(response.status, error, 'could not check the delete')
      return data
    },
  })
}

export function campaignQuery(id: number) {
  return queryOptions({
    queryKey: campaignKeys.one(id),
    queryFn: async ({ signal }): Promise<Campaign> => {
      const { data, error, response } = await api.GET('/api/v1/campaigns/{campaign_id}', {
        params: { path: { campaign_id: id } },
        signal,
      })
      if (data === undefined) fail(response.status, error, 'could not load the campaign')
      return data
    },
  })
}

/**
 * What the campaign has done (#350): sends per day in your time zone, from the first
 * send to today, and replies, bounces and opt-outs per step, with the totals.
 */
export function campaignResultsQuery(id: number) {
  return queryOptions({
    queryKey: campaignKeys.results(id),
    queryFn: async ({ signal }): Promise<CampaignResults> => {
      const { data, error, response } = await api.GET('/api/v1/campaigns/{campaign_id}/results', {
        params: { path: { campaign_id: id } },
        signal,
      })
      if (data === undefined) fail(response.status, error, 'could not load the results')
      return data
    },
  })
}

export function reviewQuery(id: number) {
  return queryOptions({
    queryKey: campaignKeys.review(id),
    queryFn: async ({ signal }): Promise<Review> => {
      const { data, error, response } = await api.GET('/api/v1/campaigns/{campaign_id}/review', {
        params: { path: { campaign_id: id } },
        signal,
      })
      if (data === undefined) fail(response.status, error, 'could not load the review')
      return data
    },
  })
}

/**
 * The guard summary's details, on demand (#346): each contact the guards skip, with every
 * reason. Informational: activation does not wait on it.
 */
export function guardDetailsQuery(id: number) {
  return queryOptions({
    queryKey: campaignKeys.guards(id),
    queryFn: async ({ signal }): Promise<GuardDetails> => {
      const { data, error, response } = await api.GET(
        '/api/v1/campaigns/{campaign_id}/review/guards',
        { params: { path: { campaign_id: id } }, signal },
      )
      if (data === undefined) fail(response.status, error, 'could not load the skipped contacts')
      return data
    },
  })
}

export const ENROLLMENT_PAGE = 25

export function enrollmentsQuery(
  id: number,
  q: string,
  status: EnrollmentStatus | '',
  offset = 0,
  limit = ENROLLMENT_PAGE,
) {
  return queryOptions({
    queryKey: [...campaignKeys.enrollments(id, q, status, offset), limit] as const,
    queryFn: async ({ signal }): Promise<EnrollmentPage> => {
      const { data, error, response } = await api.GET(
        '/api/v1/campaigns/{campaign_id}/enrollments',
        {
          params: {
            path: { campaign_id: id },
            query: { q, limit, offset, ...(status === '' ? {} : { status }) },
          },
          signal,
        },
      )
      if (data === undefined) fail(response.status, error, 'could not load the enrollments')
      return data
    },
  })
}

export async function createCampaign(body: CampaignCreate): Promise<Campaign> {
  const { data, error, response } = await api.POST('/api/v1/campaigns', { body })
  if (data === undefined) fail(response.status, error, 'could not create the campaign')
  return data
}

/** Enroll through the guards. A `list_id` or `filter` replaces the source first (a draft only). */
export async function enroll(id: number, body: EnrollIn): Promise<EnrollOut> {
  const { data, error, response } = await api.POST('/api/v1/campaigns/{campaign_id}/enroll', {
    params: { path: { campaign_id: id } },
    body,
  })
  if (data === undefined) fail(response.status, error, 'could not enroll the audience')
  return data
}

export async function pauseCampaign(id: number): Promise<Campaign> {
  const { data, error, response } = await api.POST('/api/v1/campaigns/{campaign_id}/pause', {
    params: { path: { campaign_id: id } },
  })
  if (data === undefined) fail(response.status, error, 'could not pause the campaign')
  return data
}

export async function resumeCampaign(id: number): Promise<Campaign> {
  const { data, error, response } = await api.POST('/api/v1/campaigns/{campaign_id}/resume', {
    params: { path: { campaign_id: id } },
  })
  if (data === undefined) fail(response.status, error, 'could not resume the campaign')
  return data
}

/** End an active or paused campaign for good (#345): nothing fires again. */
export async function endCampaign(id: number): Promise<Campaign> {
  const { data, error, response } = await api.POST('/api/v1/campaigns/{campaign_id}/end', {
    params: { path: { campaign_id: id } },
  })
  if (data === undefined) fail(response.status, error, 'could not end the campaign')
  return data
}

/** Hide a concluded campaign from the list and the dashboard, keeping its history (#345). */
export async function archiveCampaign(id: number): Promise<Campaign> {
  const { data, error, response } = await api.POST('/api/v1/campaigns/{campaign_id}/archive', {
    params: { path: { campaign_id: id } },
  })
  if (data === undefined) fail(response.status, error, 'could not archive the campaign')
  return data
}

export async function unarchiveCampaign(id: number): Promise<Campaign> {
  const { data, error, response } = await api.POST('/api/v1/campaigns/{campaign_id}/unarchive', {
    params: { path: { campaign_id: id } },
  })
  if (data === undefined) fail(response.status, error, 'could not unarchive the campaign')
  return data
}

/**
 * Delete a campaign never activated and with no messages (#345). 409 for any other.
 * The answer lists the Gmail drafts left behind: netkeeper never deletes one.
 */
export async function deleteCampaign(id: number): Promise<DeletePlan> {
  const { data, error, response } = await api.DELETE('/api/v1/campaigns/{campaign_id}', {
    params: { path: { campaign_id: id } },
  })
  if (data === undefined) fail(response.status, error, 'could not delete the campaign')
  return data
}

export async function startReview(id: number): Promise<Review> {
  const { data, error, response } = await api.POST('/api/v1/campaigns/{campaign_id}/review/start', {
    params: { path: { campaign_id: id } },
  })
  if (data === undefined) fail(response.status, error, 'could not start the review')
  return data
}

/** How many of a step's messages one request answers: the pager fetches a page at a time. */
export const STEP_PAGE = 20

/**
 * One step's review: a page of its rendered messages (from `offset`), every blocked
 * message, and the `fingerprint` an approval of the step is given for.
 */
export async function reviewStep(id: number, stepId: number, offset: number): Promise<StepReview> {
  const { data, error, response } = await api.GET(
    '/api/v1/campaigns/{campaign_id}/review/steps/{step_id}',
    {
      params: {
        path: { campaign_id: id, step_id: stepId },
        query: { offset, limit: STEP_PAGE },
      },
    },
  )
  if (data === undefined) fail(response.status, error, 'could not render the step')
  return data
}

export const stepReviewQuery = (id: number, stepId: number, offset: number) =>
  queryOptions({
    queryKey: campaignKeys.step(id, stepId, offset),
    queryFn: () => reviewStep(id, stepId, offset),
    placeholderData: keepPreviousData,
  })

/**
 * Approve every message of a step at once, for the `fingerprint` its review came with.
 * It covers messages rendered later too, until the step or its template changes; never
 * a blocked one. A 409 with `code: stale` means the step changed since it was shown.
 */
export async function approveStep(
  id: number,
  stepId: number,
  fingerprint: string,
): Promise<Review> {
  const { data, error, response } = await api.POST(
    '/api/v1/campaigns/{campaign_id}/review/steps/{step_id}/approve',
    { params: { path: { campaign_id: id, step_id: stepId } }, body: { fingerprint } },
  )
  if (data === undefined) fail(response.status, error, 'could not approve the step')
  return data
}

/** Approve messages of a step that uses `{{ personal_line }}` one by one. */
export async function approveMessages(
  id: number,
  stepId: number,
  messages: ReadonlyArray<Pick<MessagePreview, 'enrollment_id' | 'fingerprint'>>,
): Promise<Review> {
  const { data, error, response } = await api.POST(
    '/api/v1/campaigns/{campaign_id}/review/steps/{step_id}/messages/approve',
    {
      params: { path: { campaign_id: id, step_id: stepId } },
      body: {
        messages: messages.map((m) => ({
          enrollment_id: m.enrollment_id,
          fingerprint: m.fingerprint,
        })),
      },
    },
  )
  if (data === undefined) fail(response.status, error, 'could not approve the message')
  return data
}

export async function lintCampaign(id: number): Promise<LintResult> {
  const { data, error, response } = await api.POST('/api/v1/campaigns/{campaign_id}/review/lint', {
    params: { path: { campaign_id: id } },
  })
  if (data === undefined) fail(response.status, error, 'could not lint the campaign')
  return data
}

/**
 * Test one email step, addressed to the campaign mailbox's own address: a draft in its
 * Drafts while Gmail is armed for drafts, sent once armed to send. Refused (409) when disarmed.
 */
export async function testSend(id: number, stepId: number): Promise<TestSend> {
  const { data, error, response } = await api.POST(
    '/api/v1/campaigns/{campaign_id}/review/test-send',
    { params: { path: { campaign_id: id } }, body: { step_id: stepId } },
  )
  if (data === undefined) fail(response.status, error, 'could not send the test')
  return data
}

/**
 * The gate: 409 with `missing` unless every requirement is met. `startsAt` is the
 * scheduled start, an ISO time with its zone: nothing is sent before it (#338). Null
 * leaves it to the backend's default, the next Tuesday at 09:00.
 */
export async function activateCampaign(id: number, startsAt: string | null): Promise<Review> {
  const { data, error, response } = await api.POST('/api/v1/campaigns/{campaign_id}/activate', {
    params: { path: { campaign_id: id } },
    // Left out, the backend's default: the next Tuesday at 09:00, never "now".
    body: startsAt === null ? {} : { starts_at: startsAt },
  })
  if (data === undefined) fail(response.status, error, 'could not activate the campaign')
  return data
}

/** Move an active or paused campaign's start. 409 once it has sent anything. */
export async function setCampaignStart(id: number, startsAt: string): Promise<Campaign> {
  const { data, error, response } = await api.PUT('/api/v1/campaigns/{campaign_id}/start', {
    params: { path: { campaign_id: id } },
    body: { starts_at: startsAt },
  })
  if (data === undefined) fail(response.status, error, 'could not change the start')
  return data
}

/** A step's day offset and time of day; `sendTime` null aims for the next suggested slot. */
export async function setStepSchedule(
  id: number,
  stepId: number,
  delayDays: number,
  sendTime: string | null,
): Promise<Campaign> {
  const { data, error, response } = await api.PUT(
    '/api/v1/campaigns/{campaign_id}/steps/{step_id}/schedule',
    {
      params: { path: { campaign_id: id, step_id: stepId } },
      body: { delay_days: delayDays, send_time: sendTime },
    },
  )
  if (data === undefined) fail(response.status, error, 'could not change the step timing')
  return data
}
