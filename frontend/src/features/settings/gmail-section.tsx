import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { pollStatusKeys } from '@/features/poll-status/api'
import {
  ARM_LABEL,
  armMailbox,
  armText,
  checkMailbox,
  disarmMailbox,
  disconnectMailbox,
  mailboxKeys,
  mailboxStatusQuery,
  navigation,
  reasonText,
  startAuthorization,
  type Mailbox,
  type MailboxArm,
} from '@/features/mailboxes/api'

import { ClientForm } from './client-form'
import { GmailSetupWizard } from './gmail-setup-wizard'

/** What the OAuth callback said, from `/settings?gmail=...&reason=...`. */
export interface GmailOutcome {
  gmail?: string
  reason?: string
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

const STATUS_LABEL: Record<Mailbox['status'], string> = {
  ok: 'connected',
  reauth_required: 'needs re-authorizing',
  disabled: 'disconnected',
}

function when(value: string | null): string {
  return value === null ? 'never' : new Date(value).toLocaleString()
}

/**
 * Gmail auth (spec 11.5, P3-01): the OAuth client from your own Cloud project,
 * then one authorization on Google's page, guided step by step (#302). The token lives in the Keychain; this
 * page only ever sees the mailbox's address and health.
 */
export function GmailSection({ outcome }: { outcome: GmailOutcome }) {
  const queryClient = useQueryClient()
  const status = useQuery(mailboxStatusQuery)
  const [editingClient, setEditingClient] = useState(false)
  const [disconnecting, setDisconnecting] = useState<Mailbox | null>(null)
  const [arming, setArming] = useState<{ mailbox: Mailbox; mode: MailboxArm } | null>(null)

  // Arming, disarming, and disconnecting change what the header's poll status says.
  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: mailboxKeys.all }),
      queryClient.invalidateQueries({ queryKey: pollStatusKeys.all }),
    ])

  const connect = useMutation({
    mutationFn: startAuthorization,
    onSuccess: (url) => navigation.assign(url),
  })
  const check = useMutation({ mutationFn: checkMailbox, onSettled: refresh })
  const disconnect = useMutation({
    mutationFn: disconnectMailbox,
    onSuccess: async () => {
      setDisconnecting(null)
      await refresh()
    },
  })

  const arm = useMutation({
    mutationFn: ({ mailbox, mode }: { mailbox: Mailbox; mode: MailboxArm }) =>
      armMailbox(mailbox.id, mode),
    onSuccess: async () => {
      setArming(null)
      await refresh()
    },
  })
  const disarm = useMutation({ mutationFn: disarmMailbox, onSettled: refresh })

  const live = status.data?.mailboxes.filter((mailbox) => mailbox.status !== 'disabled') ?? []

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Gmail</CardTitle>
        <CardDescription>
          The account campaigns send from, through an OAuth client in your own Google Cloud project.
          The setup guide below walks you through it;{' '}
          <code className="font-mono">docs/gmail-setup.md</code> has the same steps.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {outcome.gmail === 'connected' && (
          <p role="status" className="rounded-lg bg-emerald-500/10 px-3 py-2">
            Gmail is connected.
          </p>
        )}
        {outcome.gmail === 'error' && (
          <p role="alert" className="rounded-lg bg-destructive/10 px-3 py-2 text-destructive">
            Gmail was not connected. {reasonText(outcome.reason) ?? 'Start again.'}
          </p>
        )}

        {status.isPending && <p role="status">Loading…</p>}
        {status.isError && <p role="alert">{message(status.error)}</p>}

        {status.isSuccess && (
          <GmailSetupWizard
            status={status.data}
            outcomeReason={outcome.gmail === 'error' ? outcome.reason : undefined}
            onConnect={(mailboxId) => connect.mutate(mailboxId)}
            connecting={connect.isPending}
          />
        )}

        {status.isSuccess && status.data.client_configured && (
          <section aria-label="OAuth client" className="space-y-2">
            <h3 className="font-medium">OAuth client</h3>
            {editingClient ? (
              <ClientForm
                onCancel={() => setEditingClient(false)}
                onSaved={() => setEditingClient(false)}
              />
            ) : (
              <div className="flex flex-wrap items-center gap-2">
                <code className="font-mono text-xs break-all">{status.data.client_id}</code>
                <Button variant="ghost" size="sm" onClick={() => setEditingClient(true)}>
                  Replace
                </Button>
              </div>
            )}
          </section>
        )}

        {status.isSuccess && status.data.mailboxes.length > 0 && (
          <section aria-label="Mailbox" className="space-y-2">
            <h3 className="font-medium">Mailbox</h3>
            {status.data.mailboxes.length > 0 && (
              <ul className="space-y-2">
                {status.data.mailboxes.map((mailbox) => (
                  <li key={mailbox.id} className="space-y-1 rounded-lg border px-3 py-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{mailbox.email}</span>
                      <Badge variant={mailbox.status === 'ok' ? 'outline' : 'destructive'}>
                        {STATUS_LABEL[mailbox.status]}
                      </Badge>
                    </div>
                    {mailbox.status !== 'disabled' && (
                      <p className="flex flex-wrap items-center gap-2">
                        <Badge variant={mailbox.arm === null ? 'secondary' : 'outline'}>
                          {ARM_LABEL[mailbox.arm ?? 'none']}
                        </Badge>
                        <span className="text-muted-foreground">{armText(mailbox)}</span>
                      </p>
                    )}
                    {mailbox.status !== 'ok' && reasonText(mailbox.status_reason) !== null && (
                      <p className="text-muted-foreground">{reasonText(mailbox.status_reason)}</p>
                    )}
                    <p className="text-xs text-muted-foreground">
                      Up to {mailbox.daily_cap} recipients a day · labels{' '}
                      <code className="font-mono">{mailbox.label_prefix}/…</code> · token last
                      refreshed {when(mailbox.checked_at)}
                    </p>
                    {mailbox.status !== 'disabled' && (
                      <div className="flex flex-wrap gap-2">
                        {mailbox.status === 'reauth_required' && (
                          <Button
                            size="sm"
                            onClick={() => connect.mutate(mailbox.id)}
                            disabled={connect.isPending}
                          >
                            Re-authorize
                          </Button>
                        )}
                        {mailbox.status === 'ok' && (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => check.mutate(mailbox.id)}
                            disabled={check.isPending}
                          >
                            {check.isPending ? 'Checking…' : 'Check now'}
                          </Button>
                        )}
                        {mailbox.arm === null && (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => setArming({ mailbox, mode: 'draft' })}
                          >
                            Arm for drafts
                          </Button>
                        )}
                        {mailbox.arm === 'draft' && (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => setArming({ mailbox, mode: 'send' })}
                            disabled={mailbox.message_id_verified_at === null}
                          >
                            Arm to send
                          </Button>
                        )}
                        {mailbox.arm !== null && (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => disarm.mutate(mailbox.id)}
                            disabled={disarm.isPending}
                          >
                            Disarm
                          </Button>
                        )}
                        <Button variant="ghost" size="sm" onClick={() => setDisconnecting(mailbox)}>
                          Disconnect
                        </Button>
                      </div>
                    )}
                    {mailbox.arm === 'draft' && mailbox.message_id_verified_at === null && (
                      <p className="text-xs text-muted-foreground">
                        Arming to send waits until netkeeper finds one of its drafts here by its
                        Message-ID, which the next drafts check does after a draft is made.
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {disarm.isError && (
              <p role="alert" className="text-destructive">
                {message(disarm.error)}
              </p>
            )}
            {check.isError && (
              <p role="alert" className="text-destructive">
                {message(check.error)}
              </p>
            )}
            {live.length === 0 && (
              <p className="text-muted-foreground">
                No mailbox is connected. Connect one in step 7 of the setup guide.
              </p>
            )}
          </section>
        )}
        {connect.isError && (
          <p role="alert" className="text-destructive">
            {message(connect.error)}
          </p>
        )}
      </CardContent>

      <ConfirmDialog
        open={disconnecting !== null}
        onOpenChange={(open) => {
          if (!open) setDisconnecting(null)
        }}
        title="Disconnect this mailbox?"
        confirmLabel="Disconnect"
        pending={disconnect.isPending}
        error={disconnect.isError ? message(disconnect.error) : null}
        onConfirm={() =>
          disconnecting === null ? Promise.resolve() : disconnect.mutateAsync(disconnecting.id)
        }
      >
        netkeeper forgets {disconnecting?.email}’s token and stops using it. Email steps pause.
        Campaigns that sent from it keep their history. To revoke access on Google’s side too,
        remove netkeeper from your Google Account’s third-party access page.
      </ConfirmDialog>

      <ConfirmDialog
        open={arming !== null}
        onOpenChange={(open) => {
          if (!open) setArming(null)
        }}
        title={
          arming?.mode === 'send' ? 'Arm this mailbox to send?' : 'Arm this mailbox for drafts?'
        }
        confirmLabel={arming?.mode === 'send' ? 'Arm to send' : 'Arm for drafts'}
        pending={arm.isPending}
        error={arm.isError ? message(arm.error) : null}
        onConfirm={() => (arming === null ? Promise.resolve() : arm.mutateAsync(arming))}
      >
        {arming?.mode === 'send'
          ? `netkeeper serve will send campaign email from ${arming.mailbox.email} on its own, with no one pressing Send. Disarm stops it from the next minute.`
          : `netkeeper serve will make each due campaign step a Gmail draft in ${arming?.mailbox.email ?? ''}, send steps included. You send each draft yourself.`}
      </ConfirmDialog>
    </Card>
  )
}
