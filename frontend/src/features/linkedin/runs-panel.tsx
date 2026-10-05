import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { SELECT_CLASS } from '@/features/imports/styles'
import { cn } from '@/lib/utils'

import { runsQuery } from './api'
import { formatWhen, RUN_STATUS_CLASSES, stopReasonLabel, summarizeFields } from './fields'
import {
  RUN_KINDS,
  RUN_KIND_LABELS,
  RUN_STATUS_LABELS,
  type RunKind,
  type RunStatus,
} from './types'

const PAGE = 20
const STATUSES: readonly RunStatus[] = ['running', 'completed', 'aborted', 'failed']

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Runs are unavailable.'
}

/**
 * Every run, newest first, filterable by kind and status; a row opens its detail
 * (spec 14.1). The Run column is the id `netkeeper linkedin run <id>` takes (#405).
 * The whole row is the click target; the kind, the row's header, is also a button,
 * for the keyboard.
 */
export function RunsPanel({
  selectedRunId,
  onSelect,
}: {
  selectedRunId: number | null
  onSelect: (runId: number) => void
}) {
  const [kind, setKind] = useState<RunKind | ''>('')
  const [status, setStatus] = useState<RunStatus | ''>('')
  const [offset, setOffset] = useState(0)

  const runs = useQuery(
    runsQuery({
      kind: kind === '' ? undefined : kind,
      status: status === '' ? undefined : status,
      limit: PAGE,
      offset,
    }),
  )

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle level={2}>Runs</CardTitle>
        <CardDescription>Every sync and enrichment, newest first.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor="run-kind-filter">
            Filter by kind
          </label>
          <select
            id="run-kind-filter"
            className={cn(SELECT_CLASS, 'w-full sm:w-auto')}
            value={kind}
            onChange={(event) => {
              setKind(event.target.value as RunKind | '')
              setOffset(0)
            }}
          >
            <option value="">Every kind</option>
            {RUN_KINDS.map((value) => (
              <option key={value} value={value}>
                {RUN_KIND_LABELS[value]}
              </option>
            ))}
          </select>
          <label className="sr-only" htmlFor="run-status-filter">
            Filter by status
          </label>
          <select
            id="run-status-filter"
            className={cn(SELECT_CLASS, 'w-full sm:w-auto')}
            value={status}
            onChange={(event) => {
              setStatus(event.target.value as RunStatus | '')
              setOffset(0)
            }}
          >
            <option value="">Every status</option>
            {STATUSES.map((value) => (
              <option key={value} value={value}>
                {RUN_STATUS_LABELS[value]}
              </option>
            ))}
          </select>
        </div>

        {runs.isPending && <p role="status">Loading the runs…</p>}
        {runs.isError && <p role="alert">{message(runs.error)}</p>}
        {runs.isSuccess && runs.data.items.length === 0 && (
          <p className="text-muted-foreground">No runs yet.</p>
        )}
        {runs.isSuccess && runs.data.items.length > 0 && (
          // Scrolls within the card at a narrow width instead of pushing the whole
          // page wider than the screen (only a real browser lays this out; jsdom does not).
          <div className="overflow-x-auto">
            <table className="w-full min-w-max text-left">
              <thead className="text-muted-foreground">
                <tr>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Kind
                  </th>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Run
                  </th>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Status
                  </th>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Started / finished
                  </th>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Stop reason
                  </th>
                  <th scope="col" className="py-1 font-medium">
                    Counts
                  </th>
                </tr>
              </thead>
              <tbody>
                {runs.data.items.map((item) => (
                  <tr
                    key={item.id}
                    className={cn(
                      'cursor-pointer border-t border-border/60 hover:bg-muted/40',
                      item.id === selectedRunId && 'bg-muted/60',
                    )}
                    onClick={() => onSelect(item.id)}
                  >
                    <th scope="row" className="py-2 pr-3 font-normal">
                      <button
                        type="button"
                        className="underline underline-offset-4"
                        aria-current={item.id === selectedRunId ? 'true' : undefined}
                        onClick={(event) => {
                          event.stopPropagation()
                          onSelect(item.id)
                        }}
                      >
                        {RUN_KIND_LABELS[item.kind]}
                      </button>
                    </th>
                    <td className="py-2 pr-3 text-muted-foreground tabular-nums">{item.id}</td>
                    <td className="py-2 pr-3">
                      <span
                        className={cn(
                          'inline-flex h-5 shrink-0 items-center rounded-4xl px-2 text-xs font-medium',
                          RUN_STATUS_CLASSES[item.status],
                        )}
                      >
                        {RUN_STATUS_LABELS[item.status]}
                      </span>
                    </td>
                    <td className="py-2 pr-3 text-muted-foreground">
                      {formatWhen(item.started_at)}
                      {item.completed_at !== null && ` → ${formatWhen(item.completed_at)}`}
                    </td>
                    <td className="py-2 pr-3 text-muted-foreground">
                      {stopReasonLabel(item) ?? '—'}
                    </td>
                    <td className="py-2 text-muted-foreground">{summarizeFields(item.counts)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {runs.isSuccess && runs.data.total > PAGE && (
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={offset === 0}
              onClick={() => setOffset((current) => Math.max(current - PAGE, 0))}
            >
              Newer
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={offset + PAGE >= runs.data.total}
              onClick={() => setOffset((current) => current + PAGE)}
            >
              Older
            </Button>
            <span className="text-muted-foreground">
              {offset + 1}–{Math.min(offset + PAGE, runs.data.total)} of {runs.data.total}
            </span>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
