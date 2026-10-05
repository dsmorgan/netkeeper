/**
 * When each background check last ran and runs next (`GET /poll-status`, #401).
 *
 * Read-only: fetching it never starts a check. The checks move on their own, with
 * no server event for most of them, so this refetches every minute, and at once
 * when a LinkedIn run ends or a mailbox's status changes (`usePollStatusRefresh`).
 * While a "Check now" waits for its poll it refetches every 15 seconds, so the new
 * last-checked time shows soon after the poll runs.
 */
import { queryOptions, useQueryClient } from '@tanstack/react-query'
import { useCallback } from 'react'

import { api } from '@/api/client'
import type { components } from '@/api/schema'
import { useServerEvent } from '@/features/events/use-server-event'

export type PollStatus = components['schemas']['PollStatusOut']
export type PollCheck = components['schemas']['PollCheckOut']
export type MailboxPoll = components['schemas']['MailboxPollOut']
export type CheckState = components['schemas']['CheckState']

export const pollStatusKeys = {
  all: ['poll-status'] as const,
}

export const pollStatusQuery = queryOptions({
  queryKey: pollStatusKeys.all,
  queryFn: async ({ signal }): Promise<PollStatus> => {
    const { data, response } = await api.GET('/api/v1/poll-status', { signal })
    if (data === undefined) {
      throw new Error(`GET /api/v1/poll-status returned ${response.status}`)
    }
    return data
  },
  refetchInterval: (query) =>
    query.state.data?.items.some((check) => check.requested) === true ? 15_000 : 60_000,
  retry: false,
})

/**
 * "Check now" for Gmail replies (#409): asks `netkeeper serve` to poll at its next
 * campaign tick, within a minute. It never reads Gmail itself, and a second press
 * before that tick joins the first. A refusal (not running, disarmed, not
 * connected, every mailbox needing sign-in) throws its reason.
 */
export async function checkRepliesNow(): Promise<void> {
  const { error, response } = await api.POST('/api/v1/poll-status/gmail-replies/check-now')
  if (response.ok) return
  const detail = (error as { detail?: unknown } | undefined)?.detail
  throw new Error(
    typeof detail === 'string' && detail !== ''
      ? detail
      : `POST /api/v1/poll-status/gmail-replies/check-now returned ${response.status}`,
  )
}

/** Refetch the poll status when a LinkedIn run ends or a mailbox's status changes. */
export function usePollStatusRefresh(): void {
  const queryClient = useQueryClient()
  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: pollStatusKeys.all })
  }, [queryClient])
  useServerEvent('run.finished', refresh)
  useServerEvent('mailbox.status', refresh)
}
