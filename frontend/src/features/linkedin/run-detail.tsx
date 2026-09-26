import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { cn } from '@/lib/utils'

import { cancelRun, linkedinKeys, resumeRun, runQuery } from './api'
import { formatFields, formatWhen, RUN_STATUS_CLASSES, type Field } from './fields'
import { RUN_KIND_LABELS, RUN_STATUS_LABELS } from './types'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * One run: what it is, its live progress while running (over SSE — the
 * cached run this reads is patched by `useRunEvents`, so no poll or reload
 * is needed here), and how it ended.
 *
 * Cancel is cooperative (spec 9.9): the run stops at its next check, not
 * instantly, so the button disables once a cancel is already requested
 * rather than only while the request itself is in flight. Resume starts a
 * new run on an aborted enrichment's remaining plan — resumable once — and,
 * like Start, sits behind a confirmation dialog because it is a button that
 * reaches out to LinkedIn the moment it is pressed.
 */
export function RunDetail({
  runId,
  onResumed,
}: {
  runId: number
  onResumed?: (runId: number) => void
}) {
  const run = useQuery(runQuery(runId))
  const queryClient = useQueryClient()
  const [resuming, setResuming] = useState(false)

  const cancel = useMutation({
    mutationFn: () => cancelRun(runId),
    onSuccess: (data) => queryClient.setQueryData(linkedinKeys.run(runId), data),
  })
  const resume = useMutation({
    mutationFn: () => resumeRun(runId, null),
    onSuccess: (data) => {
      setResuming(false)
      void queryClient.invalidateQueries({ queryKey: linkedinKeys.runs() })
      onResumed?.(data.run_id)
    },
  })

  if (run.isPending) return <p role="status">Loading the run…</p>
  if (run.isError) return <p role="alert">{message(run.error)}</p>

  const data = run.data
  // A run that could not run at all (browser unavailable, a refusal before it
  // ever started) is `failed`, not `aborted`, but its stored plan is exactly
  // as resumable — the backend already allows it (`enrich_plan.start_resume`
  // only refuses running, completed, or already-resumed, not the status name).
  // `resumed_by` is the one case a resume must never be offered for even when
  // the arithmetic still says "incomplete": a plan is resumed at most once
  // (spec 9.9), and RunOut carries who already has it (L3, #179 review).
  const resumable =
    data.kind === 'enrich' &&
    (data.status === 'aborted' || data.status === 'failed') &&
    data.resumed_by === null &&
    data.planned !== null &&
    data.completed !== null &&
    data.completed < data.planned

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2} className="flex items-center gap-2">
          {RUN_KIND_LABELS[data.kind]}
          <span
            className={cn(
              'inline-flex h-5 shrink-0 items-center rounded-4xl px-2 text-xs font-medium',
              RUN_STATUS_CLASSES[data.status],
            )}
          >
            {RUN_STATUS_LABELS[data.status]}
          </span>
        </CardTitle>
        <CardDescription>
          Started {formatWhen(data.started_at)}
          {data.completed_at !== null && ` · ended ${formatWhen(data.completed_at)}`}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {data.aging_refused !== null && (
          <p
            role="status"
            className="rounded-lg bg-amber-500/10 px-3 py-2 text-amber-800 dark:text-amber-300"
          >
            Network aging: {data.aging_refused}
          </p>
        )}
        {data.stop_reason !== null && (
          <p className="text-muted-foreground">Stopped: {data.stop_reason}</p>
        )}
        {data.error !== null && <p role="alert">{data.error}</p>}
        {data.notes !== null && <p className="text-muted-foreground">{data.notes}</p>}

        {data.status === 'running' && (
          <FieldList title="Progress" fields={formatFields(data.progress)} />
        )}
        {data.status !== 'running' && (
          <FieldList title="Counts" fields={formatFields(data.counts)} />
        )}

        <div className="flex flex-wrap gap-2">
          {data.status === 'running' && (
            <Button
              variant="outline"
              onClick={() => cancel.mutate()}
              disabled={cancel.isPending || data.cancel_requested_at !== null}
            >
              {data.cancel_requested_at !== null
                ? 'Stopping…'
                : cancel.isPending
                  ? 'Stopping…'
                  : 'Stop'}
            </Button>
          )}
          {resumable && <Button onClick={() => setResuming(true)}>Resume</Button>}
        </div>
        {cancel.isError && <p role="alert">{message(cancel.error)}</p>}
      </CardContent>

      <ConfirmDialog
        open={resuming}
        onOpenChange={setResuming}
        title="Resume this enrichment?"
        confirmVariant="default"
        confirmLabel="Resume"
        pending={resume.isPending}
        error={resume.isError ? message(resume.error) : null}
        onConfirm={() => resume.mutate()}
      >
        <p>Starts a new run on the rest of this plan, in the same order, right now.</p>
        <p>A plan can be resumed only once.</p>
      </ConfirmDialog>
    </Card>
  )
}

function FieldList({ title, fields }: { title: string; fields: Field[] }) {
  if (fields.length === 0) return null
  return (
    <div>
      <h3 className="text-xs font-medium text-muted-foreground">{title}</h3>
      {/* One column below sm: at a narrow width, two-up (each cell already a
          label-above-value flex-col there) left a long value with nowhere to
          go but into its neighbor's column, since a grid cell has no min-content
          protection of its own — only a real browser lays that out; jsdom does
          not. min-w-0 + break-words on the value is the same fix as the run and
          budget tables' overflow wrapper: give the cell somewhere to shrink to
          and something wide to wrap inside it, rather than push past it. */}
      <dl className="grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-2">
        {fields.map((field) => (
          <div
            key={field.label}
            className="flex min-w-0 justify-between gap-2 sm:flex-col sm:justify-start"
          >
            <dt className="text-xs text-muted-foreground">{field.label}</dt>
            <dd className="min-w-0 tabular-nums break-words">{field.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}
