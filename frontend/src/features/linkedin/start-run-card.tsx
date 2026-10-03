import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { INPUT_CLASS, SELECT_CLASS } from '@/features/imports/styles'
import { cn } from '@/lib/utils'

import { budgetQuery, linkedinKeys, startRun } from './api'
import { ProfileViewNotice } from './profile-view-notice'
import { RUNNABLE_KINDS, RUN_KIND_LABELS, type RunKind } from './types'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * A value the max-visits field will accept: whole, at least 1, never above
 * `remaining`. `remaining <= 0` means there is truly nothing left to spend
 * today, so there is no positive number to clamp down to — the field goes
 * empty (and the caller disables it) rather than floor to a false "1".
 */
function clamp(raw: string, remaining: number | null): string {
  if (raw === '') return ''
  if (remaining !== null && remaining <= 0) return ''
  const parsed = Math.floor(Number(raw))
  if (!Number.isFinite(parsed)) return ''
  const ceiling = remaining === null ? parsed : Math.min(parsed, remaining)
  return String(Math.max(1, ceiling))
}

/**
 * Start a run by hand (spec 9.4, 9.9): an incremental or full connections
 * sync, or an enrichment capped by `max_visits`.
 *
 * `max_visits` can only ever lower today's remaining profile-visit budget
 * (CLAUDE.md, `RunStartIn.max_visits`) — never raise it — so the field's
 * *state* is clamped to `budget.profile_visits_today.remaining` on every
 * keystroke, not just marked invalid: typing a larger number is not a
 * momentarily-wrong state to flag, it is a number the state behind this
 * field can never hold, even for the one render between the keystroke and
 * the clamp. `remaining` can also change out from under an open card — a
 * run elsewhere spends today's budget — so it is re-clamped in an effect
 * whenever `remaining` itself changes, not only on the next keystroke (L1).
 * Starting goes behind a confirmation dialog naming what is about to happen,
 * because this is the one button on the page that reaches out to LinkedIn
 * the moment it is pressed.
 */
export function StartRunCard({ onStarted }: { onStarted: (runId: number) => void }) {
  const budget = useQuery(budgetQuery)
  const remaining = budget.data?.profile_visits_today.remaining ?? null
  const [kind, setKind] = useState<RunKind>('connections_incremental')
  const [maxVisits, setMaxVisits] = useState('')
  const [asking, setAsking] = useState(false)
  const queryClient = useQueryClient()

  // `remaining` can change out from under an open card — a run elsewhere spends
  // today's budget — so it is re-clamped whenever `remaining` itself changes,
  // not only on the next keystroke (L1). Adjusted during render against a
  // tracked previous value, React's own pattern for this rather than an
  // effect (https://react.dev/learn/you-might-not-need-an-effect): a
  // same-tick setState() in an effect body is a cascading-render smell the
  // lint rule (react-hooks/set-state-in-effect) catches.
  const [previousRemaining, setPreviousRemaining] = useState(remaining)
  if (remaining !== previousRemaining) {
    setPreviousRemaining(remaining)
    setMaxVisits((current) => clamp(current, remaining))
  }

  const budgetExhausted = kind === 'enrich' && remaining === 0

  const start = useMutation({
    mutationFn: () =>
      startRun(kind, kind === 'enrich' && maxVisits !== '' ? Number(maxVisits) : null),
    onSuccess: (data) => {
      setAsking(false)
      void queryClient.invalidateQueries({ queryKey: linkedinKeys.runs() })
      void queryClient.invalidateQueries({ queryKey: linkedinKeys.status() })
      onStarted(data.run_id)
    },
  })

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Start a run</CardTitle>
        <CardDescription>A manual sync or enrichment, outside the schedule.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <div className="flex flex-col gap-1">
          <label htmlFor="run-kind" className="text-xs text-muted-foreground">
            Kind
          </label>
          <select
            id="run-kind"
            className={cn(SELECT_CLASS, 'w-full max-w-full')}
            value={kind}
            onChange={(event) => setKind(event.target.value as RunKind)}
          >
            {RUNNABLE_KINDS.map((value) => (
              <option key={value} value={value}>
                {RUN_KIND_LABELS[value]}
              </option>
            ))}
          </select>
        </div>

        {kind === 'enrich' && (
          <div className="flex flex-col gap-1">
            <label htmlFor="max-visits" className="text-xs text-muted-foreground">
              Max visits{remaining !== null && ` (today's budget: ${remaining} left)`}
            </label>
            <input
              id="max-visits"
              type="number"
              inputMode="numeric"
              min={1}
              max={remaining ?? undefined}
              className={INPUT_CLASS}
              value={maxVisits}
              disabled={budgetExhausted}
              onChange={(event) => setMaxVisits(clamp(event.target.value, remaining))}
              placeholder={remaining === null ? 'today’s budget' : String(remaining)}
            />
            {budgetExhausted && (
              <p className="text-xs text-muted-foreground">
                Today's profile-visit budget is spent; nothing is left to enrich with until it
                resets.
              </p>
            )}
          </div>
        )}

        {kind === 'enrich' && <ProfileViewNotice text={budget.data?.profile_view_notice} />}

        {start.isError && <p role="alert">{message(start.error)}</p>}

        <Button onClick={() => setAsking(true)} disabled={budgetExhausted}>
          Start run
        </Button>
      </CardContent>

      <ConfirmDialog
        open={asking}
        onOpenChange={setAsking}
        title={`Start ${RUN_KIND_LABELS[kind].toLowerCase()}?`}
        confirmLabel="Start run"
        confirmVariant="default"
        pending={start.isPending}
        error={start.isError ? message(start.error) : null}
        onConfirm={() => start.mutateAsync()}
      >
        <p>
          {kind === 'enrich'
            ? `Starts now, and visits up to ${maxVisits === '' ? "today's remaining budget" : `${maxVisits} profile${maxVisits === '1' ? '' : 's'}`}.`
            : 'Starts now, against the browser this server is attached to.'}
        </p>
        {kind === 'enrich' && <ProfileViewNotice text={budget.data?.profile_view_notice} />}
        <p>
          Refused if the session is flagged, heat is over its threshold, or a run of this account is
          already going.
        </p>
      </ConfirmDialog>
    </Card>
  )
}
