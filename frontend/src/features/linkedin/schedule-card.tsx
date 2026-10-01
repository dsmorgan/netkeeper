import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { cn } from '@/lib/utils'

import { armSchedule, budgetQuery, disarmSchedule, linkedinKeys, scheduleQuery } from './api'
import { formatWhen } from './fields'
import { RiskWarning } from './risk-warning'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Whether scheduled runs may fire, always shown plainly (spec 9.4): every
 * install starts disarmed, and arming is the one thing on this page that
 * lets netkeeper contact LinkedIn without a person watching. Arming from the
 * UI goes through a confirmation dialog that says exactly that and sends
 * `confirm: true` (the API refuses arming without it); disarming is one
 * click, because backing out of "runs happen on their own" should never be
 * harder than turning it on. When the daily profile-visit limit is above
 * 100, the dialog shows the budget's risk warning too (#318); it informs and
 * does not block arming.
 */
export function ScheduleCard() {
  const schedule = useQuery(scheduleQuery)
  const budget = useQuery(budgetQuery)
  const queryClient = useQueryClient()
  const [asking, setAsking] = useState(false)

  const arm = useMutation({
    mutationFn: armSchedule,
    onSuccess: (data) => {
      queryClient.setQueryData(linkedinKeys.schedule(), data)
      void queryClient.invalidateQueries({ queryKey: linkedinKeys.status() })
      setAsking(false)
    },
  })
  const disarm = useMutation({
    mutationFn: disarmSchedule,
    onSuccess: (data) => {
      queryClient.setQueryData(linkedinKeys.schedule(), data)
      void queryClient.invalidateQueries({ queryKey: linkedinKeys.status() })
    },
  })

  const armed = schedule.data?.armed ?? false

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Scheduled runs</CardTitle>
        <CardDescription>Syncs and enrichment that run on their own, when armed.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {schedule.isPending && <p role="status">Loading…</p>}
        {schedule.isError && <p role="alert">{message(schedule.error)}</p>}
        {schedule.isSuccess && (
          <>
            <p className="flex items-center gap-2">
              <span
                aria-hidden="true"
                className={cn(
                  'size-2 rounded-full',
                  armed ? 'bg-amber-500' : 'bg-muted-foreground/50',
                )}
              />
              <span role="status" className="font-medium">
                {armed
                  ? `Armed${schedule.data.armed_at === null ? '' : ` since ${formatWhen(schedule.data.armed_at)}`}`
                  : 'Disarmed — nothing runs on its own'}
              </span>
            </p>
            {schedule.data.jobs.length > 0 && (
              <ul className="space-y-1 text-muted-foreground">
                {schedule.data.jobs.map((job) => (
                  <li key={job.kind}>
                    {job.kind}: every {job.interval_hours}h, next due{' '}
                    {job.next_due === null ? 'not scheduled' : formatWhen(job.next_due)}
                  </li>
                ))}
              </ul>
            )}
            {armed ? (
              <Button variant="outline" onClick={() => disarm.mutate()} disabled={disarm.isPending}>
                {disarm.isPending ? 'Disarming…' : 'Disarm'}
              </Button>
            ) : (
              <Button onClick={() => setAsking(true)}>Arm scheduled runs</Button>
            )}
            {disarm.isError && <p role="alert">{message(disarm.error)}</p>}
          </>
        )}
      </CardContent>

      <ConfirmDialog
        open={asking}
        onOpenChange={setAsking}
        title="Arm scheduled runs?"
        confirmVariant="default"
        confirmLabel="Arm scheduled runs"
        pending={arm.isPending}
        error={arm.isError ? message(arm.error) : null}
        onConfirm={() => arm.mutate()}
      >
        <p>
          Scheduled syncs and enrichment will start contacting LinkedIn on their own, within active
          hours, from now on — without you starting or watching each one.
        </p>
        <RiskWarning text={budget.data?.risk_warning} />
        <p>
          You can disarm again with one click, any time, and a run already going is not cancelled by
          disarming.
        </p>
      </ConfirmDialog>
    </Card>
  )
}
