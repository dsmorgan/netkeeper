import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { campaignKeys } from '@/features/campaigns/api'
import { useServerEvent } from '@/features/events/use-server-event'
import { linkedinKeys, runQuery, statusQuery } from '@/features/linkedin/api'

import { linkedinStepKeys } from './api'

interface RunEvent {
  run_id: number
  kind?: string
  status?: string
}

type RunProgress = { run_id: number } & Record<string, unknown>

export interface PrefillRun {
  /** The `message_send` run going now, if any: a prefill typing in Chrome. */
  runningId: number | null
  /** Its latest progress, as `run.progress` sent it. Never message text (spec 11.6). */
  progress: Record<string, unknown> | null
  /** The last prefill run that ended while this page was open, to say how it ended. */
  finishedId: number | null
  /** Starts watching a run the page just submitted, before the status says so. */
  watch: (runId: number) => void
  /** Forgets the finished run's outcome once a person has read it. */
  dismiss: () => void
}

/**
 * The prefill going now, from the LinkedIn status (`running_run_id`, whose run is a
 * `message_send`), and its live progress over the app's one SSE stream. A run's start
 * or end refreshes the queue, what waits for you, and the campaigns, since any of them
 * may have moved.
 */
export function usePrefillRun(): PrefillRun {
  const queryClient = useQueryClient()
  const status = useQuery(statusQuery)
  const [submitted, setSubmitted] = useState<number | null>(null)
  const [progress, setProgress] = useState<Record<string, unknown> | null>(null)
  const [finishedId, setFinishedId] = useState<number | null>(null)

  const statusRunning = status.data?.running_run_id ?? null
  const candidate = statusRunning ?? submitted
  const run = useQuery({ ...runQuery(candidate ?? 0), enabled: candidate !== null })
  const runningId =
    candidate === null
      ? null
      : run.data === undefined
        ? // Just submitted, not read back yet: it is ours.
          candidate === submitted
          ? candidate
          : null
        : run.data.kind === 'message_send' && run.data.status === 'running'
          ? candidate
          : null
  // Ended before any event reached this page (a missed event, a fast refusal).
  const submittedEnded =
    submitted !== null && run.data?.id === submitted && run.data.status !== 'running'

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: linkedinStepKeys.all })
    void queryClient.invalidateQueries({ queryKey: campaignKeys.all })
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.status() })
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.runs() })
  }

  useServerEvent<RunEvent>('run.started', () => {
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.status() })
  })
  useServerEvent<RunProgress>('run.progress', ({ run_id, ...fields }) => {
    if (run_id === candidate) setProgress(fields)
  })
  useServerEvent<RunEvent>('run.finished', ({ run_id }) => {
    if (run_id === submitted || run_id === runningId) {
      setFinishedId(run_id)
      setSubmitted(null)
      setProgress(null)
    }
    refresh()
  })

  return {
    runningId,
    progress,
    finishedId: finishedId ?? (submittedEnded ? submitted : null),
    watch: (runId) => {
      setSubmitted(runId)
      setFinishedId(null)
      setProgress(null)
      void queryClient.invalidateQueries({ queryKey: linkedinKeys.status() })
    },
    dismiss: () => {
      setFinishedId(null)
      setSubmitted(null)
    },
  }
}
