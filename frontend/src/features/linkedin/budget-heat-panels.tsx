import { useQuery } from '@tanstack/react-query'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

import { budgetQuery, heatQuery } from './api'
import { formatWhen } from './fields'
import { ACTION_CLASS_LABELS } from './types'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Today's profile-visit budget chain and every action class's counters
 * (spec 9.6): warm-up (`ramp`) → weekend damping → heat shrink → what is
 * already spent → what is left. Each step only ever narrows the one before
 * it, which is the whole shape of the rule this panel exists to show.
 */
export function BudgetPanel() {
  const budget = useQuery(budgetQuery)

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Budget</CardTitle>
        <CardDescription>
          Today's profile-visit chain, and every action class's counters.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {budget.isPending && <p role="status">Loading…</p>}
        {budget.isError && <p role="alert">{message(budget.error)}</p>}
        {budget.isSuccess && (
          <>
            <ol className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3">
              <Step label="Warm-up" value={budget.data.profile_visits_today.ramp} />
              <Step label="After weekend" value={budget.data.profile_visits_today.after_weekend} />
              <Step label="After heat" value={budget.data.profile_visits_today.after_heat} />
              <Step label="Spent today" value={budget.data.profile_visits_today.spent_today} />
              <Step label="Week left" value={budget.data.profile_visits_today.week_left ?? '—'} />
              <Step
                label="Left today"
                value={budget.data.profile_visits_today.remaining}
                emphasize
              />
            </ol>
            {/* A narrow viewport can't fit this table at its natural width; scrolling it
                within the card (not jsdom-visible — only a real browser lays out overflow)
                keeps it from pushing the whole page wider than the screen. */}
            <div className="overflow-x-auto">
              <table className="w-full min-w-max text-left">
                <thead className="text-muted-foreground">
                  <tr>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      Action
                    </th>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      Today
                    </th>
                    <th scope="col" className="py-1 font-medium">
                      This week
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {budget.data.budgets.map((row) => (
                    <tr key={row.action} className="border-t border-border/60">
                      <th scope="row" className="py-1.5 pr-3 font-normal">
                        {ACTION_CLASS_LABELS[row.action] ?? row.action}
                      </th>
                      <td className="py-1.5 pr-3 tabular-nums">
                        {row.day.count} / {row.day.limit}
                      </td>
                      <td className="py-1.5 tabular-nums">
                        {row.week === null ? '—' : `${row.week.count} / ${row.week.limit}`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}

function Step({
  label,
  value,
  emphasize = false,
}: {
  label: string
  value: number | string
  emphasize?: boolean
}) {
  return (
    <li className="flex flex-col">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className={cn('tabular-nums', emphasize && 'font-semibold')}>{value}</span>
    </li>
  )
}

/** Heat: the score, its threshold, the slowdown multiplier, and when runs resume (spec 9.7). */
export function HeatPanel() {
  const heat = useQuery(heatQuery)

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Heat</CardTitle>
        <CardDescription>
          Rises on a throttle or a checkpoint, decays on its own. While warm, pacing slows down.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {heat.isPending && <p role="status">Loading…</p>}
        {heat.isError && <p role="alert">{message(heat.error)}</p>}
        {heat.isSuccess && (
          <>
            <p className="flex items-center gap-2">
              <span
                aria-hidden="true"
                className={cn(
                  'size-2 rounded-full',
                  heat.data.tripped ? 'bg-destructive' : 'bg-emerald-500',
                )}
              />
              <span role="status" className="font-medium">
                {heat.data.score.toFixed(2)} / {heat.data.threshold.toFixed(2)}
                {heat.data.tripped ? ' — over threshold, runs are skipped' : ''}
              </span>
            </p>
            <p className="text-muted-foreground">
              Pacing multiplier: {heat.data.multiplier.toFixed(2)}×
            </p>
            {heat.data.last_raised_at !== null && (
              <p className="text-muted-foreground">
                Last raised {formatWhen(heat.data.last_raised_at)}
              </p>
            )}
            {heat.data.resumes_at !== null && (
              <p className="text-muted-foreground">Resumes at {formatWhen(heat.data.resumes_at)}</p>
            )}
            {heat.data.cleared_at !== null && (
              <p className="text-muted-foreground">Cleared {formatWhen(heat.data.cleared_at)}</p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
