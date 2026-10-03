import { useQuery } from '@tanstack/react-query'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { renderInlineMarkdown } from '@/lib/inline-markdown'
import { cn } from '@/lib/utils'

import { browserHealthQuery, browserQuery, statusQuery } from './api'
import { formatWhen } from './fields'
import type { BrowserHealth } from './types'

const SESSION_CLASSES: Record<string, string> = {
  on: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  off: 'bg-destructive/10 text-destructive',
  unknown: 'bg-amber-500/15 text-amber-800 dark:text-amber-300',
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Browser status and launch instructions (spec 9.1, ADR 0002): netkeeper is
 * attach-only and never launches Chrome itself, so this shows the command
 * `netkeeper browser launch` prints rather than a button that would start one.
 *
 * "Last known" (#181) is what netkeeper already recorded, never a fresh look:
 * the last `netkeeper preflight` or run that read LinkedIn, the session flag
 * over both, and the newest run that could not reach Chrome when that is newer.
 * A live check has to attach, which a request handler may never do (CLAUDE.md),
 * so it stays `netkeeper preflight` in a terminal. This refetches when a run
 * starts or ends (`useRunEvents`, over SSE) and when the tab regains focus,
 * which is when a preflight run in a terminal shows up.
 */
export function BrowserCard() {
  const browser = useQuery(browserQuery)
  const status = useQuery(statusQuery)
  const health = useQuery(browserHealthQuery)

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Browser</CardTitle>
        <CardDescription>
          netkeeper attaches to a Chrome you start yourself. It never starts one.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {status.isSuccess && (
          <p>
            This server{' '}
            <span className="font-medium">
              {status.data.can_start_runs ? 'can start runs' : 'cannot start runs'}
            </span>
            {!status.data.can_start_runs &&
              ' (it was not started with `netkeeper serve`, so it has no browser worker).'}
          </p>
        )}

        {health.isError && <p role="alert">{message(health.error)}</p>}
        {health.isSuccess && <LastKnown health={health.data} />}

        {browser.isPending && <p role="status">Loading launch instructions…</p>}
        {browser.isError && <p role="alert">{message(browser.error)}</p>}
        {browser.isSuccess && (
          <>
            <p>Run this in a terminal (again whenever that Chrome is not running):</p>
            <pre className="rounded-md bg-muted p-3 font-mono text-xs break-all whitespace-pre-wrap">
              {browser.data.launch_command.join('\n')}
            </pre>
            <p className="text-muted-foreground">
              Then log in to LinkedIn once in that window, and use it for your own LinkedIn browsing
              too, so your activity and netkeeper's share one session and one fingerprint.
            </p>
            {browser.data.remote_host_note !== null && (
              <p role="alert" className="text-destructive">
                {browser.data.remote_host_note}
              </p>
            )}
            <p className="text-muted-foreground">
              Check it with: <code className="font-mono text-xs">{browser.data.check_command}</code>{' '}
              (attaches to {browser.data.cdp_url})
            </p>
          </>
        )}
      </CardContent>
    </Card>
  )
}

function LastKnown({ health }: { health: BrowserHealth }) {
  return (
    <section aria-label="Last known browser state" className="space-y-1">
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-medium">Last known session</span>
        <span
          className={cn(
            'inline-flex h-5 items-center rounded-4xl px-2 text-xs font-medium',
            SESSION_CLASSES[health.session_status] ?? 'bg-muted text-muted-foreground',
          )}
        >
          {health.session_status}
        </span>
      </p>
      <p className="break-words">{renderInlineMarkdown(health.session_summary)}</p>
      {health.session_warnings.length > 0 && (
        <ul className="space-y-1 break-words text-muted-foreground">
          {health.session_warnings.map((warning) => (
            <li key={warning}>{renderInlineMarkdown(warning)}</li>
          ))}
        </ul>
      )}
      {health.chrome_unreachable_at !== null && (
        <p role="alert" className="text-destructive">
          Run {health.chrome_unreachable_run_id} could not reach Chrome at{' '}
          {formatWhen(health.chrome_unreachable_at)}. Start it with the command below, then run{' '}
          <code className="font-mono text-xs">netkeeper preflight</code>.
        </p>
      )}
      {health.running_run_id !== null && (
        <p className="text-muted-foreground">Run {health.running_run_id} is using it now.</p>
      )}
      <p className="text-xs text-muted-foreground">
        From what netkeeper recorded, as of {formatWhen(health.checked_at)}. This page never checks
        the browser itself.
      </p>
    </section>
  )
}
