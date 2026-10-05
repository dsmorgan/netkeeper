/**
 * When each background check last ran and runs next (`GET /poll-status`, #401).
 *
 * Read-only: fetching it never starts a check. The checks move on their own, with
 * no server event for most of them, so this refetches every minute, and at once
 * when a LinkedIn run ends or a mailbox's status changes (`usePollStatusRefresh`).
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
  refetchInterval: 60_000,
  retry: false,
})

/** Refetch the poll status when a LinkedIn run ends or a mailbox's status changes. */
export function usePollStatusRefresh(): void {
  const queryClient = useQueryClient()
  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: pollStatusKeys.all })
  }, [queryClient])
  useServerEvent('run.finished', refresh)
  useServerEvent('mailbox.status', refresh)
}
