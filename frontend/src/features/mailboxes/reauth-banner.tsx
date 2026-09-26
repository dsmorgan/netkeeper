import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import { useCallback } from 'react'

import { Button } from '@/components/ui/button'
import { useServerEvent } from '@/features/events/use-server-event'

import { mailboxKeys, mailboxStatusQuery, navigation, reasonText, startAuthorization } from './api'

/**
 * The app-wide banner for a mailbox that needs authorizing again (spec 11.5).
 *
 * Shown on every page while any mailbox is `reauth_required`: email steps are
 * paused until someone does. The backend's poll publishes `mailbox.status` the
 * moment it marks one, so the banner appears without a reload.
 */
export function ReauthBanner() {
  const queryClient = useQueryClient()
  const status = useQuery(mailboxStatusQuery)
  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: mailboxKeys.all })
  }, [queryClient])
  useServerEvent('mailbox.status', refresh)

  const reauthorize = useMutation({
    mutationFn: startAuthorization,
    onSuccess: (url) => navigation.assign(url),
  })

  const stuck =
    status.data?.reauth_required === true
      ? status.data.mailboxes.filter((mailbox) => mailbox.status === 'reauth_required')
      : []
  const first = stuck[0]
  if (first === undefined) return null

  return (
    <div
      role="alert"
      className="mb-4 flex flex-wrap items-start gap-3 rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-destructive"
    >
      <AlertTriangle className="mt-0.5 size-5 shrink-0" aria-hidden="true" />
      <div className="min-w-0 flex-1 space-y-1">
        <h2 className="font-heading text-sm font-semibold">Gmail needs you to sign in again</h2>
        {stuck.map((mailbox) => (
          <p key={mailbox.id} className="text-sm">
            <span className="font-medium">{mailbox.email}</span>:{' '}
            {reasonText(mailbox.status_reason) ?? 'Google no longer accepts the token.'}
          </p>
        ))}
        <p className="text-xs text-destructive/80">
          Email steps are paused until it is authorized again.
        </p>
        {reauthorize.isError && <p className="text-sm">{(reauthorize.error as Error).message}</p>}
      </div>
      <Button
        size="sm"
        onClick={() => reauthorize.mutate(first.id)}
        disabled={reauthorize.isPending}
      >
        {reauthorize.isPending ? 'Opening Google…' : 'Re-authorize'}
      </Button>
    </div>
  )
}
