/**
 * What the Gmail page reads that no other endpoint answers (`GET /gmail/activity`,
 * #449): today's sends against each mailbox's cap, and recent email.
 *
 * Read-only, from the database alone: fetching it never calls Gmail. The sends move
 * with the campaign tick and no server event announces them, so this refetches every
 * minute, like the poll status, and at once when a mailbox's status changes.
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import { detailMessage } from '@/api/errors'
import type { components } from '@/api/schema'

export type GmailActivity = components['schemas']['GmailActivityOut']
export type MailboxSends = components['schemas']['MailboxSendsOut']
export type RecentMessage = components['schemas']['RecentMessageOut']

export const gmailKeys = {
  all: ['gmail'] as const,
  activity: () => [...gmailKeys.all, 'activity'] as const,
}

export const gmailActivityQuery = queryOptions({
  queryKey: gmailKeys.activity(),
  queryFn: async ({ signal }): Promise<GmailActivity> => {
    const { data, error, response } = await api.GET('/api/v1/gmail/activity', { signal })
    if (data === undefined) {
      throw new Error(
        detailMessage(error) ?? `GET /api/v1/gmail/activity returned ${response.status}`,
      )
    }
    return data
  },
  refetchInterval: 60_000,
})

/**
 * The posture rows that concern Gmail and campaign sending, by name. The rest of the
 * report is the LinkedIn extractor's, and stays on Settings, which shows every row.
 */
export const GMAIL_POSTURE_ROWS: readonly string[] = [
  'reply poll',
  'sending hours',
  'next campaign send',
  'campaign templates',
]
