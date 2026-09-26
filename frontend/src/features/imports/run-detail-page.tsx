import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'

import { ApiError, importKeys, rollbackRun, rowsQuery, runQuery } from './api'
import { InvitationsCard, MessagesCard } from './archive-flow'
import { RESOLUTION_LABELS, fieldLabel, formatWhen, rowLabel } from './fields'
import { ErrorNote, Note, OutcomeBadge, RunStatusBadge } from './notes'
import { RunCounts } from './run-counts'
import { SELECT_CLASS } from './styles'
import type { ColumnMapping, ImportRun, Resolution, RollbackResult } from './types'

const ROWS_PAGE = 50
const RESOLUTIONS: readonly Resolution[] = ['matched', 'created', 'candidate', 'skipped']

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** A 409: the run's own state, or a merge that has drawn in what it created. */
function isConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409
}

/** One import run: what it read, what it did, and how to undo it. */
export function RunDetailPage({ runId }: { runId: number }) {
  const run = useQuery(runQuery(runId))

  if (run.isPending) {
    return <p role="status">Loading the import…</p>
  }
  if (run.isError) {
    return <ErrorNote>{message(run.error)}</ErrorNote>
  }
  return <RunDetail run={run.data} />
}

/** What `/imports/runs/<not a number>` shows instead of a backend validation error. */
export function NoSuchRun({ runId }: { runId: string }) {
  return (
    <div className="flex max-w-5xl flex-col gap-4">
      <ErrorNote>“{runId}” is not an import run. Import runs are numbered.</ErrorNote>
      <p>
        <Link to="/imports/runs" className="underline underline-offset-2">
          See every import
        </Link>
      </p>
    </div>
  )
}

function RunDetail({ run }: { run: ImportRun }) {
  const mapping = run.mapping as ColumnMapping
  // The result outlives the card that produced it: rolling back flips the run
  // to `rolled_back`, and what it removed is the one thing worth reading after.
  const [undone, setUndone] = useState<RollbackResult | null>(null)
  return (
    <div className="flex max-w-5xl flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {run.filename}
            <RunStatusBadge status={run.status} />
          </CardTitle>
          <CardDescription>
            Read {formatWhen(run.created_at)}{' '}
            {run.source_kind === 'archive'
              ? 'from a LinkedIn data archive'
              : run.preset === null
                ? 'with a mapping of its own'
                : `with the ${run.preset} preset`}
            {run.committed_at !== null && ` · committed ${formatWhen(run.committed_at)}`}
            {run.rolled_back_at !== null && ` · rolled back ${formatWhen(run.rolled_back_at)}`}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <RunCounts run={run} tense={run.status === 'draft' ? 'plan' : 'done'} />
        </CardContent>
      </Card>

      {run.archive != null && (
        <>
          <MessagesCard
            counts={run.archive.messages}
            unfamiliarFiles={run.archive.unfamiliar_message_files}
          />
          <InvitationsCard counts={run.archive.invitations} />
        </>
      )}

      {run.status === 'draft' && (
        <Card>
          <CardHeader>
            <CardTitle>Never committed</CardTitle>
            <CardDescription>
              This file was read and resolved, but nothing was written. The rows are still here, so
              it can be finished without choosing the file again.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button render={<Link to="/imports" search={{ run: run.id }} />}>
              Finish this import
            </Button>
          </CardContent>
        </Card>
      )}

      {undone !== null && <RollbackSummary result={undone} />}

      {run.status === 'committed' && undone === null && (
        <RollbackCard run={run} onDone={setUndone} />
      )}

      {run.status === 'rolled_back' && undone === null && (
        <Note>
          <p>
            This import was undone on {formatWhen(run.rolled_back_at)}. The rows below are what it
            did before that; a run can only be rolled back once.
          </p>
        </Note>
      )}

      <RowsCard run={run} mapping={mapping} />
    </div>
  )
}

/**
 * Undoing one run, with the consequence spelled out first.
 *
 * A rollback is not a general undo: it deletes the contacts this run created,
 * puts back the fields it changed along with their provenance, and leaves every
 * later edit alone. It is refused outright when a merge has since drawn in one
 * of the contacts it created. Saying all that before the click is the whole
 * point of the dialog.
 */
