import { useMutation, useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  ARM_LABEL,
  armText,
  mailboxStatusQuery,
  navigation,
  reasonText,
  startAuthorization,
  type Mailbox,
} from '@/features/mailboxes/api'

const STATUS_LABEL: Record<Mailbox['status'], string> = {
  ok: 'connected',
  reauth_required: 'needs re-authorizing',
  disabled: 'disconnected',
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function when(value: string | null): string {
  return value === null ? 'never' : new Date(value).toLocaleString()
}

/**
 * The Gmail connection (spec 11.5): whether an OAuth client is stored, which
 * account is connected and in what state, whether `netkeeper serve` may use it, and
 * how to connect or reconnect. Setting up, arming and disconnecting stay on Settings;
 * the one action here is **Re-authorize**, the same one the app-wide banner has.
 *
 * Reads `GET /mailboxes/status`, which asks the Keychain whether a client is
 * stored; a locked Keychain shows as an error here, not as "no client".
 */
export function ConnectionCard() {
  const status = useQuery(mailboxStatusQuery)
  const reauthorize = useMutation({
    mutationFn: startAuthorization,
    onSuccess: (url) => navigation.assign(url),
  })
  const live = status.data?.mailboxes.filter((mailbox) => mailbox.status !== 'disabled') ?? []

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Connection</CardTitle>
        <CardDescription>
          The account campaigns send from, through an OAuth client in your own Google Cloud project.
          The token lives in the Keychain, never here.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {status.isPending && <p role="status">Loading…</p>}
        {status.isError && <p role="alert">{message(status.error)}</p>}
        {status.isSuccess && (
          <>
            <p className="flex flex-wrap items-center gap-2">
              <span className="font-medium">OAuth client</span>
              <Badge variant={status.data.client_configured ? 'outline' : 'secondary'}>
                {status.data.client_configured ? 'configured' : 'not set'}
              </Badge>
              {status.data.client_id !== null && status.data.client_id !== undefined && (
                <code className="font-mono text-xs break-all">{status.data.client_id}</code>
              )}
            </p>

            {live.length === 0 ? (
              <p className="text-muted-foreground">
                No Gmail account is connected yet. Email steps wait until one is.
              </p>
            ) : (
              <ul className="space-y-2">
                {live.map((mailbox) => (
                  <li key={mailbox.id} className="space-y-1 rounded-lg border px-3 py-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{mailbox.email}</span>
                      <Badge variant={mailbox.status === 'ok' ? 'outline' : 'destructive'}>
                        {STATUS_LABEL[mailbox.status]}
                      </Badge>
                      <Badge variant={mailbox.arm === null ? 'secondary' : 'outline'}>
                        {ARM_LABEL[mailbox.arm ?? 'none']}
                      </Badge>
                    </div>
                    <p className="text-muted-foreground">{armText(mailbox)}</p>
                    {mailbox.status !== 'ok' && reasonText(mailbox.status_reason) !== null && (
                      <p className="text-muted-foreground">{reasonText(mailbox.status_reason)}</p>
                    )}
                    <p className="text-xs text-muted-foreground">
                      Token last refreshed {when(mailbox.checked_at)}
                    </p>
                    {mailbox.status === 'reauth_required' && (
                      <Button
                        size="sm"
                        onClick={() => reauthorize.mutate(mailbox.id)}
                        disabled={reauthorize.isPending}
                      >
                        {reauthorize.isPending ? 'Opening Google…' : 'Re-authorize'}
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {reauthorize.isError && (
              <p role="alert" className="text-destructive">
                {message(reauthorize.error)}
              </p>
            )}

            <section aria-label="Connect or reconnect" className="space-y-1">
              <h3 className="font-medium">
                {live.length === 0 ? 'Connect' : 'Reconnect or switch account'}
              </h3>
              <p className="text-muted-foreground">
                In a terminal, run{' '}
                {!status.data.client_configured && (
                  <>
                    <code className="font-mono text-xs">netkeeper gmail client &lt;file&gt;</code>{' '}
                    with the client JSON from Google Cloud, then{' '}
                  </>
                )}
                <code className="font-mono text-xs">netkeeper gmail login</code>: it prints Google’s
                page, waits for the redirect, and stores the token. Or use the guided setup on{' '}
                <Link to="/settings" className="underline underline-offset-4">
                  Settings
                </Link>
                .
              </p>
            </section>
          </>
        )}
      </CardContent>
    </Card>
  )
}
