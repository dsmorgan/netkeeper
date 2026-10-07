import { useQueryClient } from '@tanstack/react-query'
import { useCallback } from 'react'

import { useServerEvent } from '@/features/events/use-server-event'
import { mailboxKeys } from '@/features/mailboxes/api'

import { gmailKeys } from './api'
import { ConnectionCard } from './connection-card'
import { PostureCard } from './posture-card'
import { RecentCard } from './recent-card'
import { ReplyPollCard } from './reply-poll-card'
import { SendsCard } from './sends-card'

/**
 * The Gmail page (#449), laid out like the LinkedIn page: the connection and today's
 * sends, the reply poll and the posture rows that concern Gmail, then recent email.
 * Everything on it is read from what netkeeper already stored; no request here calls
 * Gmail. The app-wide banner still announces a mailbox that needs signing in again.
 */
export function GmailPage() {
  const queryClient = useQueryClient()
  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: mailboxKeys.all })
    void queryClient.invalidateQueries({ queryKey: gmailKeys.all })
  }, [queryClient])
  useServerEvent('mailbox.status', refresh)

  return (
    <div className="flex max-w-6xl flex-col gap-4">
      <h1 className="sr-only">Gmail</h1>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <ConnectionCard />
        <SendsCard />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <ReplyPollCard />
        <PostureCard />
      </div>

      <RecentCard />
    </div>
  )
}
