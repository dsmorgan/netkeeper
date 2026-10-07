import { useQuery } from '@tanstack/react-query'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { CheckRepliesNow } from '@/features/poll-status/check-now'
import { pollStatusQuery, usePollStatusRefresh } from '@/features/poll-status/api'
import { checkSummary, everyText, repliesText } from '@/features/poll-status/format'
import { useNow } from '@/features/poll-status/use-now'
import { cn } from '@/lib/utils'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * The Gmail background checks `netkeeper serve` runs (#401): the reply poll and the
 * drafts poll, each with its interval, when it last ran and runs next, and why it
 * cannot when it cannot (a blocked mailbox, a locked Keychain, `serve` not running).
 * Read from `GET /poll-status`, which never starts a check. **Check now** (#409) asks
 * for the reply poll at the next campaign tick; it never calls Gmail itself.
 */
export function ReplyPollCard() {
  const status = useQuery(pollStatusQuery)
  usePollStatusRefresh()
  const now = useNow()
  const checks = status.data?.items.filter((check) => check.group === 'gmail') ?? []

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Reply poll</CardTitle>
        <CardDescription>
          How netkeeper finds replies, and keeps track of the drafts it made.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {status.isPending && <p role="status">Loading…</p>}
        {status.isError && <p role="alert">{message(status.error)}</p>}
        {status.isSuccess && !status.data.background_running && (
          <p
            role="note"
            className="rounded-lg bg-amber-500/10 px-3 py-2 text-amber-800 dark:text-amber-300"
          >
            netkeeper serve isn’t running, so nothing is checked in the background.
          </p>
        )}
        {status.isSuccess && checks.length === 0 && (
          <p className="text-muted-foreground">No Gmail check is listed.</p>
        )}
        {checks.map((check) => (
          <section key={check.key} aria-label={check.label} className="space-y-0.5">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h3 className="font-medium">{check.label}</h3>
              <span className="text-xs text-muted-foreground">
                {everyText(check.interval_minutes)}
              </span>
            </div>
            <p className={cn(check.state === 'blocked' && 'text-destructive')}>
              {checkSummary(check, now)}
            </p>
            {check.reason && (
              <p
                className={cn(
                  'text-xs',
                  check.state === 'blocked' ? 'text-destructive' : 'text-muted-foreground',
                )}
              >
                {check.reason}
              </p>
            )}
            {check.key === 'gmail_replies' && <CheckRepliesNow check={check} />}
          </section>
        ))}
        {status.isSuccess && status.data.mailboxes.length > 0 && (
          <section aria-label="By mailbox" className="space-y-1">
            <h3 className="font-medium">By mailbox</h3>
            <ul className="space-y-1">
              {status.data.mailboxes.map((poll) => (
                <li key={poll.mailbox_id}>
                  <span className="font-medium">{poll.email}</span>
                  <span className="text-muted-foreground">: {repliesText(poll, now)}</span>
                </li>
              ))}
            </ul>
          </section>
        )}
      </CardContent>
    </Card>
  )
}
