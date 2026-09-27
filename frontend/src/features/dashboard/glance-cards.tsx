import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import type { ReactNode } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { budgetQuery, heatQuery, scheduleQuery, statusQuery } from '@/features/linkedin/api'
import { RUN_STATUS_CLASSES, formatWhen } from '@/features/linkedin/fields'
import {
  RUN_KIND_LABELS,
  RUN_STATUS_LABELS,
  type Run,
  type RunKind,
} from '@/features/linkedin/types'
import { cn } from '@/lib/utils'

import { changedJobsQuery, inboundQuery, lastRunQuery, nextFiresQuery, type NextFire } from './api'

/**
 * The dashboard's "at a glance" cards (P3-12): what happens next, and whether
 * anything is unhealthy. Each card reads the one endpoint that already owns its
 * answer — the LinkedIn cards the same `/linkedin/*` queries the LinkedIn page
 * uses, so the two pages share one cache and cannot disagree — and each has its
 * own loading, error, and empty state, so one failing read never hides the rest.
 */

function GlanceCard({
  title,
  description,
  children,
}: {
  title: string
  description?: ReactNode
  children: ReactNode
}) {
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>{title}</CardTitle>
        {description !== undefined && <CardDescription>{description}</CardDescription>}
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">{children}</CardContent>
    </Card>
  )
}

function Checking() {
  return (
    <p role="status" className="text-muted-foreground">
      Checking…
    </p>
  )
}

function Failed({ what }: { what: string }) {
  return (
    <p role="alert" className="text-muted-foreground">
      {what} could not be loaded.
    </p>
  )
}

function Muted({ children }: { children: ReactNode }) {
  return <p className="text-muted-foreground">{children}</p>
}

function More({ shown, total }: { shown: number; total: number }) {
  if (total <= shown) return null
  return <Muted>and {total - shown} more</Muted>
}

function kindLabel(kind: string): string {
  return RUN_KIND_LABELS[kind as RunKind] ?? kind
}

/**
 * "due now" for a step whose time had come when the list was fetched (the
 * next tick's), else the time. Measured against the fetch, not the clock, so a
 * render stays pure; the list refetches every minute anyway.
 */
function dueText(due: string, fetchedAt: number): string {
  return new Date(due).getTime() <= fetchedAt ? 'due now' : formatWhen(due)
}

/** A calendar date (`YYYY-MM-DD`) as the reader writes dates, without a timezone shift. */
function formatDay(day: string): string {
  const parsed = new Date(`${day}T00:00:00Z`)
  if (Number.isNaN(parsed.getTime())) return day
  return parsed.toLocaleDateString(undefined, { dateStyle: 'medium', timeZone: 'UTC' })
}

function stepText(fire: NextFire): string {
  const step = fire.step_position === null ? 'no step left' : `step ${fire.step_position}`
  return `${fire.campaign_name} · ${step}`
}

// --- what happens next ------------------------------------------------------------------

/** The next campaign steps the minute tick will consider (`next_action_at`, spec 11.4). */
export function NextSendsCard() {
  const fires = useQuery(nextFiresQuery)

  return (
    <GlanceCard title="Next campaign sends">
      {fires.isPending ? (
        <Checking />
      ) : fires.isError ? (
        <Failed what="Upcoming sends" />
      ) : fires.data.items.length === 0 ? (
        <Muted>Nothing is scheduled to send.</Muted>
      ) : (
        <>
          <ol className="flex flex-col gap-1.5">
            {fires.data.items.map((fire) => (
              <li key={fire.enrollment_id} className="flex flex-col">
                <span>
                  <span className="font-medium tabular-nums">
                    {dueText(fire.due, fires.dataUpdatedAt)}
                  </span>
                  {' — '}
                  <Link
                    to="/contacts/$contactId"
                    params={{ contactId: String(fire.contact_id) }}
                    className="underline-offset-2 hover:underline"
                  >
                    {fire.contact_name || 'Unnamed contact'}
                  </Link>
                </span>
                <span className="text-xs text-muted-foreground">{stepText(fire)}</span>
              </li>
            ))}
          </ol>
          <More shown={fires.data.items.length} total={fires.data.total} />
        </>
      )}
    </GlanceCard>
  )
}

