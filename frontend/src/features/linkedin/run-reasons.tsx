import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useEffect, useRef } from 'react'

import { linkedinKeys, runDiagnosticsQuery } from './api'
import type { RunKind, RunVisitReason } from './types'

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'The run’s reasons are unavailable.'
}

function contactName(visit: RunVisitReason): string {
  const name = [visit.first_name, visit.last_name].filter(Boolean).join(' ')
  return name === '' ? `Contact ${visit.contact_id}` : name
}

/** Visits with their contacts and reasons: the unreadable ones, or the deferred ones. */
function VisitTable({ visits }: { visits: RunVisitReason[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-max text-left">
        <thead className="text-muted-foreground">
          <tr>
            <th scope="col" className="py-1 pr-3 font-medium">
              Visit
            </th>
            <th scope="col" className="py-1 pr-3 font-medium">
              Contact
            </th>
            <th scope="col" className="py-1 font-medium">
              Reason
            </th>
          </tr>
        </thead>
        <tbody>
          {visits.map((visit) => (
            <tr key={visit.visit} className="border-t border-border/60">
              <td className="py-1.5 pr-3 tabular-nums">{visit.visit}</td>
              <td className="py-1.5 pr-3">
                {visit.contact_exists ? (
                  <Link
                    to="/contacts/$contactId"
                    params={{ contactId: String(visit.contact_id) }}
                    className="underline underline-offset-4"
                  >
                    {contactName(visit)}
                  </Link>
                ) : (
                  <span className="text-muted-foreground">
                    Contact {visit.contact_id} (deleted)
                  </span>
                )}
              </td>
              <td className="py-1.5">
                {visit.reason_text}{' '}
                <code className="text-xs text-muted-foreground">{visit.reason}</code>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * Why a run's visits or answers could not be read (#405): each enrichment visit that
 * counted toward the unreadable limits, with its contact and reason, each visit that
 * saved the profile without its lost Contact info (listed apart since #424: it is not
 * unreadable), and each answer a connections sync lost. The reasons are fixed codes the server also gives in plain
 * words; the names are the contacts' own in netkeeper, never anything from the page.
 *
 * While the run is going, `marker` (its unreadable, mismatched and deferred counts) moves with
 * each live progress event, and a change refetches the list, so a new reason shows
 * up without a reload. A finished run's list is refetched with the runs list.
 */
export function RunReasons({
  runId,
  marker,
  stopReason = null,
  kind = null,
}: {
  runId: number
  marker: string
  stopReason?: string | null
  kind?: RunKind | null
}) {
  const queryClient = useQueryClient()
  const reasons = useQuery(runDiagnosticsQuery(runId))

  const seen = useRef(marker)
  useEffect(() => {
    if (seen.current === marker) return
    seen.current = marker
    void queryClient.invalidateQueries({ queryKey: linkedinKeys.runDiagnostics(runId) })
  }, [queryClient, runId, marker])

  if (reasons.isPending) return null
  if (reasons.isError) return <p role="alert">{message(reasons.error)}</p>

  const { unreadable_visits: visits, deferred_visits: deferred, lost_answers: lost } = reasons.data
  const stopped = reasons.data.stopped_by[0] ?? null
  // A route_changed stop always says why: by the visits below, by the one answer that
  // stopped it at once, or, for a run recorded before #405, that nothing was kept.
  const unexplained =
    kind === 'enrich' && stopReason === 'route_changed' && visits.length === 0 && stopped === null
  if (
    visits.length === 0 &&
    deferred.length === 0 &&
    lost.length === 0 &&
    stopped === null &&
    !unexplained
  )
    return null

  return (
    <div className="space-y-3">
      {stopped !== null && (
        <p className="rounded-lg bg-amber-500/10 px-3 py-2">
          Stopped at once by the page’s answer on visit {stopped.visit} (
          {stopped.contact_exists ? (
            <Link
              to="/contacts/$contactId"
              params={{ contactId: String(stopped.contact_id) }}
              className="underline underline-offset-4"
            >
              {contactName(stopped)}
            </Link>
          ) : (
            `contact ${stopped.contact_id}, deleted`
          )}
          ): {stopped.reason_text}{' '}
          <code className="text-xs text-muted-foreground">{stopped.reason}</code>
        </p>
      )}
      {unexplained && (
        <p className="text-muted-foreground">No per-visit reasons were recorded for this run.</p>
      )}
      {visits.length > 0 && (
        <section aria-labelledby={`run-${runId}-visits`}>
          <h3 id={`run-${runId}-visits`} className="text-xs font-medium text-muted-foreground">
            Unreadable visits
          </h3>
          <VisitTable visits={visits} />
        </section>
      )}
      {deferred.length > 0 && (
        <section aria-labelledby={`run-${runId}-deferred`}>
          <h3 id={`run-${runId}-deferred`} className="text-xs font-medium text-muted-foreground">
            Deferred Contact info
          </h3>
          <p className="text-xs text-muted-foreground">
            The profile was saved without its Contact info, which a later run reads. These visits
            count toward no unreadable limit.
          </p>
          <VisitTable visits={deferred} />
        </section>
      )}
      {lost.length > 0 && (
        <section aria-labelledby={`run-${runId}-lost`}>
          <h3 id={`run-${runId}-lost`} className="text-xs font-medium text-muted-foreground">
            Lost answers
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full min-w-max text-left">
              <thead className="text-muted-foreground">
                <tr>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Start
                  </th>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Cause
                  </th>
                  <th scope="col" className="py-1 font-medium">
                    Then
                  </th>
                </tr>
              </thead>
              <tbody>
                {lost.map((answer, index) => (
                  <tr key={`${answer.start}-${index}`} className="border-t border-border/60">
                    <td className="py-1.5 pr-3 tabular-nums">{answer.start}</td>
                    <td className="py-1.5 pr-3">{answer.cause}</td>
                    <td className="py-1.5 text-muted-foreground">{answer.ending ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  )
}
