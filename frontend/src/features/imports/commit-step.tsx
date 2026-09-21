import { Link } from '@tanstack/react-router'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

import { ErrorNote, Note } from './notes'
import { RunCounts } from './run-counts'
import type { Decision, ImportRun } from './types'

interface CommitStepProps {
  run: ImportRun
  decisions: Record<number, Decision>
  undecided: number
  skipUndecided: boolean
  onSkipChange: (skip: boolean) => void
  onCommit: () => void
  pending: boolean
  error: string | null
  onBack: () => void
}

/**
 * Step 5: apply the run, in one transaction.
 *
 * The button is unavailable while a candidate has no decision. That is the same
 * rule the API enforces with a 409; showing it as a state means nobody meets it
 * as an error after pressing commit.
 */
export function CommitStep({
  run,
  decisions,
  undecided,
  skipUndecided,
  onSkipChange,
  onCommit,
  pending,
  error,
  onBack,
}: CommitStepProps) {
  const chosen = Object.values(decisions)
  const merges = chosen.filter((decision) => decision.kind === 'merge_into').length
  const creates = chosen.filter((decision) => decision.kind === 'create_new').length
  const blocked = undecided > 0 && !skipUndecided

  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Commit the import</CardTitle>
          <CardDescription>
            {run.filename} · every row lands in one transaction, or none of them does.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <RunCounts run={run} tense="plan" />
          {run.candidate_count > 0 && (
            <p className="text-muted-foreground">
              Of {run.candidate_count} candidates, {merges} merge into an existing contact and{' '}
              {creates} become new contacts.
            </p>
          )}
          <p className="text-muted-foreground">
            A value the file carries is written only where no stronger source holds the field: your
            own edits always win, and this run can be undone from its history page afterwards.
          </p>
        </CardContent>
      </Card>

      {undecided > 0 && (
        <Note tone="warn">
          <p className="font-medium">
            {undecided} {undecided === 1 ? 'candidate has' : 'candidates have'} no decision.
          </p>
          <p>
            Go back and decide each one, or skip them: a skipped candidate is left out of the import
            entirely and its row is recorded as skipped.
          </p>
          <label className="flex items-center gap-2 text-foreground">
            <input
              type="checkbox"
              checked={skipUndecided}
              onChange={(event) => onSkipChange(event.target.checked)}
            />
            Skip the {undecided} undecided {undecided === 1 ? 'candidate' : 'candidates'}
          </label>
        </Note>
      )}

      {error && <ErrorNote>{error}</ErrorNote>}

      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" onClick={onBack} disabled={pending}>
          Back to the candidates
        </Button>
        <Button onClick={onCommit} disabled={pending || blocked}>
          {pending ? 'Committing…' : `Commit ${run.total_rows} rows`}
        </Button>
        {blocked && (
          <span className="text-muted-foreground">
            Decide every candidate first, or tick the skip box above.
          </span>
        )}
      </div>
    </div>
  )
}

/** What the commit did, and where to go next. */
export function CommitResult({ run, onRestart }: { run: ImportRun; onRestart: () => void }) {
  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Imported {run.filename}</CardTitle>
          <CardDescription>
            {run.created_count} new {run.created_count === 1 ? 'contact' : 'contacts'},{' '}
            {run.matched_count} updated. Undo it from this run&rsquo;s page if it was not what you
            wanted.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <RunCounts run={run} tense="done" />
        </CardContent>
      </Card>
      <div className="flex flex-wrap gap-2">
        <Button render={<Link to="/contacts" />}>See your contacts</Button>
        <Button
          variant="outline"
          render={<Link to="/imports/runs/$runId" params={{ runId: String(run.id) }} />}
        >
          Open this run
        </Button>
        <Button variant="outline" onClick={onRestart}>
          Import another file
        </Button>
      </div>
    </div>
  )
}
