import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

import { FirstPollNote } from '@/features/linkedin-steps/first-poll-note'
import { useRunGoing } from '@/features/linkedin-steps/use-prefill-run'

import { linkedinKeys, runQuery, startRun } from './api'
import { stopReasonLabel } from './fields'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * **Check inbox now** (#383): a manual LinkedIn inbox poll, which finds replies and
 * the prefilled messages you sent. It follows every rule a manual run does (active
 * hours, the session flag, heat, one run at a time) and spends one inbox poll from
 * today's budget. The poll's run shows in Runs, with how it ended in plain words.
 */
export function InboxCheckCard({ onStarted }: { onStarted: (runId: number) => void }) {
  const queryClient = useQueryClient()
  const start = useMutation({
    mutationFn: () => startRun('inbox', null),
    onSuccess: (accepted) => {
      void queryClient.invalidateQueries({ queryKey: linkedinKeys.runs() })
      void queryClient.invalidateQueries({ queryKey: linkedinKeys.status() })
      onStarted(accepted.run_id)
    },
  })

  // Off while the poll it started runs: one click, one poll.
  const checking = useRunGoing(start.data?.run_id ?? null)

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>LinkedIn inbox</CardTitle>
        <CardDescription>
          Look for replies, and for the prefilled messages you sent, now instead of at the next
          poll. It spends one inbox poll from today&apos;s budget.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        <FirstPollNote />
        <Button disabled={start.isPending || checking} onClick={() => start.mutate()}>
          {start.isPending ? 'Starting…' : 'Check inbox now'}
        </Button>
        {start.isSuccess && <InboxRunOutcome runId={start.data.run_id} />}
        {start.isError && <p role="alert">The inbox check did not start: {message(start.error)}</p>}
      </CardContent>
    </Card>
  )
}

function InboxRunOutcome({ runId }: { runId: number }) {
  const run = useQuery(runQuery(runId))
  if (!run.isSuccess || run.data.status === 'running') {
    return <p role="status">Checking the LinkedIn inbox…</p>
  }
  if (run.data.status === 'completed') return <p role="status">The inbox check finished.</p>
  return (
    <p role="status">
      The inbox check stopped: {stopReasonLabel(run.data) ?? run.data.error ?? 'no reason recorded'}
      .
    </p>
  )
}
