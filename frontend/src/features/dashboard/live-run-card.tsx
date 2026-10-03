import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { useServerEvent } from '@/features/events/use-server-event'
import {
  cancelRun,
  linkedinKeys,
  pauseRun,
  resumeRun,
  runContactsQuery,
  runQuery,
  statusQuery,
} from '@/features/linkedin/api'
import {
  RUN_STATUS_CLASSES,
  formatFields,
  formatWhen,
  stopReasonLabel,
} from '@/features/linkedin/fields'
import { RUN_KIND_LABELS, RUN_STATUS_LABELS, type Run } from '@/features/linkedin/types'
import { cn } from '@/lib/utils'

import { lastRunQuery } from './api'

type RunProgress = { run_id: number } & Record<string, unknown>

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** A run a person paused, whose plan nobody has resumed yet: the one Resume is for. */
function isPaused(run: Run): boolean {
  return run.status !== 'running' && run.stop_reason === 'paused' && run.resumed_by === null
}

/**
 * The live LinkedIn run (#324): what is running, how far it got, the last few
 * contacts it touched, and the controls for it.
 *
 * The run shown is the account's running one (`GET /linkedin/status`), or, when
 * nothing runs, the newest run if a person paused it, so it can be resumed from
 * here. Progress arrives on the event stream (`run.progress` patches the cached
 * run and refetches its contacts), so nothing polls.
 *
 * Every control goes through the run's own endpoints. Cancel and Pause are both
 * cooperative: the run stops at its next check, between pages or profiles. Cancel
 * asks first, because a cancelled run is not resumed; Pause keeps the run's place,
 * and only an enrichment has a plan to keep. Resume shows only for a paused run,
 * and asks first, because it starts visiting LinkedIn the moment it is pressed.
 */
export function LiveRunCard() {
  const queryClient = useQueryClient()
  const status = useQuery(statusQuery)
  const last = useQuery(lastRunQuery)

  const runningId = status.data?.running_run_id ?? null
  const latest = last.data?.items[0]
  const pausedId = latest !== undefined && isPaused(latest) ? latest.id : null
  const runId = runningId ?? pausedId

  useServerEvent<RunProgress>('run.progress', ({ run_id, ...progress }) => {
    queryClient.setQueryData(linkedinKeys.run(run_id), (run: Run | undefined) =>
      run === undefined ? run : { ...run, progress },
    )
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.runContacts(run_id) })
  })

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Live LinkedIn run</CardTitle>
        <CardDescription>The run going now, live, and the contacts it touched.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        {status.isPending || last.isPending ? (
          <p role="status" className="text-muted-foreground">
            Checking…
          </p>
        ) : status.isError ? (
          <p role="alert" className="text-muted-foreground">
            The LinkedIn status could not be loaded.
          </p>
        ) : runId === null ? (
          <p className="text-muted-foreground">No LinkedIn run is going.</p>
        ) : (
          <LiveRun runId={runId} />
        )}
      </CardContent>
    </Card>
  )
}

