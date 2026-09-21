import { Link } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

import { runsQuery } from './api'
import { formatWhen } from './fields'
import { ErrorNote, RunStatusBadge } from './notes'

const PAGE = 25

/** Every import, newest first, each one openable and undoable (spec 10.5 step 5). */
export function RunHistoryPage() {
  const [offset, setOffset] = useState(0)
  const runs = useQuery(runsQuery(PAGE, offset))

  if (runs.isPending) {
    return <p role="status">Loading the import history…</p>
  }
  if (runs.isError) {
    return (
      <ErrorNote>
        {runs.error instanceof Error ? runs.error.message : 'The import history is unavailable.'}
      </ErrorNote>
    )
  }

  const { items, total } = runs.data
  if (total === 0) {
    return (
      <Card className="max-w-2xl">
        <CardHeader>
          <CardTitle>No imports yet</CardTitle>
          <CardDescription>
            Every import is kept here with the rows it read, so one can be audited or undone later.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button render={<Link to="/imports" />}>Import a CSV</Button>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="flex max-w-5xl flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Import history</CardTitle>
          <CardDescription>
            {total} {total === 1 ? 'import' : 'imports'}. Open one to see its rows or undo it.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <table className="w-full text-left">
            <thead className="text-muted-foreground">
              <tr>
                <th scope="col" className="py-1 pr-3 font-medium">
                  File
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  When
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  Preset
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  Rows
                </th>
                <th scope="col" className="py-1 pr-3 font-medium">
                  Outcome
                </th>
                <th scope="col" className="py-1 font-medium">
                  Status
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((run) => (
                <tr key={run.id} className="border-t border-border/60">
                  <th scope="row" className="py-2 pr-3 font-normal">
                    <Link
                      to="/imports/runs/$runId"
                      params={{ runId: String(run.id) }}
                      className="underline underline-offset-4"
                    >
                      {run.filename}
                    </Link>
                  </th>
                  <td className="py-2 pr-3 text-muted-foreground">{formatWhen(run.created_at)}</td>
                  <td className="py-2 pr-3 text-muted-foreground">{run.preset ?? 'custom'}</td>
                  <td className="py-2 pr-3 tabular-nums">{run.total_rows}</td>
                  <td className="py-2 pr-3 text-muted-foreground">
                    {run.created_count} new, {run.matched_count} matched, {run.skipped_count}{' '}
                    skipped
                  </td>
                  <td className="py-2">
                    <RunStatusBadge status={run.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>

      {total > PAGE && (
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            disabled={offset === 0}
            onClick={() => setOffset((current) => Math.max(current - PAGE, 0))}
          >
            Newer
          </Button>
          <Button
            variant="outline"
            disabled={offset + PAGE >= total}
            onClick={() => setOffset((current) => current + PAGE)}
          >
            Older
          </Button>
          <span className="text-muted-foreground">
            {offset + 1}–{Math.min(offset + PAGE, total)} of {total}
          </span>
        </div>
      )}
    </div>
  )
}