/**
 * The next scheduled LinkedIn run, from the scheduler's persisted due times
 * (`GET /linkedin/schedule`). Unarmed, nothing fires however soon a due time
 * is, so the card says that instead of showing a time that will not happen.
 */
export function NextLinkedInRunCard() {
  const schedule = useQuery(scheduleQuery)

  return (
    <GlanceCard title="Next LinkedIn run">
      {schedule.isPending ? (
        <Checking />
      ) : schedule.isError ? (
        <Failed what="The LinkedIn schedule" />
      ) : !schedule.data.armed ? (
        <>
          <Muted>
            Scheduled runs are off. Nothing visits LinkedIn on its own until you arm them.
          </Muted>
          <div>
            <Button size="sm" variant="outline" render={<Link to="/linkedin" />}>
              Open LinkedIn
            </Button>
          </div>
        </>
      ) : (
        <LinkedInJobs
          jobs={schedule.data.jobs}
          schedulerRunning={schedule.data.scheduler_running}
        />
      )}
    </GlanceCard>
  )
}

function LinkedInJobs({
  jobs,
  schedulerRunning,
}: {
  jobs: { kind: string; next_due: string | null }[]
  schedulerRunning: boolean
}) {
  const due = jobs
    .filter((job): job is { kind: string; next_due: string } => job.next_due !== null)
    .sort((a, b) => a.next_due.localeCompare(b.next_due))

  return (
    <>
      {!schedulerRunning && (
        <p role="alert" className="text-destructive">
          No scheduler is running in this process, so nothing below will fire.
        </p>
      )}
      {due.length === 0 ? (
        <Muted>No run is scheduled yet.</Muted>
      ) : (
        <ol className="flex flex-col gap-1">
          {due.map((job) => (
            <li key={job.kind}>
              <span className="font-medium tabular-nums">{formatWhen(job.next_due)}</span>
              {' — '}
              {kindLabel(job.kind)}
            </li>
          ))}
        </ol>
      )}
    </>
  )
}

// --- is anything unhealthy --------------------------------------------------------------

/**
 * The browser side's health: the session flag and how the last run ended.
 *
 * "No flag" is not "the session is healthy": nothing here checks the live
 * session (a request handler never touches the browser), and a session nobody
 * has checked is #282's to show. So the card says only what it knows.
 */
export function BrowserHealthCard() {
  const status = useQuery(statusQuery)
  const runs = useQuery(lastRunQuery)

  return (
    <GlanceCard title="LinkedIn browser">
      {status.isPending ? (
        <Checking />
      ) : status.isError ? (
        <Failed what="The session flag" />
      ) : status.data.session_flag === 'checkpoint' ? (
        <p role="alert" className="text-destructive">
          LinkedIn asked for a checkpoint. No run touches the browser until it is cleared.
        </p>
      ) : status.data.session_flag !== null ? (
        <p role="alert" className="text-destructive">
          Logged out of LinkedIn. No run touches the browser until you log in and run{' '}
          <code className="font-mono text-xs">netkeeper preflight</code>.
        </p>
      ) : (
        <Muted>No session flag is raised.</Muted>
      )}
      {runs.isPending ? (
        <Checking />
      ) : runs.isError ? (
        <Failed what="The last run" />
      ) : runs.data.items[0] === undefined ? (
        <Muted>No LinkedIn run yet.</Muted>
      ) : (
        <LastRun run={runs.data.items[0]} />
      )}
      {(status.data?.session_flag ?? null) !== null && (
        <div>
          <Button size="sm" variant="outline" render={<Link to="/linkedin" />}>
            Open LinkedIn
          </Button>
        </div>
      )}
    </GlanceCard>
  )
}

function LastRun({ run }: { run: Run }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <span>Last run: {kindLabel(run.kind)}</span>
        <Badge className={cn(RUN_STATUS_CLASSES[run.status])}>
          {RUN_STATUS_LABELS[run.status]}
        </Badge>
      </div>
      <span className="text-xs text-muted-foreground">
        {run.status === 'running'
          ? `Started ${formatWhen(run.started_at)}`
          : `Ended ${formatWhen(run.completed_at ?? run.started_at)}`}
        {run.stop_reason !== null && ` · ${run.stop_reason}`}
      </span>
    </div>
  )
}

/**
 * Today's profile-visit budget and heat (spec 9.6, 9.7), as the LinkedIn page
 * shows them in full: `/linkedin/budget` and `/linkedin/heat` do the math.
 */
