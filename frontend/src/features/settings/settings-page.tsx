import { useQuery } from '@tanstack/react-query'

import { meQuery } from '@/api/queries'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

import { AboutYouSection } from './about-you-section'
import { ConfigSections } from './config-section'
import { DoNotSendSection } from './do-not-send-section'
import { type GmailOutcome, GmailSection } from './gmail-section'
import { PostureSection } from './posture-section'
import { SendingHoursSection } from './sending-hours-section'

export function SettingsPage({ gmail = {} }: { gmail?: GmailOutcome }) {
  const me = useQuery(meQuery)

  return (
    <div className="grid max-w-3xl gap-4">
      <Card size="sm">
        <CardHeader>
          <CardTitle level={2}>Settings</CardTitle>
          <CardDescription>
            Gmail auth, sending hours, about you, LinkedIn budgets and hours, campaign defaults,
            LLM, backups. You change them here; config.toml is optional, and a value it sets wins.
          </CardDescription>
        </CardHeader>
      </Card>

      <GmailSection outcome={gmail} />

      <SendingHoursSection />

      <AboutYouSection />

      <ConfigSections />

      <DoNotSendSection />

      <PostureSection />

      <Card size="sm">
        <CardHeader>
          <CardTitle level={2}>Current user</CardTitle>
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
