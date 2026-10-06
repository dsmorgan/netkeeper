/**
 * The LinkedIn queue and what waits for you (#383, spec 11.6), on the dashboard and
 * on a campaign's page.
 *
 * - **The queue** lists due LinkedIn steps. A person clicks **Prefill** (or **Prefill
 *   next**) to start one; nothing prefills on its own. While it types: "Typing in
 *   Chrome…", with the run's live progress. A refusal says why, and that nothing was
 *   typed.
 * - **Waiting for you** lists prefilled messages to send by hand in Chrome, stale ones
 *   (highlighted), and prefills that stopped before they said what they typed. Each
 *   offers **I sent it, check now** (an inbox poll) and **Discard**.
 * - **One prefill at a time:** while one is typing or open, every Prefill button is
 *   off, and the queue says why.
 *
 * Never a retry for a failed prefill: a person clears the composer in Chrome. No
 * message text is shown anywhere here; netkeeper never closes the tab.
 */
import { Link } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { campaignKeys } from '@/features/campaigns/api'
import { formatWhen } from '@/features/campaigns/format'
import { Callout, ErrorNote, LoadingNote } from '@/features/crm/controls'
import { linkedinKeys, runQuery } from '@/features/linkedin/api'
import { stopReasonLabel, summarizeFields } from '@/features/linkedin/fields'
import { cn } from '@/lib/utils'

import {
  LinkedInStepError,
  checkSent,
  discard,
  linkedinStepKeys,
  prefill,
  readyQuery,
  stepOptionsQuery,
  waitingQuery,
  type ReadyItem,
  type WaitingItem,
} from './api'
import {
  ONE_AT_A_TIME,
  REVIEW_AND_SEND,
  ageText,
  openPrefill,
  reasonText,
  waitingState,
  type WaitingState,
} from './format'
import {
  PARTLY_TYPED,
  PARTLY_TYPED_SENT_ANYWAY,
  PREFILL_NOTE,
  TYPED_WHOLE,
  TYPING,
  TYPING_WARNING,
  prefillEnding,
} from './prefill-copy'
import { FirstPollNote } from './first-poll-note'
import { usePrefillRun, useRunGoing, type PrefillRun } from './use-prefill-run'

/** The queue and the waiting list together: one campaign's when `campaignId` is given. */
export function LinkedInStepsSection({ campaignId }: { campaignId?: number }) {
  const run = usePrefillRun()
  return (
    <>
      <LinkedInQueueCard campaignId={campaignId} run={run} />
      <WaitingForYouCard campaignId={campaignId} />
    </>
  )
}

function useInvalidateSteps() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: linkedinStepKeys.all })
    void queryClient.invalidateQueries({ queryKey: campaignKeys.all })
  }
}

function AutoSendBadge() {
  const options = useQuery(stepOptionsQuery)
  if (options.isPending) return null
  if (options.isError) {
    return (
      <p className="flex flex-wrap items-center gap-2">
        <Badge variant="outline">Auto-send: unknown</Badge>
        <span className="text-muted-foreground">
          netkeeper could not read the setting; Settings, Posture shows it.
        </span>
      </p>
    )
  }
  // A standing setting, not an event: a highlighted note, never an alert.
  return options.data.auto_send ? (
    <p
      role="note"
      className="flex flex-wrap items-center gap-2 rounded-md border border-destructive/50 bg-destructive/10 px-2 py-1 text-destructive"
    >
      <Badge variant="destructive">Auto-send on</Badge>
      netkeeper sends LinkedIn messages itself. See Settings, Posture.
    </p>
  ) : (
    <p className="flex flex-wrap items-center gap-2">
      <span className="inline-flex h-5 items-center rounded-4xl bg-emerald-500/15 px-2 text-xs font-medium text-emerald-800 ring-1 ring-emerald-600/40 dark:text-emerald-300">
        Auto-send off
      </span>
      <span className="text-muted-foreground">You send every LinkedIn message yourself.</span>
    </p>
  )
}

