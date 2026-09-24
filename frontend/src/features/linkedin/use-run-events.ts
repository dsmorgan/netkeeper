import { useQueryClient } from '@tanstack/react-query'

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

  useServerEvent<RunStarted>('run.started', () => {
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.runs() })
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.status() })
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
  })
}
