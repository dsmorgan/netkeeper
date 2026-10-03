import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'

import { clearSessionFlag, linkedinKeys } from './api'
import { formatWhen } from './fields'
import type { LinkedInStatus } from './types'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * The session flag, as a banner nothing else on the page can outshine (spec
 * 9.7). The two flags clear differently (`netkeeper/services/posture.py`'s
 * `_session_flag`, #168 review F1): a `logged_out` flag clears itself the
 * moment `netkeeper preflight` finds a live session again, so its advice is
 * "log in, then run preflight". `netkeeper` never clears a `checkpoint` flag
 * itself, since a live session cookie is not proof a checkpoint is resolved, so
 * that advice is to resolve it, then clear the flag by hand.
 *
 * "Clear flag", on a `checkpoint` flag only, is that by-hand clear, the same
 * as `netkeeper linkedin clear-flag` (#181), and like the command it always
 * asks first, naming the flag and when it was raised. A `logged_out` flag gets
 * no button: preflight clears it once it sees a live session, which is better
 * evidence than a click (the command still clears either, from a terminal). The request carries the flag shown here, and the
 * server clears only that exact flag: one raised again while the dialog was
 * open is refused, never cleared by an answer about the older one.
 */
export function SessionBanner({ status }: { status: LinkedInStatus }) {
  const queryClient = useQueryClient()
  const [asking, setAsking] = useState(false)

  const clear = useMutation({
    mutationFn: ({ outcome, flaggedAt }: { outcome: string; flaggedAt: string }) =>
      clearSessionFlag(outcome, flaggedAt),
    onSuccess: (data) => {
      queryClient.setQueryData(linkedinKeys.status(), data)
      void queryClient.invalidateQueries({ queryKey: linkedinKeys.browserHealth() })
      setAsking(false)
    },
    // A refusal means the flag is not what this banner shows any more; reread it.
    onError: () => void queryClient.invalidateQueries({ queryKey: linkedinKeys.status() }),
  })

  if (status.session_flag === null) return null

  const flag = status.session_flag
  const flaggedAt = status.session_flagged_at
  const checkpoint = flag === 'checkpoint'
  const when = flaggedAt === null ? '' : ` (raised ${formatWhen(flaggedAt)})`

  return (
    <div
      role="alert"
      className="flex items-start gap-3 rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-destructive"
    >
      <AlertTriangle className="mt-0.5 size-5 shrink-0" aria-hidden="true" />
      <div className="min-w-0 space-y-1">
        <h2 className="font-heading text-sm font-semibold">
          {checkpoint ? 'LinkedIn asked for a checkpoint' : 'Logged out of LinkedIn'}
        </h2>
        <p className="text-sm">
          {checkpoint ? (
            <>
              Open LinkedIn yourself in the netkeeper Chrome profile and resolve the checkpoint,
              then clear the flag here or with{' '}
              <code className="font-mono text-xs">netkeeper linkedin clear-flag</code>.{when}
            </>
          ) : (
            <>
              Log in to LinkedIn in the netkeeper Chrome profile, then run{' '}
              <code className="font-mono text-xs">netkeeper preflight</code>, which clears this
              automatically.{when}
            </>
          )}
        </p>
        <p className="text-xs text-destructive/80">
          No run will touch the browser again until this is cleared.
        </p>
        {checkpoint && flaggedAt !== null && (
          <Button
            variant="outline"
            size="sm"
            className="mt-1"
            onClick={() => {
              clear.reset()
              setAsking(true)
            }}
          >
            Clear flag…
          </Button>
        )}
      </div>

      {checkpoint && flaggedAt !== null && (
        <ConfirmDialog
          open={asking}
          onOpenChange={(open) => {
            if (!open) setAsking(false)
          }}
          title="Clear the checkpoint session flag?"
          confirmLabel="Clear flag"
          pending={clear.isPending}
          error={clear.isError ? message(clear.error) : null}
          onConfirm={() => clear.mutateAsync({ outcome: flag, flaggedAt })}
        >
          <p>
            This clears the checkpoint flag raised {formatWhen(flaggedAt)}. Runs can use this
            LinkedIn session again as soon as it is cleared, and scheduled runs fire when they are
            due if they are armed.
          </p>
          <p>
            netkeeper cannot tell whether the checkpoint is resolved. Clear it only after you have
            opened LinkedIn in the netkeeper Chrome profile yourself and the account looks healthy.
          </p>
          <p>If the flag changes while this is open, nothing is cleared.</p>
        </ConfirmDialog>
      )}
    </div>
  )
}