function LiveRun({ runId }: { runId: number }) {
  const queryClient = useQueryClient()
  const run = useQuery(runQuery(runId))
  const [asking, setAsking] = useState<'cancel' | 'resume' | null>(null)

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.runs() })
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.status() })
  }
  const cancel = useMutation({
    mutationFn: () => cancelRun(runId),
    onSuccess: (data) => {
      queryClient.setQueryData(linkedinKeys.run(runId), data)
      setAsking(null)
    },
  })
  const pause = useMutation({
    mutationFn: () => pauseRun(runId),
    onSuccess: (data) => queryClient.setQueryData(linkedinKeys.run(runId), data),
  })
  const resume = useMutation({
    mutationFn: () => resumeRun(runId, null),
    onSuccess: () => {
      setAsking(null)
      refresh()
    },
  })

  if (run.isPending) {
    return (
      <p role="status" className="text-muted-foreground">
        Loading the run…
      </p>
    )
  }
  if (run.isError) return <p role="alert">{message(run.error)}</p>

  const data = run.data
  const running = data.status === 'running'
  const stopping = data.cancel_requested_at !== null
  const paused = isPaused(data)
  const fields = formatFields(running ? data.progress : data.counts)

  return (
    <>
      <p className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{RUN_KIND_LABELS[data.kind]}</span>
        <span
          className={cn(
            'inline-flex h-5 shrink-0 items-center rounded-4xl px-2 text-xs font-medium',
            RUN_STATUS_CLASSES[data.status],
          )}
        >
          {paused ? 'Paused' : RUN_STATUS_LABELS[data.status]}
        </span>
        <span className="text-muted-foreground">started {formatWhen(data.started_at)}</span>
      </p>

      {running && stopping && (
        <p role="status" className="text-muted-foreground">
          {data.pause_requested
            ? 'Pausing at the next check; it keeps its place.'
            : 'Stopping at the next check.'}
        </p>
      )}
      {!running && data.stop_reason !== null && (
        <p className="text-muted-foreground">Stopped: {stopReasonLabel(data)}</p>
      )}
      {data.planned !== null && (
        <p>
          <span className="tabular-nums">
            {data.completed ?? 0} of {data.planned}
          </span>{' '}
          planned profiles done
        </p>
      )}

      {fields.length > 0 && (
        <dl
          aria-label={running ? 'Progress' : 'Counts'}
          className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3"
        >
          {fields.map((field) => (
            <div key={field.label} className="flex min-w-0 flex-col">
              <dt className="text-xs text-muted-foreground">{field.label}</dt>
              <dd className="min-w-0 tabular-nums break-words">{field.value}</dd>
            </div>
          ))}
        </dl>
      )}

      <RecentContacts runId={runId} />

      <div className="flex flex-wrap gap-2">
        {running && data.kind === 'enrich' && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => pause.mutate()}
            disabled={pause.isPending || stopping}
          >
            Pause
          </Button>
        )}
        {running && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => setAsking('cancel')}
            disabled={stopping && !data.pause_requested}
          >
            Cancel run
          </Button>
        )}
        {paused && (
          <Button size="sm" onClick={() => setAsking('resume')}>
            Resume
          </Button>
        )}
      </div>
      {pause.isError && <p role="alert">{message(pause.error)}</p>}

      <ConfirmDialog
        open={asking === 'cancel'}
        onOpenChange={(open) => setAsking(open ? 'cancel' : null)}
        title="Cancel this run?"
        confirmLabel="Cancel run"
        pending={cancel.isPending}
        error={cancel.isError ? message(cancel.error) : null}
        onConfirm={() => cancel.mutateAsync()}
      >
        <p>
          The run stops at its next check, between pages or profiles, and keeps what it already did.
        </p>
        <p>
          A cancelled run is not resumed.
          {data.kind === 'enrich' && ' To stop and continue later, pause it instead.'}
        </p>
      </ConfirmDialog>

      <ConfirmDialog
        open={asking === 'resume'}
        onOpenChange={(open) => setAsking(open ? 'resume' : null)}
        title="Resume this enrichment?"
        confirmVariant="default"
        confirmLabel="Resume"
        pending={resume.isPending}
        error={resume.isError ? message(resume.error) : null}
        onConfirm={() => resume.mutateAsync()}
      >
        <p>
          Starts a new run on the rest of this plan, in the same order, right now. It visits
          LinkedIn within today&apos;s budget and active hours.
        </p>
      </ConfirmDialog>
    </>
  )
}

function contactName(first: string | null, last: string | null): string {
  return [first, last].filter(Boolean).join(' ') || 'Unnamed contact'
}

function RecentContacts({ runId }: { runId: number }) {
  const contacts = useQuery(runContactsQuery(runId))

  if (contacts.isPending) return null
  if (contacts.isError) {
    return (
      <p role="alert" className="text-muted-foreground">
        The contacts this run touched could not be loaded.
      </p>
    )
  }
  return (
    <div>
      <h3 className="text-xs font-medium text-muted-foreground">Recent contacts</h3>
      {contacts.data.items.length === 0 ? (
        <p className="text-muted-foreground">No contacts touched yet.</p>
      ) : (
        <ul aria-label="Recent contacts" className="flex flex-col gap-1">
          {contacts.data.items.map((item, index) => (
            <li key={`${item.contact_id}-${index}`}>
              <Link
                to="/contacts/$contactId"
                params={{ contactId: String(item.contact_id) }}
                className="font-medium underline-offset-2 hover:underline"
              >
                {contactName(item.first_name, item.last_name)}
              </Link>
              <span className="text-muted-foreground"> · {item.outcome_text}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
