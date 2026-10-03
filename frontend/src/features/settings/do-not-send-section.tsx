import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'

import { type DoNotSendEntry, doNotSendQuery, removeDoNotSend } from './api'

const REASON_LABELS: Record<DoNotSendEntry['reason'], string> = {
  bounced: 'bounced',
  invalid: 'invalid',
  opted_out: 'opted out',
  manual: 'added by hand',
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * The do-not-send list (#238): addresses no campaign sends to, whichever contact holds
 * them. An address lands here on a bounce, an opt-out, or a person marking it bounced or
 * invalid. Removing one is the only way off, so each removal asks first.
 */
export function DoNotSendSection() {
  const entries = useQuery(doNotSendQuery)
  const queryClient = useQueryClient()
  const [removing, setRemoving] = useState<DoNotSendEntry | null>(null)
  const remove = useMutation({
    mutationFn: removeDoNotSend,
    onSuccess: () => setRemoving(null),
    onSettled: () => queryClient.invalidateQueries({ queryKey: doNotSendQuery.queryKey }),
  })

  return (
    <Card size="sm">
      <CardHeader>
        <h2 className="font-heading text-sm leading-snug font-medium">Do-not-send list</h2>
        <CardDescription>
          Addresses no campaign sends to, whichever contact holds them. An address with a +tag is
          its own address. Merging a contact that still has an address marked bounced or invalid
          puts the address back on the list, even if you removed it here.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {entries.isPending && <p role="status">Loading…</p>}
        {entries.isError && <p role="alert">{message(entries.error)}</p>}
        {entries.isSuccess && entries.data.length === 0 && (
          <p className="text-muted-foreground">No addresses on the list.</p>
        )}
        {entries.isSuccess && entries.data.length > 0 && (
          <ul className="divide-y divide-border/60" aria-label="Do-not-send addresses">
            {entries.data.map((entry) => (
              <li key={entry.id} className="flex items-center justify-between gap-2 py-2">
                <div className="min-w-0">
                  <p className="font-mono break-all">{entry.email}</p>
                  <p className="text-xs text-muted-foreground">{REASON_LABELS[entry.reason]}</p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    remove.reset()
                    setRemoving(entry)
                  }}
                  aria-label={`Remove ${entry.email}`}
                >
                  Remove
                </Button>
              </li>
            ))}
          </ul>
        )}
      </CardContent>

      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => {
          if (!open) setRemoving(null)
        }}
        title="Remove this address from the list?"
        confirmLabel="Remove"
        pending={remove.isPending}
        error={remove.isError ? message(remove.error) : null}
        onConfirm={() => (removing === null ? Promise.resolve() : remove.mutateAsync(removing.id))}
      >
        Campaigns may send to {removing?.email} again.{' '}
        {removing !== null && removing.bounced && removing.reason !== 'bounced' && (
          <strong>This address also bounced; removing the entry allows email to it again. </strong>
        )}
        A contact that still has it marked bounced or invalid stays excluded until you mark it OK
        there, and merging that contact puts the address back on the list.
      </ConfirmDialog>
    </Card>
  )
}
