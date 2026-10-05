import { useQuery } from '@tanstack/react-query'

import { pollStatusQuery } from './api'
import { repliesText } from './format'
import { useNow } from './use-now'

/**
 * The campaign page's line beside the reply counts (#401): when the campaign's
 * mailbox was last polled for replies, so a count that has not moved yet reads as
 * "not checked since", not "nobody replied". Shares the header's poll status query.
 */
export function RepliesChecked({ mailboxId }: { mailboxId: number | null }) {
  const status = useQuery(pollStatusQuery)
  const now = useNow()
  if (mailboxId === null || status.data === undefined) return null
  const poll = status.data.mailboxes.find((mailbox) => mailbox.mailbox_id === mailboxId)
  if (poll === undefined) return null
  return <p className="text-muted-foreground">{repliesText(poll, now)}</p>
}
