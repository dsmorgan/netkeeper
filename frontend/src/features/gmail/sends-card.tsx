import { useQuery } from '@tanstack/react-query'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

import { gmailActivityQuery } from './api'
import { capWarning } from './cap'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Today's sends against each mailbox's daily cap (spec 11.4): recipients fired today,
 * counted as the campaign tick counts them (a failed send still counts, since it may
 * have gone out), over the cap the tick enforces. A mailbox near or at its cap gets
 * the same amber note the LinkedIn budget panel uses for a risk.
 */
export function SendsCard() {
  const activity = useQuery(gmailActivityQuery)

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Today’s sends</CardTitle>
        <CardDescription>Recipients so far today against the daily cap.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {activity.isPending && <p role="status">Loading…</p>}
        {activity.isError && <p role="alert">{message(activity.error)}</p>}
        {activity.isSuccess && activity.data.mailboxes.length === 0 && (
          <p className="text-muted-foreground">No mailbox is connected, so nothing is sent.</p>
        )}
        {activity.isSuccess &&
          activity.data.mailboxes.map((sends) => {
            const warning = capWarning(sends)
            const share =
              sends.daily_cap === 0
                ? 100
                : Math.min(100, (sends.sent_today / sends.daily_cap) * 100)
            return (
              <div key={sends.mailbox_id} className="space-y-1">
                <p className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="font-medium">{sends.email}</span>
                  <span className="tabular-nums">
                    <span className="font-semibold">{sends.sent_today}</span> / {sends.daily_cap}
                  </span>
                </p>
                <div
                  role="meter"
                  aria-label={`${sends.email} sends today`}
                  aria-valuemin={0}
                  aria-valuemax={sends.daily_cap}
                  aria-valuenow={Math.min(sends.sent_today, sends.daily_cap)}
                  className="h-2 overflow-hidden rounded-full bg-muted"
                >
                  <div
                    className={cn('h-full', warning === null ? 'bg-emerald-500' : 'bg-amber-500')}
                    style={{ width: `${share}%` }}
                  />
                </div>
                {warning !== null && (
                  <p
                    role="note"
                    aria-label="Daily cap"
                    className="rounded-lg bg-amber-500/10 px-3 py-2 text-amber-800 dark:text-amber-300"
                  >
                    {warning}
                  </p>
                )}
              </div>
            )
          })}
        {activity.isSuccess && (
          <p className="text-xs text-muted-foreground">
            The day runs on your {activity.data.timezone} clock. This page never calls Gmail.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
