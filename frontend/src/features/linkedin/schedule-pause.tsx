import { useMutation, useQueryClient } from '@tanstack/react-query'

import { Button } from '@/components/ui/button'
import { pollStatusKeys } from '@/features/poll-status/api'

import { linkedinKeys, pauseSchedule, unpauseSchedule } from './api'
import { formatWhen } from './fields'
import type { Schedule } from './types'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Pause and unpause the schedule (#324), for the dashboard and the LinkedIn page.
 *
 * Pausing holds new scheduled runs without disarming: a run already going keeps
 * going (Cancel stops it), and the pause survives a restart. Neither button reaches
 * LinkedIn, so neither asks first. Unpausing replays nothing that was skipped:
 * each kind waits for its next due time, and the line under the button says so.
 */
export function SchedulePause({ schedule }: { schedule: Schedule }) {
  const queryClient = useQueryClient()
  const onSuccess = (data: Schedule) => {
    queryClient.setQueryData(linkedinKeys.schedule(), data)
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.status() })
    void queryClient.invalidateQueries({ queryKey: pollStatusKeys.all })
  }
  const pause = useMutation({ mutationFn: pauseSchedule, onSuccess })
  const unpause = useMutation({ mutationFn: unpauseSchedule, onSuccess })
  const failed = pause.error ?? unpause.error

  return (
    <div className="flex flex-col gap-2">
      {schedule.paused ? (
        <>
          <p role="status" className="font-medium text-amber-800 dark:text-amber-300">
            Paused{schedule.paused_at === null ? '' : ` since ${formatWhen(schedule.paused_at)}`} —
            no new scheduled run starts.
          </p>
          <p className="text-muted-foreground">
            A run already going keeps going. When you unpause, nothing skipped is replayed: each
            kind runs at its next due time.
          </p>
          <div>
            <Button
              size="sm"
              variant="outline"
              onClick={() => unpause.mutate()}
              disabled={unpause.isPending}
            >
              {unpause.isPending ? 'Unpausing…' : 'Unpause schedule'}
            </Button>
          </div>
        </>
      ) : (
        <div>
          <Button
            size="sm"
            variant="outline"
            onClick={() => pause.mutate()}
            disabled={pause.isPending}
          >
            {pause.isPending ? 'Pausing…' : 'Pause schedule'}
          </Button>
        </div>
      )}
      {failed !== null && <p role="alert">{message(failed)}</p>}
    </div>
  )
}
