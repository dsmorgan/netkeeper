/**
 * Every call the campaign screens make, through the generated client (P3-11a).
 *
 * `/campaigns` is P3-13's (create, enroll, list, status, pause, resume) and the
 * review gate under `/campaigns/{id}/review/...` plus `POST .../activate` is
 * P3-09's (spec 11.8). Nothing here activates a campaign except `activate`,
 * which is the gate itself: it answers 409 with `missing` unless every
 * requirement is recorded and current.
 */
import { queryOptions } from '@tanstack/react-query'

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
export type Previews = Schemas['PreviewsOut']
export type EnrollmentPreview = Schemas['EnrollmentPreviewOut']
export type LintResult = Schemas['LintOut']
export type TestSend = Schemas['TestSendOut']
export type Enrollment = Schemas['EnrollmentOut']
export type EnrollmentPage = Schemas['EnrollmentPageOut']

/** A request the backend refused: its status, its sentence, and `missing` when it sent one. */
export class CampaignApiError extends Error {
  readonly status: number
  readonly missing: Missing[] | null

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
  one: (id: number) => [...campaignKeys.all, 'one', id] as const,
  review: (id: number) => [...campaignKeys.all, 'review', id] as const,
  enrollments: (id: number, q: string, status: string, offset: number) =>
    [...campaignKeys.all, 'enrollments', id, q, status, offset] as const,
}

export const campaignsQuery = queryOptions({
  queryKey: campaignKeys.list(),
  queryFn: async ({ signal }): Promise<CampaignSummary[]> => {
    const { data, error, response } = await api.GET('/api/v1/campaigns', { signal })
    if (data === undefined) fail(response.status, error, 'could not load the campaigns')
    return data
  },
})

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

export async function startReview(id: number): Promise<Review> {
  const { data, error, response } = await api.POST('/api/v1/campaigns/{campaign_id}/review/start', {
    params: { path: { campaign_id: id } },
  })
  if (data === undefined) fail(response.status, error, 'could not start the review')
  return data
}

/** The sample of up to 10: the same draw while the audience is unchanged. */
export async function samplePreviews(id: number): Promise<Previews> {
  const { data, error, response } = await api.POST(
    '/api/v1/campaigns/{campaign_id}/review/sample',
    { params: { path: { campaign_id: id } } },
  )
  if (data === undefined) fail(response.status, error, 'could not draw the sample')
  return data
}

/** Render enrollments you looked up. Each one viewed must then be approved too. */
export async function viewPreviews(id: number, enrollmentIds: number[]): Promise<Previews> {
  const { data, error, response } = await api.POST(
    '/api/v1/campaigns/{campaign_id}/review/previews',
    { params: { path: { campaign_id: id } }, body: { enrollment_ids: enrollmentIds } },
  )
  if (data === undefined) fail(response.status, error, 'could not render the previews')
  return data
}

/** Approve previews for the fingerprint each was shown with. */
export async function approvePreviews(
  id: number,
  previews: ReadonlyArray<Pick<EnrollmentPreview, 'enrollment_id' | 'fingerprint'>>,
): Promise<Review> {
  const { data, error, response } = await api.POST(
    '/api/v1/campaigns/{campaign_id}/review/approve',
    {
      params: { path: { campaign_id: id } },
      body: {
        previews: previews.map((p) => ({
          enrollment_id: p.enrollment_id,
          fingerprint: p.fingerprint,
        })),
      },
    },
  )
  if (data === undefined) fail(response.status, error, 'could not approve the previews')
  return data
}

export async function lintCampaign(id: number): Promise<LintResult> {
  const { data, error, response } = await api.POST('/api/v1/campaigns/{campaign_id}/review/lint', {
    params: { path: { campaign_id: id } },
  })
  if (data === undefined) fail(response.status, error, 'could not lint the campaign')
  return data
}

/** Send one email step to the campaign mailbox's own address. Refused (409) unless armed for send. */
export async function testSend(id: number, stepId: number): Promise<TestSend> {
  const { data, error, response } = await api.POST(
    '/api/v1/campaigns/{campaign_id}/review/test-send',
    { params: { path: { campaign_id: id } }, body: { step_id: stepId } },
  )
  if (data === undefined) fail(response.status, error, 'could not send the test')
  return data
}

/** Acknowledge the guard summary exactly as it was shown, for the audience it was shown for. */
export async function acknowledgeGuards(id: number, review: Review): Promise<Review> {
  const { data, error, response } = await api.POST(
    '/api/v1/campaigns/{campaign_id}/review/guards/acknowledge',
    {
      params: { path: { campaign_id: id } },
      body: { summary: review.guard_summary, audience_fingerprint: review.audience_fingerprint },
    },
  )
  if (data === undefined) fail(response.status, error, 'could not acknowledge the guards')
  return data
}

/** The gate: 409 with `missing` unless every requirement is met. */
export async function activateCampaign(id: number): Promise<Review> {
  const { data, error, response } = await api.POST('/api/v1/campaigns/{campaign_id}/activate', {
    params: { path: { campaign_id: id } },
  })
  if (data === undefined) fail(response.status, error, 'could not activate the campaign')
  return data
}
