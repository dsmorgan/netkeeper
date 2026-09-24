import { useQuery } from '@tanstack/react-query'

import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { renderInlineMarkdown } from '@/lib/inline-markdown'
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
            {/* Below `sm` a table has no room for a Detail column at all: a fixed
                width just wraps prose one character per line rather than making
                it any narrower (review179r2 measured a 31,093px-tall page from
                exactly that). Below `sm` every protection is its own stacked
                block instead, each with the full page width to wrap in; from
                `sm` up there is room for a table, so this renders one markup
                and hides half of it with `sm:` rather than measuring width in
                script — the same approach `run-detail.tsx`'s FieldList uses. */}
            <ul className="space-y-2 sm:hidden" data-testid="posture-blocks">
              {posture.data.protections.map((row) => (
                <ProtectionBlock key={row.name} row={row} />
              ))}
            </ul>
            <table
              className="hidden w-full table-fixed text-left sm:table"
              data-testid="posture-table"
            >
              <colgroup>
                <col className="w-40" />
                <col className="w-20" />
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
                    <li key={gap}>{renderInlineMarkdown(gap)}</li>
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

function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={cn(
        'inline-flex h-5 shrink-0 items-center rounded-4xl px-2 text-xs font-medium',
        STATUS_CLASSES[status] ?? 'bg-muted text-muted-foreground',
      )}
    >
      {status}
    </span>
  )
}

function ProtectionDetail({ row }: { row: Protection }) {
  return (
    <>
      <p>{renderInlineMarkdown(row.value)}</p>
      {row.warnings.length > 0 && (
        <ul className="mt-1 space-y-1 text-destructive">
          {row.warnings.map((warning) => (
            <li key={warning}>{renderInlineMarkdown(warning)}</li>
          ))}
        </ul>
      )}
    </>
  )
}

function ProtectionRow({ row }: { row: Protection }) {
  return (
    <tr className="border-t border-border/60 align-top">
      <th scope="row" className="py-2 pr-3 font-normal break-words">
        {row.name}
      </th>
      <td className="py-2 pr-3">
        <StatusBadge status={row.status} />
      </td>
      <td className="py-2 break-words">
        <ProtectionDetail row={row} />
      </td>
    </tr>
  )
}

function ProtectionBlock({ row }: { row: Protection }) {
  // `p-2`, not the card's usual `p-3`: this app's sidebar nav does not
  // collapse below `sm` (out of scope here), so the content column left for
  // a card at 390px is already only ~166px — every point of padding this
  // block keeps for itself is a point the prose below cannot wrap in.
  return (
    <li className="rounded-lg border border-border/60 p-2">
      <div className="flex items-center justify-between gap-2">
        {/* `min-w-0`: a flex item's default `min-width: auto` refuses to
            shrink below its content's own min-content width (the longest
            unbreakable word), so without it a long name pushes the badge
            past the card's own right edge instead of wrapping — measured in
            a real browser at 390px (review179r2), where the effective
            content column is much narrower than the viewport (the sidebar
            nav does not collapse below `sm`). */}
        <span className="min-w-0 break-words font-medium">{row.name}</span>
        <StatusBadge status={row.status} />
      </div>
      <div className="mt-2 break-words text-muted-foreground">
        <ProtectionDetail row={row} />
      </div>
    </li>
  )
}