export function BudgetHeatCard() {
  const budget = useQuery(budgetQuery)
  const heat = useQuery(heatQuery)

  return (
    <GlanceCard title="Budget and heat">
      {budget.isPending ? (
        <Checking />
      ) : budget.isError ? (
        <Failed what="Today's budget" />
      ) : (
        <p>
          <span className="font-medium tabular-nums">
            {budget.data.profile_visits_today.remaining}
          </span>{' '}
          profile visits left today
          <span className="text-muted-foreground">
            {' '}
            ({budget.data.profile_visits_today.spent_today} spent of{' '}
            {budget.data.profile_visits_today.after_heat})
          </span>
        </p>
      )}
      {heat.isPending ? (
        <Checking />
      ) : heat.isError ? (
        <Failed what="Heat" />
      ) : heat.data.tripped ? (
        <p role="alert" className="text-destructive">
          Heat is over its threshold, so runs are skipped
          {heat.data.resumes_at !== null ? ` until ${formatWhen(heat.data.resumes_at)}` : ''}.
        </p>
      ) : (
        <p className="flex items-center gap-2">
          <span aria-hidden="true" className="size-2 rounded-full bg-emerald-500" />
          <span>
            Heat {heat.data.score.toFixed(2)} of {heat.data.threshold.toFixed(2)}
            {heat.data.multiplier > 1 && (
              <span className="text-muted-foreground">
                {' '}
                · pacing slowed {heat.data.multiplier.toFixed(2)}×
              </span>
            )}
          </span>
        </p>
      )}
    </GlanceCard>
  )
}

// --- people -----------------------------------------------------------------------------

/**
 * Replies this week. Reply detection is P3-08 and is not built, so this says
 * so plainly, and shows the one count that does exist — inbound messages
 * logged in the last seven days — under its own name rather than as replies.
 */
export function RepliesCard() {
  const inbound = useQuery(inboundQuery)

  return (
    <GlanceCard title="Replies this week">
      {inbound.isPending ? (
        <Checking />
      ) : inbound.isError ? (
        <Failed what="Inbound messages" />
      ) : (
        <>
          {!inbound.data.reply_detection && (
            <Muted>Reply detection is not set up yet, so campaign replies are not counted.</Muted>
          )}
          <p>
            {inbound.data.count === 0 ? (
              'No inbound messages logged in the last 7 days.'
            ) : (
              <>
                <span className="font-medium tabular-nums">{inbound.data.count}</span> inbound{' '}
                {inbound.data.count === 1 ? 'message' : 'messages'} (email and LinkedIn) logged in
                the last 7 days.
              </>
            )}
          </p>
        </>
      )}
    </GlanceCard>
  )
}

/** Contacts whose position changed recently: the best reason to reconnect (spec 9.8). */
export function ChangedJobsCard() {
  const changed = useQuery(changedJobsQuery)

  return (
    <GlanceCard
      title="Changed jobs"
      description={
        changed.isSuccess
          ? `Started or left a position in the last ${changed.data.days} days.`
          : undefined
      }
    >
      {changed.isPending ? (
        <Checking />
      ) : changed.isError ? (
        <Failed what="Changed jobs" />
      ) : changed.data.items.length === 0 ? (
        <Muted>Nobody's position changed in the last {changed.data.days} days.</Muted>
      ) : (
        <>
          <ul className="flex flex-col gap-1.5">
            {changed.data.items.map((row) => (
              <li key={row.contact_id} className="flex flex-col">
                <span>
                  <Link
                    to="/contacts/$contactId"
                    params={{ contactId: String(row.contact_id) }}
                    className="font-medium underline-offset-2 hover:underline"
                  >
                    {row.contact_name || 'Unnamed contact'}
                  </Link>
                  <span className="text-muted-foreground"> · {formatDay(row.changed_on)}</span>
                </span>
                {(row.current_title !== null || row.current_company !== null) && (
                  <span className="text-xs text-muted-foreground">
                    {[row.current_title, row.current_company].filter(Boolean).join(' at ')}
                  </span>
                )}
              </li>
            ))}
          </ul>
          <More shown={changed.data.items.length} total={changed.data.total} />
        </>
      )}
    </GlanceCard>
  )
}
