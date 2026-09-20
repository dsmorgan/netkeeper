import { useQuery } from '@tanstack/react-query'

import { healthQuery, meQuery } from '@/api/queries'
import { Facts } from '@/components/facts'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useEventStreamStatus } from '@/features/events/event-stream-context'
import { cn } from '@/lib/utils'

export function DashboardPage() {
  const health = useQuery(healthQuery)
  const me = useQuery(meQuery)
  const stream = useEventStreamStatus()

  return (
    <div className="grid max-w-3xl gap-4 sm:grid-cols-2">
      <Card size="sm">
        <CardHeader>
          <CardTitle>Backend</CardTitle>
        </CardHeader>
        <CardContent>
          {health.isPending && <p className="text-muted-foreground">Checking…</p>}
          {health.isError && (
            <p>
              Backend unreachable. Start it with <code className="font-mono">make dev</code>.
            </p>
          )}
          {health.isSuccess && (
            <Facts
              items={[
                ['Status', health.data.status],
                ['Version', health.data.version],
              ]}
            />
          )}
        </CardContent>
      </Card>

      <Card size="sm">
        <CardHeader>
          <CardTitle>Current user</CardTitle>
        </CardHeader>
        <CardContent>
          {me.isPending && <p className="text-muted-foreground">Loading…</p>}
          {me.isError && (
            <p className="text-muted-foreground">Unavailable until the backend is reachable.</p>
          )}
          {me.isSuccess && (
            <Facts
              items={[
                ['Name', me.data.display_name],
                ['Email', me.data.email],
                ['Kind', me.data.kind],
                ['Timezone', me.data.timezone],
              ]}
            />
          )}
        </CardContent>
      </Card>

      <Card size="sm">
        <CardHeader>
          <CardTitle>Event stream</CardTitle>
          <CardDescription>
            Task progress, run status, mailbox and browser health arrive here once P0-04 lands.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex items-center gap-2">
          <span
            aria-hidden="true"
            className={cn(
              'size-2 rounded-full',
              stream === 'connected' ? 'bg-emerald-500' : 'bg-muted-foreground/50',
            )}
          />
          <span role="status">{stream === 'connected' ? 'Connected' : 'Disconnected'}</span>
          <span className="text-muted-foreground">· reconnects automatically</span>
        </CardContent>
      </Card>

      <Card size="sm">
        <CardHeader>
          <CardTitle>Coming later</CardTitle>
          <CardDescription>
            Next fires, budgets, heat, mailbox and browser health, replies this week, changed-jobs
            prompts.
          </CardDescription>
        </CardHeader>
      </Card>
    </div>
  )
}
