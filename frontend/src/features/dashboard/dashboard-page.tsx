import { useQuery, useQueryClient } from '@tanstack/react-query'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { listsQuery } from '@/features/crm/api'
import { useEventStreamStatus } from '@/features/events/event-stream-context'
import { useServerEvent } from '@/features/events/use-server-event'
import { linkedinKeys } from '@/features/linkedin/api'
import { cn } from '@/lib/utils'

import { openImportsQuery, statsQuery } from './api'
import {
  BrowserHealthCard,
  BudgetHeatCard,
  ChangedJobsCard,
  NextLinkedInRunCard,
  NextSendsCard,
  RepliesCard,
} from './glance-cards'
import { MailboxCard } from './mailbox-card'
import { SetupStepCard } from './setup-step-card'
import { buildSetupSteps } from './setup-steps'

/**
 * A LinkedIn run starting or ending moves the schedule, the last run, the
 * session flag, budget, and heat, and none of them says so on its own; refetch
 * them all once per run, as the LinkedIn page does (`use-run-events.ts`).
 */
function useLinkedInRefresh(): void {
  const queryClient = useQueryClient()
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.all })
  }
  useServerEvent('run.started', refresh)
  useServerEvent('run.finished', refresh)
}

/**
 * The dashboard: at a glance, what happens next and whether anything is
 * unhealthy (P3-12, `glance-cards.tsx`); below it, not a set of scaffold cards, but the setup path — import,
 * review, triage, build a list, export — with each step's real count and the
 * control that advances it (issue #115, spec 10.1, 14.3).
 *
 * `contacts/stats` is the one query every step's state is gated on: without
 * it nothing here is knowable, so its failure is the page's failure, shown
 * the same way the old scaffold's "backend unreachable" card was. The
 * draft-imports and lists queries feed one step each; a failure in either
 * degrades that one step (`setup-steps.ts` says how) rather than the page.
 */
export function DashboardPage() {
  const stats = useQuery(statsQuery)
  const openImports = useQuery(openImportsQuery)
  const lists = useQuery(listsQuery)
  const stream = useEventStreamStatus()
  useLinkedInRefresh()

  if (stats.isPending) {
    return (
      <p role="status" className="text-muted-foreground">
        Checking your setup…
      </p>
    )
  }

  if (stats.isError) {
    return (
      <Card size="sm" className="max-w-xl" role="alert">
        <CardHeader>
          <CardTitle level={2}>Backend unreachable</CardTitle>
        </CardHeader>
        <CardContent>
          <p>
            Start it with <code className="font-mono">make dev</code>. The setup path needs it to
            say where you are.
          </p>
        </CardContent>
      </Card>
    )
  }

  const hasContacts = stats.data.total > 0
  const steps = buildSetupSteps({
    stats: stats.data,
    openImports: openImports.data,
    openImportsPending: openImports.isPending,
    openImportsUnavailable: openImports.isError,
    lists: lists.data,
    listsUnavailable: lists.isError,
  })

  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-muted-foreground">What happens next, and anything that needs you.</p>
        <span className="flex items-center gap-2 text-sm text-muted-foreground">
          <span
            aria-hidden="true"
            className={cn(
              'size-2 rounded-full',
              stream === 'connected' ? 'bg-emerald-500' : 'bg-muted-foreground/50',
            )}
          />
          <span role="status">
            Live updates {stream === 'connected' ? 'connected' : 'disconnected'}
          </span>
        </span>
      </div>

      {!hasContacts && (
        <Card size="sm" className="border-primary/40">
          <CardHeader>
            <CardTitle level={2}>Start here</CardTitle>
            <CardDescription>
              Nothing is imported yet. Bring in a CSV or your LinkedIn archive first — every other
              step needs contacts to work with.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      <section aria-label="At a glance" className="grid gap-3 sm:grid-cols-2">
        <NextSendsCard />
        <NextLinkedInRunCard />
        <BrowserHealthCard />
        <MailboxCard />
        <BudgetHeatCard />
        <RepliesCard />
        <div className="sm:col-span-2">
          <ChangedJobsCard />
        </div>
      </section>

      <ol aria-label="Setup path" className="flex flex-col gap-3">
        {steps.map((step, index) => (
          <li key={step.key}>
            <SetupStepCard step={step} index={index + 1} />
          </li>
        ))}
      </ol>
    </div>
  )
}
