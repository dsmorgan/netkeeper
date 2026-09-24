import { useQuery } from '@tanstack/react-query'

import { meQuery } from '@/api/queries'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

import { PostureSection } from './posture-section'

export function SettingsPage() {
  const me = useQuery(meQuery)

  return (
    <div className="grid max-w-3xl gap-4">
      <Card size="sm">
        <CardHeader>
          <CardTitle>Settings</CardTitle>
          <CardDescription>
            Gmail auth, pacing, budgets, send windows, LLM, [me] merge fields, backups.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex items-center gap-2">
          <Badge variant="outline">Phase 0</Badge>
          <span className="text-muted-foreground">
            Only the current user and posture are wired up. Each section arrives with its feature's
            phase.
          </span>
        </CardContent>
      </Card>

      <PostureSection />

      <Card size="sm">
        <CardHeader>
          <CardTitle>Current user</CardTitle>
          <CardDescription>
            Raw <code className="font-mono">GET /api/v1/me</code> payload.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {me.isPending && <p className="text-muted-foreground">Loading…</p>}
          {me.isError && <p className="text-muted-foreground">Backend unreachable.</p>}
          {me.isSuccess && (
            <pre className="overflow-x-auto rounded-md bg-muted p-3 font-mono text-xs">
              {JSON.stringify(me.data, null, 2)}
            </pre>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
