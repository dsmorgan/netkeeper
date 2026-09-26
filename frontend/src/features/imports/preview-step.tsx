import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

import { droppedCells, fieldLabel, refusedChanges, rowLabel } from './fields'
import { ErrorNote, Note, OutcomeBadge } from './notes'
import { RunCounts } from './run-counts'
import type { ColumnMapping, ImportRun, PlannedChange, PreviewRow } from './types'

interface PreviewStepProps {
  run: ImportRun
  rows: PreviewRow[] | undefined
  mapping: ColumnMapping
  /** Rows these resolutions call candidates, whatever the draft recorded. */
  liveCandidates: number
  /** The draft's counts no longer match what the rows resolve to now. */
  stale: boolean
  pending: boolean
  error: string | null
  onRetry: () => void
  /** Null when the run was resumed from history and there is no file in hand. */
  onBack: (() => void) | null
  onContinue: () => void
}

function value(text: string | null): string {
  return text === null || text === '' ? '—' : text
}

/** One field a row would write, or one a more authoritative source keeps out. */
function Change({ change }: { change: PlannedChange }) {
  if (change.refused) {
    return (
      <li className="text-destructive">
        <span className="font-medium">{fieldLabel(change.field)}</span> refused: the{' '}
        {change.kept_source === 'manual' ? 'edit you made' : `${change.kept_source} value`} “
        {value(change.before)}” stays, and “{value(change.after)}” from the file is not written.
      </li>
    )
  }
  return (
    <li>
      <span className="font-medium">{fieldLabel(change.field)}</span>{' '}
      {change.before === null ? (
        <>set to “{value(change.after)}”</>
      ) : (
        <>
          “{value(change.before)}” → “{value(change.after)}”
        </>
      )}
    </li>
  )
}

/**
 * Step 3: the first rows resolved against the database as it is now.
 *
 * The counts cover the whole file; the table covers the rows the API resolved.
 * Refused fields get their own callout: a value a manual edit outranks is the
 * one thing a person cannot find out after committing (spec 10.5).
 */
export function PreviewStep({
  run,
  rows,
  mapping,
  liveCandidates,
  stale,
  pending,
  error,
  onRetry,
  onBack,
  onContinue,
}: PreviewStepProps) {
  // The larger of what the draft recorded and what these rows resolve to now,
  // so a stale draft never labels the button "Go to commit" over a candidate.
  const candidates = Math.max(run.candidate_count, liveCandidates)
  const refused = rows ? refusedChanges(rows) : []
  const refusedFields = [...new Set(refused.map(({ change }) => fieldLabel(change.field)))]
  const dropped = rows ? droppedCells(rows) : []

  return (
    <div className="flex max-w-4xl flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle level={2}>What this import would do</CardTitle>
          <CardDescription>
            {run.filename} · read with{' '}
            {run.preset === null ? 'a mapping of your own' : `the ${run.preset} preset`} · nothing
            is written until you commit
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <RunCounts run={run} tense="plan" />
          {stale && (
            <Note tone="warn">
              <p className="font-medium">This draft was read before your contacts changed.</p>
              <p>
                The counts above are what the file meant then. The rows below are what it means now,
                and a commit uses these: {liveCandidates}{' '}
                {liveCandidates === 1 ? 'row needs' : 'rows need'} a decision that the draft did not
                record.
              </p>
            </Note>
          )}
        </CardContent>
      </Card>

      {refused.length > 0 && (
        <Note tone="warn">
          <p className="font-medium">
            {refused.length} {refused.length === 1 ? 'value' : 'values'} in these rows will not be
            written.
          </p>
          <p>
            A manual edit outranks an import, and among automated sources a LinkedIn sync outranks
            an archive, which outranks a CSV. Affected fields: {refusedFields.join(', ')}. Revert
            the field on the contact first if you want the file&rsquo;s value.
          </p>
        </Note>
      )}

      {dropped.length > 0 && (
        <Note tone="warn">
          <p className="font-medium">
            {dropped.length} {dropped.length === 1 ? 'row has' : 'rows have'} a cell the importer
            cannot use.
          </p>
          <p>
            The rest of each row still lands. A LinkedIn URN that is not a{' '}
            <code className="font-mono">urn:li:…</code> and a date in no readable format are dropped
            rather than guessed at.
          </p>
          <ul className="list-disc pl-5">
            {[...new Set(dropped.map((row) => row.problem))].map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </Note>
      )}

      <Card>
        <CardHeader>
          <CardTitle level={2}>
            {rows === undefined ? 'Preview' : `The first ${rows.length} rows`}
          </CardTitle>
          <CardDescription>Resolved against your contacts as they are right now.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {pending && <p role="status">Resolving the first rows…</p>}
          {error && (
            <>
              <ErrorNote>{error}</ErrorNote>
              <Button variant="outline" onClick={onRetry}>
                Try again
              </Button>
            </>
          )}
          {rows !== undefined && rows.length === 0 && (
            <p className="text-muted-foreground">
              This file has no data rows below its header, so there is nothing to import.
            </p>
          )}
          {rows !== undefined && rows.length > 0 && (
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
                    Fields
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.row_number} className="border-t border-border/60 align-top">
                    <td className="py-2 pr-3 tabular-nums text-muted-foreground">
                      {row.row_number}
                    </td>
                    <th scope="row" className="py-2 pr-3 font-normal">
                      {rowLabel(row.raw, mapping)}
                    </th>
                    <td className="py-2 pr-3">
                      <OutcomeBadge resolution={row.resolution} />
                      <p className="mt-1 text-muted-foreground">
                        {row.resolution === 'matched' &&
                          `contact #${row.contact_id} by ${row.matched_by}`}
                        {row.resolution === 'candidate' &&
                          `could be ${row.candidate_ids.map((id) => `#${id}`).join(' or ')}`}
                        {row.resolution === 'created' && 'nobody here looks like this person'}
                        {row.resolution === 'skipped' && (row.problem ?? 'nothing to import')}
                      </p>
                    </td>
                    <td className="py-2">
                      {row.changes.length === 0 ? (
                        <span className="text-muted-foreground">no change</span>
                      ) : (
                        <ul className="space-y-0.5">
                          {row.changes.map((change) => (
                            <Change key={change.field} change={change} />
                          ))}
                        </ul>
                      )}
                      {row.problem !== null && row.resolution !== 'skipped' && (
                        <p className="mt-1 text-amber-700 dark:text-amber-300">
                          Dropped: {row.problem}
                        </p>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      <div className="flex items-center gap-2">
        {onBack && (
          <Button variant="outline" onClick={onBack}>
            Change the mapping
          </Button>
        )}
        <Button onClick={onContinue} disabled={pending || rows === undefined}>
          {candidates > 0
            ? `Review ${candidates} ${candidates === 1 ? 'candidate' : 'candidates'}`
            : 'Go to commit'}
        </Button>
      </div>
    </div>
  )
}
