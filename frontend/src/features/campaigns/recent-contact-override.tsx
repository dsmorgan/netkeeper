/**
 * Enroll contacts the recent-contact guard skipped, on purpose (#446).
 *
 * The Audience step lists the contacts that guard alone skips, each with when and how
 * someone last contacted them. The person picks some or all and confirms; the backend
 * then sets aside the recent-contact guard for exactly those contacts. Every other guard
 * (no LinkedIn member id, another campaign, opted out, yourself, merged ...) still
 * applies and is never overridable here, so a contact any other guard skips is not
 * offered.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { ErrorNote, LoadingNote } from '@/features/crm/controls'

import {
  campaignKeys,
  enroll,
  errorText,
  guardDetailsQuery,
  type Campaign,
  type EnrollOut,
  type SkippedContact,
} from './api'
import { lastContactText } from './format'

function daysText(days: number): string {
  return days === 1 ? '1 day' : `${days} days`
}

export function RecentContactOverride({
  campaign,
  onEnrolled,
}: {
  campaign: Campaign
  onEnrolled: (outcome: EnrollOut) => void
}) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [picked, setPicked] = useState<ReadonlySet<number>>(new Set())
  const [confirming, setConfirming] = useState(false)
  // The overrides the last call refused, with the names shown when it was made.
  const [refused, setRefused] = useState<{ id: number; name: string; reason: string }[]>([])
  const details = useQuery({ ...guardDetailsQuery(campaign.id), enabled: open })
  const windowText = daysText(campaign.contacted_within_days_guard)

  const skipped = details.data?.skipped ?? []
  const offered = skipped.filter((c) => c.overridable)
  // Only the recent-contact guard skips these, but the backend says why they can't be
  // overridden anyway (a last contact dated in the future).
  const withNote = skipped.filter((c) => !c.overridable && c.override_note)
  const alsoOther = skipped.filter(
    (c) =>
      !c.overridable && !c.override_note && (c.reason_codes ?? []).includes('contacted_recently'),
  ).length
  // Only what is still offered counts: a refetch may have dropped a contact.
  const chosen = offered.filter(
    (c) =>
      picked.has(c.contact_id) && c.last_contacted_at !== null && c.last_contacted_at !== undefined,
  )
  const allPicked = offered.length > 0 && chosen.length === offered.length

  const run = useMutation({
    // Each contact goes with the last contact shown for it: that is the override's cutoff,
    // and a contact someone reached since is refused rather than overridden.
    mutationFn: (contacts: SkippedContact[]) =>
      enroll(campaign.id, {
        override_recent_contact: contacts.map((c) => ({
          contact_id: c.contact_id,
          seen_last_contacted_at: c.last_contacted_at as string,
        })),
        confirm: true,
      }),
    onSuccess: async (answer, contacts) => {
      const names = new Map(
        contacts.map((c) => [c.contact_id, c.name || `Contact ${c.contact_id}`]),
      )
      setRefused(
        (answer.override_refused ?? []).map((r) => ({
          id: r.contact_id,
          name: names.get(r.contact_id) ?? `Contact ${r.contact_id}`,
          reason: r.reason,
        })),
      )
      setConfirming(false)
      setPicked(new Set())
      onEnrolled(answer)
      await queryClient.invalidateQueries({ queryKey: campaignKeys.all })
    },
  })

  function toggle(id: number, on: boolean) {
    setPicked((current) => {
      const next = new Set(current)
      if (on) next.add(id)
      else next.delete(id)
      return next
    })
  }

  return (
    <div className="flex flex-col gap-2">
      {refused.length > 0 && (
        <div role="status" className="flex flex-col gap-1">
          <p className="font-medium">
            {refused.length === 1 ? '1 contact was' : `${refused.length} contacts were`} not
            enrolled:
          </p>
          <ul aria-label="Not overridden" className="flex flex-col gap-1">
            {refused.map((r) => (
              <li key={r.id}>
                <span className="font-medium">{r.name}</span>
                <span className="text-muted-foreground">: {r.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <Button
        variant="outline"
        className="w-fit"
        aria-expanded={open}
        onClick={() => setOpen((shown) => !shown)}
      >
        {open
          ? 'Hide contacts skipped for recent contact'
          : 'Show contacts skipped for recent contact'}
      </Button>
      {open &&
        (details.isPending ? (
          <LoadingNote label="Loading the skipped contacts…" />
        ) : details.isError ? (
          <ErrorNote label="The skipped contacts are unavailable." error={details.error} />
        ) : (
          <section aria-label="Contacted recently" className="flex flex-col gap-2">
            {offered.length === 0 ? (
              <p className="text-muted-foreground">
                Nobody is skipped only for being contacted in the last {windowText}.
              </p>
            ) : (
              <>
                <p className="text-muted-foreground">
                  The recent-contact guard skips these contacts: someone contacted them in the last{' '}
                  {windowText}. You can enroll the ones you pick anyway. Every other guard still
                  applies to them.
                </p>
                <label className="flex w-fit items-center gap-2">
                  <Checkbox
                    checked={allPicked}
                    indeterminate={chosen.length > 0 && !allPicked}
                    onCheckedChange={(checked) =>
                      setPicked(
                        checked === true ? new Set(offered.map((c) => c.contact_id)) : new Set(),
                      )
                    }
                  />
                  Select all {offered.length}
                </label>
                <ul className="flex flex-col gap-1">
                  {offered.map((c) => {
                    const name = c.name || `Contact ${c.contact_id}`
                    return (
                      <li key={c.contact_id}>
                        <label className="flex items-center gap-2">
                          <Checkbox
                            checked={picked.has(c.contact_id)}
                            onCheckedChange={(checked) => toggle(c.contact_id, checked === true)}
                          />
                          <span className="font-medium">{name}</span>
                          <span className="text-muted-foreground">
                            last contacted {lastContactText(c)}
                          </span>
                        </label>
                      </li>
                    )
                  })}
                </ul>
                <Button
                  className="w-fit"
                  disabled={chosen.length === 0 || run.isPending}
                  onClick={() => setConfirming(true)}
                >
                  Enroll anyway{chosen.length > 0 ? ` (${chosen.length})` : ''}
                </Button>
              </>
            )}
            {withNote.length > 0 && (
              <ul aria-label="Not overridable" className="flex flex-col gap-1">
                {withNote.map((c) => (
                  <li key={c.contact_id} className="text-muted-foreground">
                    <span className="font-medium text-foreground">
                      {c.name || `Contact ${c.contact_id}`}
                    </span>
                    : {c.override_note}
                  </li>
                ))}
              </ul>
            )}
            {alsoOther > 0 && (
              <p className="text-muted-foreground">
                {alsoOther === 1 ? '1 more contact was' : `${alsoOther} more contacts were`}{' '}
                contacted recently, but another guard skips them too. Only the recent-contact guard
                can be overridden.
              </p>
            )}
            {details.data.skipped_total > skipped.length && (
              <p className="text-muted-foreground">
                Showing the first {skipped.length} of {details.data.skipped_total} skipped contacts.
              </p>
            )}
          </section>
        ))}
      <ConfirmDialog
        open={confirming}
        onOpenChange={(next) => {
          setConfirming(next)
          if (!next) run.reset()
        }}
        title={`Enroll ${chosen.length === 1 ? '1 contact' : `${chosen.length} contacts`} contacted recently?`}
        confirmLabel="Enroll anyway"
        confirmVariant="default"
        onConfirm={() => run.mutateAsync(chosen)}
        pending={run.isPending}
        error={run.isError ? errorText(run.error) : null}
      >
        <p>
          Someone contacted{' '}
          {chosen.length === 1 ? 'this contact' : `these ${chosen.length} contacts`} in the last{' '}
          {windowText}. Enrolling sets aside the recent-contact guard for{' '}
          {chosen.length === 1 ? 'it' : 'them'} only, and the enrollment records that you did.
        </p>
        <p>
          Every other guard still applies, now and when each step fires. Any contact with them dated
          after the last contact shown above counts as recent again.
        </p>
      </ConfirmDialog>
    </div>
  )
}
