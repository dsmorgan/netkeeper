import { useQuery } from '@tanstack/react-query'

import { pollStatusQuery } from './api'
import { CheckRepliesNow } from './check-now'
import { canCheckNow, repliesText } from './format'
import { useNow } from './use-now'

/**
 * The campaign page's line beside the reply counts (#401): when the campaign's
 * mailbox was last polled for replies, so a count that has not moved yet reads as
 * "not checked since", not "nobody replied". Shares the header's poll status query.
 * While the mailbox's replies are polled, "Check now" (#409) beside it asks for the
 * next poll at the next tick; it polls every armed mailbox, as each poll does.
 */
export function RepliesChecked({ mailboxId }: { mailboxId: number | null }) {
  const status = useQuery(pollStatusQuery)
  const now = useNow()
  if (mailboxId === null || status.data === undefined) return null
  const poll = status.data.mailboxes.find((mailbox) => mailbox.mailbox_id === mailboxId)
  if (poll === undefined) return null
  const check = status.data.items.find((item) => item.key === 'gmail_replies')
  const text = <p className="text-muted-foreground">{repliesText(poll, now)}</p>
  if (check === undefined || !canCheckNow(poll)) return text
  return (
    <div className="flex flex-wrap items-start justify-between gap-2">
      {text}
      <CheckRepliesNow check={check} />
    </div>
  )
}
