import { useQuery } from '@tanstack/react-query'
import { Clock } from 'lucide-react'

import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverTitle,
  PopoverTrigger,
} from '@/components/ui/popover'
import { cn } from '@/lib/utils'

import { pollStatusQuery, usePollStatusRefresh, type PollCheck, type PollStatus } from './api'
import { CheckRepliesNow } from './check-now'
import { checkSummary, everyText } from './format'
import { useNow } from './use-now'

/** The two checks the header names; the popover lists every one. */
const HEADLINE: ReadonlyArray<{ key: string; label: string }> = [
  { key: 'gmail_replies', label: 'Gmail' },
  { key: 'linkedin_inbox', label: 'LinkedIn inbox' },
]

const GROUPS: ReadonlyArray<{ group: PollCheck['group']; label: string }> = [
  { group: 'gmail', label: 'Gmail' },
  { group: 'linkedin', label: 'LinkedIn' },
]

/**
 * The top bar's background-check status (#401): when Gmail was last checked and is
 * checked next, and the LinkedIn inbox's state, with every check in a popover.
 *
 * A check that cannot run (paused, outside active hours, disarmed, `netkeeper serve`
 * not running, not built yet) says so and never shows a time. Below `sm` only the
 * clock icon shows; the lines stay for screen readers.
 */
export function PollStatusHeader() {
  const status = useQuery(pollStatusQuery)
  usePollStatusRefresh()
  const now = useNow()

  if (status.data === undefined) return null
  const byKey = new Map(status.data.items.map((check) => [check.key, check]))
  const lines = HEADLINE.flatMap(({ key, label }) => {
    const check = byKey.get(key)
    return check === undefined ? [] : [{ key, label, text: checkSummary(check, now) }]
  })

  return (
    <Popover>
      <PopoverTrigger className="flex min-w-0 items-center justify-center gap-2 rounded-md max-sm:min-h-8 max-sm:min-w-8 px-1.5 py-0.5 text-left text-xs text-muted-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring">
        <Clock className="size-4 shrink-0" aria-hidden="true" />
        <span className="sr-only">Background checks: </span>
        <span className="flex min-w-0 flex-col leading-tight max-sm:sr-only">
          {lines.map((line) => (
            <span key={line.key} className="truncate">
              {line.label}: {line.text}
            </span>
          ))}
        </span>
      </PopoverTrigger>
      <PopoverContent>
        <ChecksDetail status={status.data} now={now} />
      </PopoverContent>
    </Popover>
  )
}

function ChecksDetail({ status, now }: { status: PollStatus; now: number }) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <PopoverTitle>Background checks</PopoverTitle>
        <PopoverDescription>
          {status.background_running
            ? 'What netkeeper serve checks on its own, and when. Pages update after each check.'
            : 'netkeeper serve isn’t running, so nothing is checked in the background.'}
        </PopoverDescription>
      </div>
      {GROUPS.map(({ group, label }) => {
        const checks = status.items.filter((check) => check.group === group)
        if (checks.length === 0) return null
        return (
          <section key={group} aria-label={label} className="flex flex-col gap-2">
            <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
              {label}
            </h3>
            <ul className="flex flex-col gap-2">
              {checks.map((check) => (
                <li key={check.key} className="flex flex-col gap-0.5">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="font-medium">{check.label}</span>
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {everyText(check.interval_minutes)}
                    </span>
                  </div>
                  <span
                    className={cn(
                      check.state === 'blocked' && 'text-destructive',
                      check.state !== 'scheduled' &&
                        check.state !== 'due' &&
                        check.state !== 'blocked' &&
                        'text-muted-foreground',
                    )}
                  >
                    {checkSummary(check, now)}
                  </span>
                  {check.reason && (
                    <span className="text-xs text-muted-foreground">{check.reason}</span>
                  )}
                  {check.key === 'gmail_replies' && <CheckRepliesNow check={check} />}
                </li>
              ))}
            </ul>
          </section>
        )
      })}
    </div>
  )
}
