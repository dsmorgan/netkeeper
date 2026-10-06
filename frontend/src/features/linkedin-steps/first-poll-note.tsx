import { useQuery } from '@tanstack/react-query'

import { pollStatusQuery } from '@/features/poll-status/api'

import { FIRST_POLL_COMMAND, FIRST_POLL_NOTE } from './prefill-copy'

/**
 * Before the first inbox poll has completed, LinkedIn prefills are held until a person
 * runs one by hand (#417, #433). Read from the poll status; nothing shows when it can't
 * be read, or once a poll has completed.
 */
export function FirstPollNote() {
  const status = useQuery(pollStatusQuery)
  const inbox = status.data?.items.find((check) => check.key === 'linkedin_inbox')
  if (inbox === undefined || inbox.last_at !== null) return null
  return (
    <p role="note" className="rounded-md border border-amber-500/60 bg-amber-500/10 px-2 py-1">
      {FIRST_POLL_NOTE.split(FIRST_POLL_COMMAND).flatMap((part, index) =>
        index === 0
          ? [part]
          : [
              <code key={index} className="rounded bg-muted px-1 font-mono text-xs">
                {FIRST_POLL_COMMAND}
              </code>,
              part,
            ],
      )}
    </p>
  )
}
