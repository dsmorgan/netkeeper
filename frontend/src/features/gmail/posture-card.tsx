import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { postureQuery } from '@/features/settings/api'
import { ProtectionDetail, StatusBadge } from '@/features/settings/posture-section'

import { GMAIL_POSTURE_ROWS } from './api'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * The posture rows that concern Gmail (sending hours, the reply poll, the next send,
 * template problems), from the same `GET /posture` report Settings shows in full. The
 * rest of that report is about LinkedIn and stays there, as it did before this page.
 */
export function PostureCard() {
  const posture = useQuery(postureQuery)
  const rows =
    posture.data?.protections.filter((row) => GMAIL_POSTURE_ROWS.includes(row.name)) ?? []

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Posture</CardTitle>
        <CardDescription>
          What protects your sending right now, read-only.{' '}
          <Link to="/settings" className="underline underline-offset-4">
            Every row is on Settings.
          </Link>
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {posture.isPending && <p role="status">Loading…</p>}
        {posture.isError && <p role="alert">{message(posture.error)}</p>}
        {posture.isSuccess && rows.length === 0 && (
          <p className="text-muted-foreground">The report has no Gmail rows.</p>
        )}
        {rows.length > 0 && (
          <ul className="space-y-2" aria-label="Gmail posture">
            {rows.map((row) => (
              <li key={row.name} className="rounded-lg border border-border/60 p-2">
                <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
                  <span className="font-medium">{row.name}</span>
                  <StatusBadge status={row.status} />
                </div>
                <div className="mt-1 break-words text-muted-foreground">
                  <ProtectionDetail row={row} expanded={false} />
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}
