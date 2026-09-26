import { useQuery } from '@tanstack/react-query'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

import { browserQuery, statusQuery } from './api'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Browser status and launch instructions (spec 9.1, ADR 0002): netkeeper is
 * attach-only and never launches Chrome itself, so this shows the command
 * `netkeeper browser launch` prints rather than a button that would start one.
 *
 * "Status" here is only ever `can_start_runs` — whether *this* backend
 * process has a browser worker at all (started by `netkeeper serve`, spec
 * 9.4) — not whether Chrome is actually reachable or the session inside it is
 * healthy. That is what `netkeeper preflight` checks, and it has to attach to
 * find out, which a request handler may never do (CLAUDE.md); #175 ships no
 * API for it, so a real live check stays a terminal command and a follow-up
 * for this page.
 */
export function BrowserCard() {
  const browser = useQuery(browserQuery)
  const status = useQuery(statusQuery)

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
