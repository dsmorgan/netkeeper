/**
 * A campaign's LinkedIn steps, prefilled one at a time while you watch Chrome
 * (spec 11.6; P4-09's `/campaigns/linkedin` API, #383's screens).
 *
 * Nothing here prefills on its own: `prefill` is called only from a button a
 * person clicks. `checkSent` asks for an inbox poll, and `discard` lets the step
 * count as fired without sending it. No call here returns message text.
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import { detailMessage } from '@/api/errors'
import type { components } from '@/api/schema'

type Schemas = components['schemas']

export type ReadyItem = Schemas['ReadyOut']
export type TryAgainItem = Schemas['TryAgainOut']
export type LastTry = Schemas['LastTryOut']
export type ReadyPage = Schemas['ReadyPage']
export type WaitingItem = Schemas['WaitingOut']
export type WaitingPage = Schemas['WaitingPage']
export type PrefillAccepted = Schemas['PrefillAccepted']
export type PrefillRefusal = Schemas['PrefillRefused']
export type Discarded = Schemas['DiscardedOut']
export type StepOptions = Schemas['OptionsOut']
export type RunAccepted = Schemas['RunAccepted']
export type AutoSendResumed = Schemas['AutoSendResumed']

/**
 * A refused request: its status and sentence, and for a refused prefill (`409`) the
 * reasons the backend gave. A refused prefill typed nothing.
 */
export class LinkedInStepError extends Error {
  readonly status: number
  readonly refusal: PrefillRefusal | null

  constructor(status: number, body: unknown, fallback: string) {
    const refusal = refusalOf(body)
    super(
      refusal !== null
        ? (refusal.detail ?? 'The prefill was refused.')
        : (detailMessage(body) ?? `${fallback} (HTTP ${status})`),
    )
    this.name = 'LinkedInStepError'
    this.status = status
    this.refusal = refusal
  }
}

/** The `409` body of a refused prefill: `{"detail": {enrollment_id, reasons, detail}}`. */
function refusalOf(body: unknown): PrefillRefusal | null {
  if (body === null || typeof body !== 'object' || !('detail' in body)) return null
  const detail = (body as { detail: unknown }).detail
  if (detail === null || typeof detail !== 'object' || !('reasons' in detail)) return null
  const { reasons } = detail as { reasons: unknown }
  if (!Array.isArray(reasons)) return null
  const shape = detail as Partial<PrefillRefusal>
  return {
    enrollment_id: typeof shape.enrollment_id === 'number' ? shape.enrollment_id : null,
    reasons: reasons.filter((r): r is string => typeof r === 'string'),
    detail: typeof shape.detail === 'string' ? shape.detail : null,
  }
}

function fail(status: number, body: unknown, fallback: string): never {
  throw new LinkedInStepError(status, body, fallback)
}

export const linkedinStepKeys = {
  all: ['linkedin-steps'] as const,
  ready: (campaignId: number | null) => [...linkedinStepKeys.all, 'ready', campaignId] as const,
  waiting: (campaignId: number | null) => [...linkedinStepKeys.all, 'waiting', campaignId] as const,
  options: () => [...linkedinStepKeys.all, 'options'] as const,
}

/** The most rows one list answers (`READY_PAGE_MAX`). */
export const QUEUE_PAGE = 50

/** Due LinkedIn steps, oldest first; one campaign's when `campaignId` is given. */
export function readyQuery(campaignId: number | null = null) {
  return queryOptions({
    queryKey: linkedinStepKeys.ready(campaignId),
    queryFn: async ({ signal }): Promise<ReadyPage> => {
      const { data, error, response } = await api.GET('/api/v1/campaigns/linkedin/ready', {
        params: {
          query: { limit: QUEUE_PAGE, ...(campaignId === null ? {} : { campaign_id: campaignId }) },
        },
        signal,
      })
      if (data === undefined) fail(response.status, error, 'could not load the LinkedIn queue')
      return data
    },
  })
}

/**
 * Prefilled, stale, and interrupted LinkedIn messages: each waits for you. Every
 * campaign's by default: the one-open-prefill rule is per user, not per campaign.
 */
export function waitingQuery(campaignId: number | null = null) {
  return queryOptions({
    queryKey: linkedinStepKeys.waiting(campaignId),
    queryFn: async ({ signal }): Promise<WaitingPage> => {
      const { data, error, response } = await api.GET('/api/v1/campaigns/linkedin/waiting', {
        params: {
          query: { limit: QUEUE_PAGE, ...(campaignId === null ? {} : { campaign_id: campaignId }) },
        },
        signal,
      })
      if (data === undefined) fail(response.status, error, 'could not load what waits for you')
      return data
    },
  })
}

/** Whether a LinkedIn step may be `auto_send`: the config flag, off by default. */
export const stepOptionsQuery = queryOptions({
  queryKey: linkedinStepKeys.options(),
  queryFn: async ({ signal }): Promise<StepOptions> => {
    const { data, error, response } = await api.GET('/api/v1/campaigns/linkedin/options', {
      signal,
    })
    if (data === undefined) fail(response.status, error, 'could not load the LinkedIn options')
    return data
  },
  staleTime: 60_000,
})

/** One enrollment's step, or Try again on one whose latest prefill typed nothing (#445). */
export type PrefillTarget =
  | { enrollmentId: number; retry?: false }
  | { enrollmentId: number; retry: true; noBubbleOpen: boolean }

/**
 * Claim one LinkedIn step and start its prefill: `202` with the run, before any
 * browser work. A refusal throws a {@link LinkedInStepError} with its reasons. Try again
 * (`retry`) is always one enrollment; `noBubbleOpen` says the person checked Chrome.
 */
export async function prefill(target: PrefillTarget | 'next'): Promise<PrefillAccepted> {
  const { data, error, response } = await api.POST('/api/v1/campaigns/linkedin/prefill', {
    body:
      target === 'next'
        ? { next: true, retry: false, no_bubble_open: false }
        : target.retry === true
          ? {
              enrollment_id: target.enrollmentId,
              next: false,
              retry: true,
              no_bubble_open: target.noBubbleOpen,
            }
          : {
              enrollment_id: target.enrollmentId,
              next: false,
              retry: false,
              no_bubble_open: false,
            },
  })
  if (data === undefined) fail(response.status, error, 'could not start the prefill')
  return data
}

/** "I sent it, check now": a manual inbox poll that finds the message sent. */
export async function checkSent(messageId: number): Promise<RunAccepted> {
  const { data, error, response } = await api.POST(
    '/api/v1/campaigns/linkedin/messages/{message_id}/check',
    { params: { path: { message_id: messageId } } },
  )
  if (data === undefined) fail(response.status, error, 'could not start the inbox check')
  return data
}

/**
 * You closed the message bubbles in Chrome: auto-send may go again (ADR 0008). It
 * changes nothing in LinkedIn.
 */
export async function resumeAutoSend(): Promise<AutoSendResumed> {
  const { data, error, response } = await api.POST('/api/v1/campaigns/linkedin/auto-send/resume', {
    body: { confirm: true },
  })
  if (data === undefined) fail(response.status, error, 'could not resume auto-send')
  return data
}

/** You will not send it: the step counts as fired, and the enrollment moves on. */
export async function discard(messageId: number): Promise<Discarded> {
  const { data, error, response } = await api.POST(
    '/api/v1/campaigns/linkedin/messages/{message_id}/discard',
    { params: { path: { message_id: messageId } } },
  )
  if (data === undefined) fail(response.status, error, 'could not discard the message')
  return data
}
