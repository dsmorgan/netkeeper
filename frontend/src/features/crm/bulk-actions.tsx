/**
 * A bulk action on everyone a list selects, count confirmed.
 *
 * The flow is the backend's: `POST /contacts/bulk/count` answers with a count
 * and a token bound to this user, this action, this value and reason, and this
 * exact selection; `POST /contacts/bulk` takes the token and counts again
 * inside the writer transaction.
 *
 * What the token is bound to is why `confirmed` holds the whole request and not
 * just the token. The selection arrives as a prop, and for a static list it is
 * the ids of a members query that any list write invalidates — so removing a
 * member while the count is on screen changes the selection under a
 * confirmation nobody re-read. Applying `confirmed.request` rather than a fresh
 * one means the action that lands is the action that was counted, and the
 * server's digest check is a backstop rather than the only thing standing
 * between the person and a surprise.
 *
 * The refusal kinds, and the words for them, come from the Contacts table's
 * `readBulkRefusal` and `refusalMessage` (spec 14.1). A moved count and an
 * expired token are the confirmation working, and they read differently; a
 * rejected token is a dead end that a fresh count reopens. Nothing is applied
 * to rows the person did not see a count for.
 */
import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'

import { applyBulk, countBulk, readBulkRefusal, refusalMessage } from './api'
import type { BulkConfirmable, BulkRefusal } from './api'
import { Callout, ErrorNote } from './controls'
import type { BulkAction, BulkCountOut, BulkSelection, ContactMet } from './types'

const ACTIONS: ReadonlyArray<{ value: BulkAction; label: string }> = [
  { value: 'set_met', label: 'Set “met”' },
  { value: 'archive', label: 'Archive' },
  { value: 'unarchive', label: 'Unarchive' },
  { value: 'set_do_not_contact', label: 'Set do-not-contact' },
]

const MET_VALUES: readonly ContactMet[] = ['unknown', 'met', 'not_met', 'skip']

/** A count and the exact request it was issued for. Apply this, not a fresh one. */
interface Confirmed {
  request: BulkConfirmable
  counted: BulkCountOut
}

export function BulkActionBar({
  selection,
  onApplied,
}: {
  selection: BulkSelection
  onApplied: () => void
}) {
  const [action, setAction] = useState<BulkAction>('archive')
  const [met, setMet] = useState<ContactMet>('met')
  const [flag, setFlag] = useState(true)
  const [reason, setReason] = useState('')
  const [confirmed, setConfirmed] = useState<Confirmed | null>(null)
  const [refusal, setRefusal] = useState<BulkRefusal | null>(null)
  const [applied, setApplied] = useState<number | null>(null)

  const request = (): BulkConfirmable => ({
    selection,
    action,
    value: action === 'set_met' ? met : action === 'set_do_not_contact' ? flag : null,
    reason: action === 'set_do_not_contact' && reason.trim() !== '' ? reason.trim() : null,
  })

  /** Any change to what would be sent voids the count it was issued against. */
  const invalidate = () => {
    setConfirmed(null)
    setRefusal(null)
  }

  const count = useMutation({
    mutationFn: (body: BulkConfirmable) => countBulk(body),
    onSuccess: (counted, body) => {
      setConfirmed({ request: body, counted })
      setRefusal(null)
      setApplied(null)
    },
    onError: (error) => setRefusal(readBulkRefusal(error)),
  })

  const apply = useMutation({
    mutationFn: ({ request: body, counted }: Confirmed) => applyBulk(body, counted.token),
    onSuccess: (affected) => {
      setApplied(affected)
      setConfirmed(null)
      setRefusal(null)
      onApplied()
    },
    onError: (error) => {
      // A refused action changed nothing, and the token is spent either way, so
      // the way forward is a fresh count rather than a retry.
      setConfirmed(null)
      setRefusal(readBulkRefusal(error))
    },
  })

  const busy = count.isPending || apply.isPending

  return (
    <div data-slot="bulk-actions" className="space-y-2 rounded-lg border p-3">
      <div className="flex flex-wrap items-end gap-2">
        <div className="grid gap-1">
          <Label htmlFor="bulk-action">Bulk action</Label>
          <Select
            id="bulk-action"
            value={action}
            onChange={(event) => {
              setAction(event.target.value as BulkAction)
              invalidate()
            }}
          >
            {ACTIONS.map((candidate) => (
              <option key={candidate.value} value={candidate.value}>
                {candidate.label}
              </option>
            ))}
          </Select>
        </div>
        {action === 'set_met' && (
          <div className="grid gap-1">
            <Label htmlFor="bulk-met">Value</Label>
            <Select
              id="bulk-met"
              value={met}
              onChange={(event) => {
                setMet(event.target.value as ContactMet)
                invalidate()
              }}
            >
              {MET_VALUES.map((value) => (
                <option key={value} value={value}>
                  {value.replace(/_/g, ' ')}
                </option>
              ))}
            </Select>
          </div>
        )}
        {action === 'set_do_not_contact' && (
          <>
            <div className="grid gap-1">
              <Label htmlFor="bulk-dnc">Value</Label>
              <Select
                id="bulk-dnc"
                value={flag ? 'true' : 'false'}
                onChange={(event) => {
                  setFlag(event.target.value === 'true')
                  invalidate()
                }}
              >
                <option value="true">do not contact</option>
                <option value="false">ok to contact</option>
              </Select>
            </div>
            <div className="grid gap-1">
              <Label htmlFor="bulk-reason">Reason</Label>
              <Input
                id="bulk-reason"
                value={reason}
                onChange={(event) => {
                  setReason(event.target.value)
                  invalidate()
                }}
              />
            </div>
          </>
        )}
        <Button variant="outline" onClick={() => count.mutate(request())} disabled={busy}>
          {refusal === null ? 'Count first' : 'Count again'}
        </Button>
      </div>

      {count.isError && refusal === null && (
        <ErrorNote label="Could not count the selection" error={count.error} />
      )}

      {refusal !== null && (
        <Callout tone="warning" title="Nothing was changed">
          <p>{refusalMessage(refusal)}</p>
          <p>Count again to see what the selection holds now.</p>
        </Callout>
      )}

      {confirmed !== null && (
        <Callout tone="info">
          <p>
            This affects <strong>{confirmed.counted.count.toLocaleString()}</strong>{' '}
            {confirmed.counted.count === 1 ? 'contact' : 'contacts'} — {confirmed.counted.describe}.
          </p>
          <Button
            className="mt-2"
            onClick={() => apply.mutate(confirmed)}
            disabled={busy || confirmed.counted.count === 0}
          >
            {apply.isPending
              ? 'Applying…'
              : confirmed.counted.count === 0
                ? 'Nothing to apply'
                : `Apply to ${confirmed.counted.count.toLocaleString()}`}
          </Button>
        </Callout>
      )}

      {applied !== null && (
        <p role="status" className="text-sm text-muted-foreground">
          Applied to {applied.toLocaleString()} contacts.
        </p>
      )}
    </div>
  )
}
