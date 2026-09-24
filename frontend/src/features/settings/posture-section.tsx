import { useQuery } from '@tanstack/react-query'

import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { cn } from '@/lib/utils'

import { postureQuery, type Protection } from './api'

const STATUS_CLASSES: Record<string, string> = {
  on: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  off: 'bg-destructive/10 text-destructive',
  unknown: 'bg-amber-500/15 text-amber-800 dark:text-amber-300',
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Every protection the LinkedIn extractor has, right now (spec section 9,
 * P2-11) — the same report `netkeeper posture` prints from a terminal, read
 * here instead of typed. Read-only and built with no browser probe, so the
 * `linkedin session` row always reads "unknown": a live check stays
 * `netkeeper preflight`. The verdict line is shown exactly as the backend
 * sends it — "nothing is misconfigured" on a clean report, never "you are
 * safe" — because this reads configuration and counters, not whether the
 * code that would enforce them actually runs (`netkeeper posture`'s own
 * wording, spec 9, `netkeeper/services/posture.py`).
 */
export function PostureSection() {
  const posture = useQuery(postureQuery)

  return (
    <Card size="sm">
      <CardHeader>
        <h2 className="font-heading text-sm leading-snug font-medium">Posture</h2>
        <CardDescription>Every protection running right now, read-only.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {posture.isPending && <p role="status">Loading…</p>}
        {posture.isError && <p role="alert">{message(posture.error)}</p>}
        {posture.isSuccess && (
          <>
            {/* Not min-w-max like the Runs/Budget tables: those hold short numeric
                cells that read badly wrapped, so they scroll horizontally within
                their own card instead. Detail here holds full sentences (a
                protection's warning), which want to wrap, not scroll — a
                paragraph of prose you have to scroll sideways to read is worse
                than one that just wraps. table-fixed + a Detail column width
                keeps the name/state columns from being squeezed by a long one. */}
            <table className="w-full table-fixed text-left">
              <colgroup>
                <col className="w-20 sm:w-40" />
                <col className="w-16 sm:w-20" />
                <col />
              </colgroup>
              <thead className="text-muted-foreground">
                <tr>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Protection
                  </th>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    State
                  </th>
                  <th scope="col" className="py-1 font-medium">
                    Detail
                  </th>
                </tr>
              </thead>
              <tbody>
                {posture.data.protections.map((row) => (
                  <ProtectionRow key={row.name} row={row} />
                ))}
              </tbody>
            </table>

            {posture.data.gaps.length > 0 && (
              <div>
                <h3 className="text-xs font-medium text-muted-foreground">
                  Not covered by this report
                </h3>
                <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
                  {posture.data.gaps.map((gap) => (
                    <li key={gap}>{gap}</li>
                  ))}
                </ul>
              </div>
            )}

            <p role="status" className="font-medium">
              {posture.data.verdict}
            </p>
          </>
        )}
      </CardContent>
    </Card>
  )
}

function ProtectionRow({ row }: { row: Protection }) {
  return (
    <tr className="border-t border-border/60 align-top">
      <th scope="row" className="py-2 pr-3 font-normal break-words">
        {row.name}
      </th>
      <td className="py-2 pr-3">
        <span
          className={cn(
            'inline-flex h-5 shrink-0 items-center rounded-4xl px-2 text-xs font-medium',
            STATUS_CLASSES[row.status] ?? 'bg-muted text-muted-foreground',
          )}
        >
          {row.status}
        </span>
      </td>
      <td className="py-2 break-words">
        <p>{row.value}</p>
        {row.warnings.length > 0 && (
          <ul className="mt-1 space-y-1 text-destructive">
            {row.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        )}
      </td>
    </tr>
  )
}
