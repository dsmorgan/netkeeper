import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useCallback } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useServerEvent } from '@/features/events/use-server-event'
import {
  ARM_LABEL,
  armText,
  mailboxKeys,
  mailboxListQuery,
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

function when(value: string | null): string {
  return value === null ? 'never' : new Date(value).toLocaleString()
}

/**
 * Which mailbox the card shows: the one live account (`ok` or
 * `reauth_required`) when there is one, so a person acting on it sees it
 * first; otherwise the most recently touched `disabled` row, so a mailbox
 * someone disconnected still reads as "disconnected" rather than "nothing
 * connected". An empty list is the only case with no mailbox to show.
 */
function pickMailbox(mailboxes: Mailbox[]): Mailbox | undefined {
  const live = mailboxes.find((mailbox) => mailbox.status !== 'disabled')
  if (live !== undefined) return live
  return [...mailboxes].sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0]
}

/**
 * The mailbox's health on the dashboard (issue #268, CP5 spec 10.1): the
 * connected address and status, when its token was last refreshed, and a
 * link to Settings — or **Re-authorize** when Google needs it again — and
 * whether `serve` may use it: not armed, drafts only, or sending (#277).
 *
 * Reads `GET /api/v1/mailboxes`, never `/status`: the list comes from the
 * database alone, so this still shows while the Keychain is locked (#256
 * item 5) — the same reasoning as the re-auth banner
 * (`mailboxes/reauth-banner.tsx`), which this mirrors. It refreshes on the
 * `mailbox.status` server event the backend's poll publishes, the same as
 * the banner, so a status change appears without a reload.
 */
export function MailboxCard() {
  const queryClient = useQueryClient()
  const mailboxes = useQuery(mailboxListQuery)
  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: mailboxKeys.all })
  }, [queryClient])
  useServerEvent('mailbox.status', refresh)

  const reauthorize = useMutation({
    mutationFn: startAuthorization,
    onSuccess: (url) => navigation.assign(url),
  })

  if (mailboxes.isPending) {
    return (
      <Card size="sm">
        <CardHeader>
          <CardTitle level={2}>Mailbox</CardTitle>
        </CardHeader>
        <CardContent>
          <p role="status" className="text-muted-foreground">
            Checking…
          </p>
        </CardContent>
      </Card>
    )
  }

  if (mailboxes.isError) {
    return (
      <Card size="sm">
        <CardHeader>
          <CardTitle level={2}>Mailbox</CardTitle>
        </CardHeader>
        <CardContent>
          <p role="alert" className="text-muted-foreground">
            Mailbox status could not be checked.
          </p>
        </CardContent>
      </Card>
    )
  }

  const mailbox = pickMailbox(mailboxes.data)

  if (mailbox === undefined) {
    return (
      <Card size="sm">
        <CardHeader>
          <CardTitle level={2}>Mailbox</CardTitle>
          <CardDescription>No Gmail account is connected yet.</CardDescription>
        </CardHeader>
        <CardContent>
          <Button size="sm" variant="outline" render={<Link to="/settings" />}>
            Set up Gmail
          </Button>
        </CardContent>
      </Card>
    )
  }

  const reason = mailbox.status !== 'ok' ? reasonText(mailbox.status_reason) : null

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Mailbox</CardTitle>
        <CardDescription>{mailbox.email}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-3">
          <Badge variant={mailbox.status === 'ok' ? 'outline' : 'destructive'}>
            {STATUS_LABEL[mailbox.status]}
          </Badge>
          <span className="text-sm text-muted-foreground">
            Token last refreshed {when(mailbox.checked_at)}
          </span>
        </div>
        {mailbox.status !== 'disabled' && (
          <div className="flex flex-wrap items-center gap-3">
            <Badge variant={mailbox.arm === null ? 'secondary' : 'outline'}>
              {ARM_LABEL[mailbox.arm ?? 'none']}
            </Badge>
            <span className="text-sm text-muted-foreground">{armText(mailbox)}</span>
          </div>
        )}
        {reason !== null && <p className="text-sm text-muted-foreground">{reason}</p>}
        <div className="flex flex-wrap items-center gap-2">
          {mailbox.status === 'reauth_required' ? (
            <Button
              size="sm"
              onClick={() => reauthorize.mutate(mailbox.id)}
              disabled={reauthorize.isPending}
            >
              {reauthorize.isPending ? 'Opening Google…' : 'Re-authorize'}
            </Button>
          ) : (
            <Button size="sm" variant="ghost" render={<Link to="/settings" />}>
              Settings
            </Button>
          )}
        </div>
        {reauthorize.isError && (
          <p role="alert" className="text-sm text-destructive">
            {(reauthorize.error as Error).message}
          </p>
        )}
      </CardContent>
    </Card>
  )
}