export function LinkedInQueueCard({ campaignId, run }: { campaignId?: number; run: PrefillRun }) {
  const ready = useQuery(readyQuery(campaignId ?? null))
  // Every campaign's: one open prefill anywhere holds the slot.
  const waiting = useQuery(waitingQuery())
  const invalidate = useInvalidateSteps()
  const queryClient = useQueryClient()

  const start = useMutation({
    mutationFn: (target: { enrollmentId: number } | 'next') => prefill(target),
    onSuccess: (accepted) => {
      run.watch(accepted.run_id)
      invalidate()
    },
    onError: () => {
      // A refusal can still change an enrollment (a reply ended it, a step parked).
      invalidate()
      void queryClient.invalidateQueries({ queryKey: linkedinKeys.status() })
    },
  })

  const now = new Date()
  const open = waiting.isSuccess ? openPrefill(waiting.data.items, now) : null
  const typing = run.runningId !== null || start.isPending
  const blocked = typing || open !== null || !waiting.isSuccess
  const items = ready.data?.items ?? []
  const nextReady = items.find((item) => !held(item, now))

  return (
    <Card>
      <CardHeader>
        <CardTitle level={2}>LinkedIn queue</CardTitle>
        <CardDescription>
          Due LinkedIn steps. Prefill one while you watch Chrome; you send it yourself.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        <AutoSendBadge />
        <FirstPollNote />
        {!typing && <p className="text-muted-foreground">{PREFILL_NOTE}</p>}
        {typing && (
          <div role="status" className="rounded-lg border border-sky-500/40 bg-sky-500/10 p-3">
            <p className="font-medium">{TYPING}</p>
            <p>{TYPING_WARNING}</p>
            {run.progress !== null && (
              <p className="text-muted-foreground">{summarizeFields(run.progress, 6)}</p>
            )}
          </div>
        )}
        {!typing && run.finishedId !== null && (
          <FinishedPrefill runId={run.finishedId} onDismiss={run.dismiss} />
        )}
        {start.isError && <Refused error={start.error} onDismiss={() => start.reset()} />}
        {!typing && open !== null && <p className="text-muted-foreground">{ONE_AT_A_TIME}</p>}

        {ready.isPending ? (
          <LoadingNote label="Loading the queue…" />
        ) : ready.isError ? (
          <ErrorNote label="The LinkedIn queue is unavailable." error={ready.error} />
        ) : items.length === 0 ? (
          <p className="text-muted-foreground">No LinkedIn step is due.</p>
        ) : (
          <>
            <ul aria-label="Ready to prefill" className="flex flex-col">
              {items.map((item) => (
                <ReadyRow
                  key={item.enrollment_id}
                  item={item}
                  showCampaign={campaignId === undefined}
                  disabled={blocked || held(item, now)}
                  onPrefill={() => start.mutate({ enrollmentId: item.enrollment_id })}
                />
              ))}
            </ul>
            <div className="flex flex-wrap items-center gap-2">
              {campaignId === undefined && (
                <Button
                  disabled={blocked || nextReady === undefined}
                  onClick={() => start.mutate('next')}
                >
                  Prefill next
                </Button>
              )}
              {ready.data.total > items.length && (
                <span className="text-muted-foreground">
                  Showing {items.length} of {ready.data.total}, oldest due first.
                </span>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}

/** Held by the sending hours: due, but not until `held_until`. */
function held(item: ReadyItem, now: Date): boolean {
  return item.held_until !== null && new Date(item.held_until).getTime() > now.getTime()
}

function ReadyRow({
  item,
  showCampaign,
  disabled,
  onPrefill,
}: {
  item: ReadyItem
  showCampaign: boolean
  disabled: boolean
  onPrefill: () => void
}) {
  const name = item.contact_name || 'Unnamed contact'
  return (
    <li
      aria-label={`${name}, step ${item.step_position}`}
      className="flex flex-wrap items-center gap-2 border-t border-border/60 py-2 first:border-t-0"
    >
      <Link
        to="/contacts/$contactId"
        params={{ contactId: String(item.contact_id) }}
        className="font-medium underline underline-offset-4"
      >
        {name}
      </Link>
      {showCampaign && (
        <>
          <span className="text-muted-foreground">in</span>
          <Link
            to="/campaigns/$campaignId"
            params={{ campaignId: String(item.campaign_id) }}
            className="underline underline-offset-4"
          >
            {item.campaign_name}
          </Link>
        </>
      )}
      <span className="text-muted-foreground">step {item.step_position}</span>
      {item.held_until !== null && (
        <span className="text-muted-foreground">
          held by your sending hours until {formatWhen(item.held_until)}
        </span>
      )}
      <Button
        size="sm"
        className="ml-auto"
        disabled={disabled}
        aria-label={`Prefill ${name}`}
        onClick={onPrefill}
      >
        Prefill
      </Button>
    </li>
  )
}

/** The statuses that mean the backend answered and claimed nothing (no run, no typing). */
const REFUSED_STATUSES = new Set([404, 409, 503])

/**
 * Why a prefill did not start. Only an answer the backend gave (404, 409, 503) proves
 * nothing was typed; any other failure (the network, a timeout, a 5xx) leaves it
 * unknown, so it says to look before trying again.
 */
function Refused({ error, onDismiss }: { error: unknown; onDismiss: () => void }) {
  const answered = error instanceof LinkedInStepError && REFUSED_STATUSES.has(error.status)
  const refusal = answered ? error.refusal : null
  return (
    <Callout
      tone="warning"
      role="alert"
      title={
        answered
          ? 'Not prefilled. Nothing was typed in Chrome.'
          : 'The prefill request failed. Check Waiting for you and the LinkedIn page before you try again.'
      }
    >
      {refusal !== null ? (
        <>
          <ul className="list-disc pl-5">
            {refusal.reasons.map((reason) => (
              <li key={reason}>{reasonText(reason)}</li>
            ))}
          </ul>
          {refusal.detail !== null && <p className="text-muted-foreground">{refusal.detail}</p>}
        </>
      ) : (
        <p>{error instanceof Error ? error.message : 'The prefill could not start.'}</p>
      )}
      <Button size="sm" variant="outline" className="mt-2" onClick={onDismiss}>
        Dismiss
      </Button>
    </Callout>
  )
}

/** How the last prefill run ended, when it ended without a prefilled message. */
function FinishedPrefill({ runId, onDismiss }: { runId: number; onDismiss: () => void }) {
  const run = useQuery(runQuery(runId))
  if (!run.isSuccess || run.data.status === 'running') return null
  if (run.data.status === 'completed') {
    return (
      <p role="status" className="text-muted-foreground">
        {TYPED_WHOLE}{' '}
        <Button size="sm" variant="ghost" onClick={onDismiss}>
          Dismiss
        </Button>
      </p>
    )
  }
  const ending = prefillEnding(run.data.stop_reason, run.data.error, run.data.counts)
  if (ending !== null) {
    return (
      <Callout tone="warning" role="alert" title={ending.title}>
        {ending.reason !== null && <p>{ending.reason}</p>}
        {ending.steps.map((step) => (
          <p key={step}>{step}</p>
        ))}
        <Button size="sm" variant="outline" className="mt-2" onClick={onDismiss}>
          Dismiss
        </Button>
      </Callout>
    )
  }
  const reason = stopReasonLabel(run.data) ?? run.data.error ?? 'no reason recorded'
  return (
    <Callout tone="warning" title="The prefill stopped.">
      <p>{reason}.</p>
      <p>
        If anything was typed, clear the composer in Chrome yourself. netkeeper never retries a
        prefill.
      </p>
      <Button size="sm" variant="outline" className="mt-2" onClick={onDismiss}>
        Dismiss
      </Button>
    </Callout>
  )
}

const STATE_TEXT: Record<WaitingState, string> = {
  prefilled: REVIEW_AND_SEND,
  stale:
    'Stale: prefilled three days ago or more. The tab stays open; send it, or discard it to move on.',
  interrupted:
    'The prefill stopped before it recorded what it typed. Check the composer in Chrome, clear it, then discard this.',
  partly_typed: PARTLY_TYPED,
}

export function WaitingForYouCard({ campaignId }: { campaignId?: number }) {
  const waiting = useQuery(waitingQuery(campaignId ?? null))
  const now = new Date()
  const items = waiting.data?.items ?? []
  return (
    <Card>
      <CardHeader>
        <CardTitle level={2}>Waiting for you</CardTitle>
        <CardDescription>
          LinkedIn messages typed in Chrome for you to send, or to discard.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col text-sm">
        {waiting.isPending ? (
          <LoadingNote label="Loading what waits for you…" />
        ) : waiting.isError ? (
          <ErrorNote label="What waits for you is unavailable." error={waiting.error} />
        ) : items.length === 0 ? (
          <p className="text-muted-foreground">Nothing waits for you.</p>
        ) : (
          <ul aria-label="Waiting for you" className="flex flex-col gap-2">
            {items.map((item) => (
              <WaitingRow
                key={item.message_id}
                item={item}
                state={waitingState(item, now)}
                now={now}
                showCampaign={campaignId === undefined}
              />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

function WaitingRow({
  item,
  state,
  now,
  showCampaign,
}: {
  item: WaitingItem
  state: WaitingState
  now: Date
  showCampaign: boolean
}) {
  const invalidate = useInvalidateSteps()
  const [confirming, setConfirming] = useState(false)
  const check = useMutation({ mutationFn: () => checkSent(item.message_id) })
  const checking = useRunGoing(check.data?.run_id ?? null)
  const drop = useMutation({
    mutationFn: () => discard(item.message_id),
    onSuccess: () => {
      setConfirming(false)
      invalidate()
    },
  })
  const name = item.contact_name || 'Unnamed contact'

  return (
    <li
      aria-label={`${name}, ${state}`}
      data-state={state}
      className={cn(
        'flex flex-col gap-1.5 rounded-lg border p-3',
        state === 'stale' && 'border-amber-500/60 bg-amber-500/10',
        (state === 'interrupted' || state === 'partly_typed') &&
          'border-destructive/40 bg-destructive/5',
        state === 'partly_typed' && 'border-destructive bg-destructive/10',
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Link
          to="/contacts/$contactId"
          params={{ contactId: String(item.contact_id) }}
          className="font-medium underline underline-offset-4"
        >
          {name}
        </Link>
        {showCampaign && (
          <>
            <span className="text-muted-foreground">in</span>
            <Link
              to="/campaigns/$campaignId"
              params={{ campaignId: String(item.campaign_id) }}
              className="underline underline-offset-4"
            >
              {item.campaign_name}
            </Link>
          </>
        )}
        {state === 'stale' && <Badge variant="outline">Stale</Badge>}
        {state === 'interrupted' && <Badge variant="destructive">Stopped</Badge>}
        {state === 'partly_typed' && <Badge variant="destructive">Part typed</Badge>}
        {state === 'prefilled' || state === 'stale' ? (
          <span className="ml-auto text-muted-foreground">
            Prefilled {ageText(item.prefilled_at, now)}
          </span>
        ) : null}
      </div>
      <p>{STATE_TEXT[state]}</p>
      {state === 'partly_typed' && <p>{PARTLY_TYPED_SENT_ANYWAY}</p>}
      <div className="flex flex-wrap items-center gap-2">
        {(state === 'prefilled' || state === 'stale') && (
          <Button size="sm" disabled={check.isPending || checking} onClick={() => check.mutate()}>
            I sent it, check now
          </Button>
        )}
        <Button
          size="sm"
          variant={state === 'partly_typed' ? 'default' : 'outline'}
          onClick={() => setConfirming(true)}
        >
          {state === 'partly_typed' ? 'I cleared it, discard' : 'Discard'}
        </Button>
      </div>
      {check.isSuccess && <CheckStarted runId={check.data.run_id} />}
      {check.isError && <ErrorNote label="The inbox check did not start." error={check.error} />}
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Discard the message to ${name}?`}
        confirmLabel={state === 'partly_typed' ? 'I cleared it, discard' : 'Discard'}
        onConfirm={() => drop.mutateAsync()}
        pending={drop.isPending}
        error={drop.isError ? drop.error.message : null}
      >
        <p>
          The step counts as fired and the enrollment moves to its next step. netkeeper changes
          nothing in LinkedIn: if the message is still in the composer, clear it in Chrome yourself.
        </p>
      </ConfirmDialog>
    </li>
  )
}

/** The inbox check a person asked for: running, or how it ended. */
function CheckStarted({ runId }: { runId: number }) {
  const run = useQuery(runQuery(runId))
  if (!run.isSuccess || run.data.status === 'running') {
    return (
      <p role="status" className="text-muted-foreground">
        Checking your LinkedIn inbox. The message is marked sent once the check finds it.
      </p>
    )
  }
  if (run.data.status === 'completed') {
    return (
      <p role="status" className="text-muted-foreground">
        The inbox check finished. If it found the message, it no longer waits here.
      </p>
    )
  }
  return (
    <p role="status" className="text-muted-foreground">
      The inbox check stopped: {stopReasonLabel(run.data) ?? run.data.error ?? 'no reason recorded'}
      .
    </p>
  )
}
