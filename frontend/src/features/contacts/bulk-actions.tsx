import { useMutation } from '@tanstack/react-query'
import { ChevronDown } from 'lucide-react'
import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import {
  Menu,
  MenuContent,
  MenuGroupLabel,
  MenuItem,
  MenuSeparator,
  MenuTrigger,
} from '@/components/ui/menu'

import {
  applyBulk,
  countBulk,
  readBulkRefusal,
  type BulkConfirmable,
  type BulkRefusal,
} from './api'
import type { BulkAction, BulkCountOut, BulkSelection, ContactMet } from './types'

interface Choice {
  label: string
  action: BulkAction
  value?: ContactMet | boolean
}

const CHOICES: readonly Choice[] = [
  { label: 'Mark as met', action: 'set_met', value: 'met' },
  { label: 'Mark as not met', action: 'set_met', value: 'not_met' },
  { label: 'Mark as skipped', action: 'set_met', value: 'skip' },
  { label: 'Clear met', action: 'set_met', value: 'unknown' },
  { label: 'Set do not contact', action: 'set_do_not_contact', value: true },
  { label: 'Allow contact again', action: 'set_do_not_contact', value: false },
  { label: 'Archive', action: 'archive' },
  { label: 'Unarchive', action: 'unarchive' },
]

export interface BulkBarProps {
  /** What the action would apply to: the picked ids, or the whole filter. */
  selection: BulkSelection
  /** How many rows are picked, or the filter's total when the filter is selected. */
  selectedCount: number
  everything: boolean
  /** Rows matching the filter, so "select all of them" can say how many. */
  total: number
  onSelectEverything: () => void
  onClear: () => void
  /** Called once an action landed, with how many contacts it touched. */
  onApplied: (affected: number) => void
}

/**
 * The bar that appears once rows are picked, and the confirmation behind it.
 *
 * A bulk action applies to a selection, so the person confirms a count rather
 * than a list (spec 10.1, 14.1): the count comes from the server with a token
 * bound to it, and the action sends that token back. When the count has moved
 * under the dialog the server refuses and nothing is changed — that refusal is
 * the feature, so it is spelled out and offers to count again.
 */
export function BulkBar({
  selection,
  selectedCount,
  everything,
  total,
  onSelectEverything,
  onClear,
  onApplied,
}: BulkBarProps) {
  const [pending, setPending] = useState<Choice | null>(null)

  return (
    <div
      role="region"
      aria-label="Bulk actions"
      className="flex flex-wrap items-center gap-3 rounded-xl bg-muted/60 px-3 py-2 text-sm"
    >
      <span className="font-medium">
        {everything
          ? `All ${total.toLocaleString()} matching contacts selected`
          : `${selectedCount.toLocaleString()} selected`}
      </span>
      {!everything && total > selectedCount && (
        <Button variant="link" size="sm" onClick={onSelectEverything}>
          Select all {total.toLocaleString()} matching this filter
        </Button>
      )}
      <Menu>
        <MenuTrigger
          render={
            <Button variant="outline" size="sm">
              Bulk actions
              <ChevronDown data-icon="inline-end" />
            </Button>
          }
        />
        <MenuContent align="start">
          <MenuGroupLabel>Apply to the selection</MenuGroupLabel>
          {CHOICES.map((choice) => (
            <MenuItem key={choice.label} onClick={() => setPending(choice)}>
              {choice.label}
            </MenuItem>
          ))}
          <MenuSeparator />
          <MenuItem onClick={onClear}>Clear selection</MenuItem>
        </MenuContent>
      </Menu>
      <Button variant="ghost" size="sm" onClick={onClear}>
        Clear selection
      </Button>

      {pending && (
        <BulkConfirmDialog
          choice={pending}
          selection={selection}
          onClose={() => setPending(null)}
          onApplied={(affected) => {
            setPending(null)
            onApplied(affected)
          }}
        />
      )}
    </div>
  )
}

