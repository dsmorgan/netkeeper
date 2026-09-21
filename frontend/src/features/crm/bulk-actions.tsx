/**
 * A bulk action on everyone a list selects, count confirmed.
 *
 * The flow is the backend's: `POST /contacts/bulk/count` answers with a count
 * and a token bound to this user, this action, this value and reason, and this
 * exact selection; `POST /contacts/bulk` takes the token and counts again
 * inside the writer transaction. A refusal means the world moved between the
 * two — someone archived a contact, or the token aged past five minutes — so
 * this asks for a fresh count and shows the new number rather than reporting a
 * failure. Nothing is applied to rows the person did not see a count for.
 */
import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

import { ConfirmationStale, applyBulk, countBulk } from './api'
import type { BulkRequest } from './api'
import { Callout, ErrorNote, NativeSelect } from './controls'
import type { BulkAction, BulkCountOut, BulkSelection, ContactMet } from './types'

const ACTIONS: ReadonlyArray<{ value: BulkAction; label: string }> = [
  { value: 'set_met', label: 'Set “met”' },
  { value: 'archive', label: 'Archive' },
  { value: 'unarchive', label: 'Unarchive' },
  { value: 'set_do_not_contact', label: 'Set do-not-contact' },
]

const MET_VALUES: readonly ContactMet[] = ['unknown', 'met', 'not_met', 'skip']

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
  const [pending, setPending] = useState<BulkCountOut | null>(null)
  const [moved, setMoved] = useState(false)
  const [applied, setApplied] = useState<number | null>(null)

  const request = (): BulkRequest => ({
    selection,
    action,
    value: action === 'set_met' ? met : action === 'set_do_not_contact' ? flag : null,
    reason: action === 'set_do_not_contact' && reason.trim() !== '' ? reason.trim() : null,
  })

  const count = useMutation({
    mutationFn: () => countBulk(request()),
    onSuccess: (data) => {
      setPending(data)
      setApplied(null)
    },
  })

  const apply = useMutation({
    mutationFn: async (token: string) => {
      try {
        return await applyBulk({ ...request(), token })
      } catch (error) {
        if (error instanceof ConfirmationStale) {
          // The selection moved. Count again and ask once more, rather than
          // reporting a failure for something nobody did wrong.
          const fresh = await countBulk(request())
          setPending(fresh)
          setMoved(true)
          return null
        }
        throw error
      }
    },
    onSuccess: (affected) => {
      if (affected !== null) {
        setApplied(affected)
        setPending(null)
        setMoved(false)
        onApplied()
      }
    },
  })

  return (
    <div data-slot="bulk-actions" className="space-y-2 rounded-lg border p-3">
      <div className="flex flex-wrap items-end gap-2">
        <div className="grid gap-1">
          <Label htmlFor="bulk-action">Bulk action</Label>
          <NativeSelect
            id="bulk-action"
            value={action}
            onChange={(event) => {
              setAction(event.target.value as BulkAction)
              setPending(null)
              setMoved(false)
            }}
          >
            {ACTIONS.map((candidate) => (
              <option key={candidate.value} value={candidate.value}>
                {candidate.label}
              </option>
            ))}
          </NativeSelect>
        </div>
        {action === 'set_met' && (
          <div className="grid gap-1">
            <Label htmlFor="bulk-met">Value</Label>
            <NativeSelect
              id="bulk-met"
              value={met}
              onChange={(event) => {
                setMet(event.target.value as ContactMet)
                setPending(null)
              }}
            >
              {MET_VALUES.map((value) => (
                <option key={value} value={value}>
                  {value.replace(/_/g, ' ')}
                </option>
              ))}
            </NativeSelect>
          </div>
        )}
        {action === 'set_do_not_contact' && (
          <>
            <div className="grid gap-1">
              <Label htmlFor="bulk-dnc">Value</Label>
              <NativeSelect
                id="bulk-dnc"
                value={flag ? 'true' : 'false'}
                onChange={(event) => {
                  setFlag(event.target.value === 'true')
                  setPending(null)
                }}
              >
                <option value="true">do not contact</option>
                <option value="false">ok to contact</option>
              </NativeSelect>
            </div>
            <div className="grid gap-1">
              <Label htmlFor="bulk-reason">Reason</Label>
              <Input
                id="bulk-reason"
                value={reason}
                onChange={(event) => {
                  setReason(event.target.value)
                  setPending(null)
                }}
              />
            </div>
          </>
        )}
        <Button variant="outline" onClick={() => count.mutate()} disabled={count.isPending}>
          Count first
        </Button>
      </div>

      {count.isError && <ErrorNote label="Could not count the selection" error={count.error} />}
      {apply.isError && <ErrorNote label="Could not apply the action" error={apply.error} />}

      {pending !== null && (
        <Callout
          tone={moved ? 'warning' : 'info'}
          title={moved ? 'The selection moved' : undefined}
        >
          <p>
            {moved
              ? 'Someone or something changed the selection before this ran, so nothing was applied. '
              : ''}
            This affects <strong>{pending.count.toLocaleString()}</strong> contacts —{' '}
            {pending.describe}.
          </p>
          <Button
            className="mt-2"
            onClick={() => apply.mutate(pending.token)}
            disabled={apply.isPending}
          >
            {moved ? 'Confirm the new count' : 'Apply'}
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
