import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'

import { useEventStreamStatus } from '@/features/events/event-stream-context'
import { useServerEvent } from '@/features/events/use-server-event'

import { linkedinKeys } from './api'
import type { Run } from './types'

interface RunStarted {
  run_id: number
  kind: string
}

type RunProgress = { run_id: number } & Record<string, unknown>

interface RunFinished {
  run_id: number
  status: string
}

/**
 * Wires `run.started`, `run.progress`, and `run.finished` (spec 14.1, P2-10's
 * `GET /events`) onto the query cache, so the LinkedIn page updates live
 * without a reload or polling — the "done when" this item is built to.
 *
 * `run.started` and `run.finished` invalidate the runs list and the page's
 * banner (`status`), so a run's arrival and its ending both show up. A
 * finish also invalidates budget, heat, and pins, since any of the three can
 * move while a run is going and none of them says so on its own — cheaper to
 * refetch once per run than to guess which of the three actually changed.
 * `run.progress` skips all of that and patches the one cached run detail
 * directly: it can arrive many times a second while a run is going, and nothing
 * but that one run's own page needs to see it.
 */
export function useRunEvents(): void {
  const queryClient = useQueryClient()
  const status = useEventStreamStatus()
  const everConnected = useRef(false)
  const previousStatus = useRef(status)

  useEffect(() => {
    // M1: SSE is fire-and-forget — a gap in the connection is a gap in what
    // this page saw, and nothing replays a missed event once reconnected.
    // Recover by invalidating everything on this page the moment the
    // connection comes *back*, so a dropped run.progress or run.finished
    // shows up as a plain refetch instead of silently going stale. Guarded by
    // `everConnected` so the very first connect (nothing was ever missed)
    // does not double the page's initial fetch.
    if (
      status === 'connected' &&
      previousStatus.current === 'disconnected' &&
      everConnected.current
    ) {
      void queryClient.invalidateQueries({ queryKey: linkedinKeys.all })
    }
    if (status === 'connected') everConnected.current = true
    previousStatus.current = status
  }, [status, queryClient])

  useServerEvent<RunStarted>('run.started', () => {
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.runs() })
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.status() })
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.browserHealth() })
  })

  useServerEvent<RunProgress>('run.progress', ({ run_id, ...progress }) => {
    queryClient.setQueryData(linkedinKeys.run(run_id), (run: Run | undefined) =>
      run === undefined ? run : { ...run, progress },
    )
  })

  useServerEvent<RunFinished>('run.finished', () => {
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.runs() })
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.status() })
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.budget() })
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.heat() })
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.pins() })
    // A run that read LinkedIn, or could not reach Chrome, is new browser evidence (#181).
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.browserHealth() })
  })
}