function BulkConfirmDialog({
  choice,
  selection,
  onClose,
  onApplied,
}: {
  choice: Choice
  selection: BulkSelection
  onClose: () => void
  onApplied: (affected: number) => void
}) {
  // A reason is part of what the token binds, so it is settled before the
  // count; everything else is fixed by the menu item that opened this.
  const takesReason = choice.action === 'set_do_not_contact' && choice.value === true
  const [composing, setComposing] = useState(takesReason)
  const [reason, setReason] = useState('')
  const [counted, setCounted] = useState<BulkCountOut | null>(null)
  const [refusal, setRefusal] = useState<BulkRefusal | null>(null)

  const confirmable: BulkConfirmable = {
    action: choice.action,
    selection,
    ...(choice.value === undefined ? {} : { value: choice.value }),
    ...(takesReason && reason.trim() !== '' ? { reason: reason.trim() } : {}),
  }

  const count = useMutation({
    mutationFn: (body: BulkConfirmable) => countBulk(body),
    onSuccess: (result) => {
      setCounted(result)
      setRefusal(null)
    },
    onError: (error) => setRefusal(readBulkRefusal(error)),
  })

  const apply = useMutation({
    mutationFn: (token: string) => applyBulk(confirmable, token),
    onSuccess: onApplied,
    onError: (error) => {
      // A refused action changed nothing; the token is spent either way, so the
      // way forward is a fresh count.
      setCounted(null)
      setRefusal(readBulkRefusal(error))
    },
  })

  const recount = count.mutate
  useEffect(() => {
    if (!composing) recount(confirmable)
    // Counting is driven by leaving the compose step, not by every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [composing, recount])

  const busy = count.isPending || apply.isPending

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent aria-label={`Confirm: ${choice.label}`}>
        <DialogTitle>{choice.label}</DialogTitle>

        {composing && (
          <>
            <DialogDescription>
              The reason is part of what you confirm, so it is settled before the count.
            </DialogDescription>
            <label className="grid gap-1.5">
              <span className="text-muted-foreground">Reason (optional)</span>
              <Input
                autoFocus
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') setComposing(false)
                }}
              />
            </label>
          </>
        )}

        {!composing && count.isPending && (
          <DialogDescription>Counting the selection…</DialogDescription>
        )}

        {!composing && refusal && (
          <div
            role="alert"
            className="grid gap-2 rounded-lg bg-destructive/10 p-3 text-destructive"
          >
            <p>{refusalMessage(refusal)}</p>
            <p className="text-destructive/80">Nothing was changed.</p>
          </div>
        )}

        {!composing && counted && (
          <DialogDescription>
            This will {choice.label.toLowerCase()} on{' '}
            <strong className="text-foreground">{counted.count.toLocaleString()}</strong>{' '}
            {counted.count === 1 ? 'contact' : 'contacts'}: {counted.describe}.
            {takesReason &&
              (reason.trim() === '' ? ' No reason given.' : ` Reason: ${reason.trim()}.`)}
          </DialogDescription>
        )}

        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={apply.isPending}>
            Cancel
          </Button>
          {composing ? (
            <Button size="sm" onClick={() => setComposing(false)}>
              Count the selection
            </Button>
          ) : (
            <>
              {takesReason && (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={busy}
                  onClick={() => {
                    setCounted(null)
                    setRefusal(null)
                    setComposing(true)
                  }}
                >
                  Edit reason
                </Button>
              )}
              {refusal ? (
                <Button size="sm" onClick={() => count.mutate(confirmable)} disabled={busy}>
                  Count again
                </Button>
              ) : (
                <Button
                  size="sm"
                  disabled={busy || counted === null || counted.count === 0}
                  onClick={() => counted && apply.mutate(counted.token)}
                >
                  {apply.isPending
                    ? 'Applying…'
                    : counted
                      ? `Apply to ${counted.count.toLocaleString()}`
                      : 'Apply'}
                </Button>
              )}
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function refusalMessage(refusal: BulkRefusal): string {
  switch (refusal.kind) {
    case 'count_mismatch':
      return (
        `The selection changed while the confirmation was open: it now matches ` +
        `${refusal.actual.toLocaleString()} contacts, not the ${refusal.expected.toLocaleString()} you confirmed.`
      )
    case 'expired':
      return 'This confirmation expired; a count is good for five minutes.'
    case 'rejected':
      return `The confirmation was refused: ${refusal.detail}.`
    case 'failed':
      return `The action could not be applied: ${refusal.detail}.`
  }
}
