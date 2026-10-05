import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useId } from 'react'

import { Button } from '@/components/ui/button'

import { checkRepliesNow, pollStatusKeys, type PollCheck } from './api'
import { canCheckNow, checkNowLate } from './format'
import { useNow } from './use-now'

/**
 * "Check now" for Gmail replies (#409), in the poll-status popover and beside the
 * campaign page's "replies checked" line.
 *
 * Disabled while the request is sending and until the poll status has refetched,
 * then while the server says the check waits for the next tick. The tick clears
 * that whether the poll succeeds or fails; a request still waiting after
 * `LATE_AFTER_MS` says so and enables the button again, so it never stays stuck.
 */
export function CheckRepliesNow({ check }: { check: PollCheck }) {
  const queryClient = useQueryClient()
  const statusId = useId()
  const now = useNow(15_000)
  const request = useMutation({
    mutationFn: checkRepliesNow,
    // Returned, so the button stays pending until the refetch settles, either way.
    onSettled: () => queryClient.invalidateQueries({ queryKey: pollStatusKeys.all }),
  })
  const late = checkNowLate(check, now)
  const waiting = check.requested && !late
  const disabled = !canCheckNow(check) || request.isPending || waiting
  return (
    <span className="flex flex-col items-start gap-1">
      <Button
        size="sm"
        variant="outline"
        disabled={disabled}
        onClick={() => request.mutate()}
        aria-describedby={check.requested ? statusId : undefined}
      >
        Check now
      </Button>
      {check.requested && (
        <span id={statusId} role="status" className="text-xs text-muted-foreground">
          {late ? 'The check hasn’t run yet' : 'Checking within a minute'}
        </span>
      )}
      {request.isError && (
        <span role="alert" className="text-xs text-destructive">
          {request.error.message}
        </span>
      )}
    </span>
  )
}