function RollbackSummary({ result }: { result: RollbackResult }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Rolled back</CardTitle>
      </CardHeader>
      <CardContent>
        <p role="status">
          Deleted {result.contacts_deleted} {result.contacts_deleted === 1 ? 'contact' : 'contacts'}{' '}
          this import created, put {result.fields_restored}{' '}
          {result.fields_restored === 1 ? 'field' : 'fields'} back on {result.contacts_restored}{' '}
          {result.contacts_restored === 1 ? 'contact' : 'contacts'} it had changed, and removed{' '}
          {result.children_deleted} related {result.children_deleted === 1 ? 'record' : 'records'}{' '}
          such as emails, phones and positions.
        </p>
      </CardContent>
    </Card>
  )
}

function RollbackCard({ run, onDone }: { run: ImportRun; onDone: (r: RollbackResult) => void }) {
  const [asking, setAsking] = useState(false)
  const queryClient = useQueryClient()

  const rollback = useMutation({
    mutationFn: (force: boolean) => rollbackRun(run.id, force),
    onSuccess: (undone) => {
      onDone(undone)
      setAsking(false)
      void queryClient.invalidateQueries({ queryKey: importKeys.all })
    },
    onError: (failure) => {
      // A 409 will not come right by pressing the button again: something about
      // the data has to change first. Close the dialog and explain on the page.
      if (isConflict(failure)) setAsking(false)
    },
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Undo this import</CardTitle>
        <CardDescription>Reverses what this run did, and only what this run did.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
          <li>
            The {run.created_count} {run.created_count === 1 ? 'contact' : 'contacts'} this import
            created {run.created_count === 1 ? 'is' : 'are'} deleted, along with the emails, phones,
            links and positions that came with {run.created_count === 1 ? 'it' : 'them'}.
          </li>
          <li>
            Contacts it only updated are kept, with the fields it wrote put back to the values they
            held before {formatWhen(run.committed_at)} — and with the record of where those values
            came from, so your own edits stay yours.
          </li>
          <li>
            Anything changed after the import — by you, by a later import, or by a sync — keeps the
            newer value. This is not a general undo, and it cannot itself be undone.
          </li>
          <li>
            If a merge has since drawn in one of the contacts this import created, the rollback is
            refused whole rather than half done: deleting that contact would take rows the import
            never created.
          </li>
          <li>
            If a later import wrote over the same fields, undo that one first. If a contact this
            import created has since gained notes, tags, lists or interactions, you are told what
            would go with it before anything is deleted.
          </li>
        </ul>
        {rollback.isError && !asking && (
          <RollbackRefusal
            error={rollback.error}
            pending={rollback.isPending}
            onForce={() => rollback.mutate(true)}
          />
        )}
        <Button variant="destructive" onClick={() => setAsking(true)}>
          Roll back this import
        </Button>
      </CardContent>

      <ConfirmDialog
        open={asking}
        onOpenChange={setAsking}
        title={`Roll back ${run.filename}?`}
        confirmLabel={
          run.created_count === 1
            ? 'Delete 1 contact and restore the rest'
            : `Delete ${run.created_count} contacts and restore the rest`
        }
        pending={rollback.isPending}
        error={rollback.isError ? message(rollback.error) : null}
        onConfirm={() => rollback.mutate(false)}
      >
        <p>
          This deletes the {run.created_count} {run.created_count === 1 ? 'contact' : 'contacts'}{' '}
          the import created and puts back the fields it changed on the {run.matched_count} it
          updated, provenance and all.
        </p>
        <p>
          Edits made since the import are left as they are. A rollback cannot be undone, and it is
          refused outright if a merge has drawn in one of the contacts this import created.
        </p>
      </ConfirmDialog>
    </Card>
  )
}

/**
 * Why the backend refused a rollback, keyed off its `code` (#78), and the way on.
 *
 * `superseded` and `merged` need something else undone first; only
 * `created_contacts_changed` can be overridden here, and the button says what
 * that costs. A 409 without a code is the older, merge-only refusal.
 */
function RollbackRefusal({
  error,
  pending,
  onForce,
}: {
  error: Error
  pending: boolean
  onForce: () => void
}) {
  if (!isConflict(error)) return <ErrorNote>{message(error)}</ErrorNote>
  const code = error instanceof ApiError ? error.code : null
  if (code === 'superseded') {
    return (
      <Note tone="warn">
        <p className="font-medium">A later import has to be undone first.</p>
        <p>{message(error)}</p>
        <p>
          Nothing was changed. Roll back the later imports named above, newest first, then come back
          here.
        </p>
      </Note>
    )
  }
  if (code === 'created_contacts_changed') {
    return (
      <Note tone="warn">
        <p className="font-medium">Rolling back would delete more than this import added.</p>
        <p>{message(error)}</p>
        <p>Nothing was changed yet.</p>
        <Button variant="destructive" disabled={pending} onClick={onForce}>
          Delete them anyway and roll back
        </Button>
      </Note>
    )
  }
  return (
    <Note tone="warn">
      <p className="font-medium">This import cannot be undone as it stands.</p>
      <p>{message(error)}</p>
      <p>Nothing was changed. Undo the merge on the contacts named above, then come back here.</p>
    </Note>
  )
}

function RowsCard({ run, mapping }: { run: ImportRun; mapping: ColumnMapping }) {
  const [resolution, setResolution] = useState<Resolution | null>(null)
  const [offset, setOffset] = useState(0)
  const rows = useQuery(rowsQuery(run.id, { resolution, limit: ROWS_PAGE, offset }))

  return (
    <Card>
      <CardHeader>
        <CardTitle>Rows</CardTitle>
        <CardDescription>
          Every row as it was read, with what happened to it. Cells from columns the mapping left
          out are kept here and nowhere else.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor="row-filter" className="font-medium">
            Show
          </label>
          <select
            id="row-filter"
            className={SELECT_CLASS}
            value={resolution ?? ''}
            onChange={(event) => {
              setResolution((event.target.value || null) as Resolution | null)
              setOffset(0)
            }}
          >
            <option value="">Every row</option>
            {RESOLUTIONS.map((value) => (
              <option key={value} value={value}>
                {RESOLUTION_LABELS[value]}
              </option>
            ))}
          </select>
        </div>

        {rows.isPending && <p role="status">Loading the rows…</p>}
        {rows.isError && <ErrorNote>{message(rows.error)}</ErrorNote>}
        {rows.isSuccess && rows.data.items.length === 0 && (
          <p className="text-muted-foreground">No rows match that filter.</p>
        )}
        {rows.isSuccess && rows.data.items.length > 0 && (
          <>
            <table className="w-full text-left">
              <thead className="text-muted-foreground">
                <tr>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Row
                  </th>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Who
                  </th>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Outcome
                  </th>
                  <th scope="col" className="py-1 font-medium">
                    Notes
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.data.items.map((row) => (
                  <tr key={row.id} className="border-t border-border/60 align-top">
                    <td className="py-2 pr-3 tabular-nums text-muted-foreground">
                      {row.row_number}
                    </td>
                    <th scope="row" className="py-2 pr-3 font-normal">
                      {rowLabel(row.raw, mapping)}
                    </th>
                    <td className="py-2 pr-3">
                      <OutcomeBadge resolution={row.resolution} />
                      {row.contact_id !== null && (
                        <p className="mt-1 text-muted-foreground">contact #{row.contact_id}</p>
                      )}
                    </td>
                    <td className="py-2">
                      {row.error !== null &&
                        (row.resolution === 'skipped' ? (
                          <p className="text-destructive">{row.error}</p>
                        ) : (
                          // The row landed and only a cell was dropped: the preview's
                          // amber, not the red of a row that failed (#94).
                          <p className="text-amber-700 dark:text-amber-300">Dropped: {row.error}</p>
                        ))}
                      {row.refused.length > 0 && (
                        <ul className="space-y-0.5 text-destructive">
                          {row.refused.map((refused) => (
                            <li key={refused.field}>
                              {fieldLabel(refused.field)} refused: kept “{refused.kept ?? '—'}” from{' '}
                              {refused.source}, not “{refused.incoming ?? '—'}” from the file.
                            </li>
                          ))}
                        </ul>
                      )}
                      {row.error === null && row.refused.length === 0 && (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {rows.data.total > ROWS_PAGE && (
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  disabled={offset === 0}
                  onClick={() => setOffset((current) => Math.max(current - ROWS_PAGE, 0))}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  disabled={offset + ROWS_PAGE >= rows.data.total}
                  onClick={() => setOffset((current) => current + ROWS_PAGE)}
                >
                  Next
                </Button>
                <span className="text-muted-foreground">
                  {offset + 1}–{Math.min(offset + ROWS_PAGE, rows.data.total)} of {rows.data.total}
                </span>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
