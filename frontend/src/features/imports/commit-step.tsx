import { Link } from '@tanstack/react-router'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

import { DuplicateGroupsNote, ErrorNote, Note } from './notes'
import { RunCounts } from './run-counts'
import type { Decision, ImportRun } from './types'

interface CommitStepProps {
  run: ImportRun
  decisions: Record<number, Decision>
  /** Live where it can be: the draft's count is what the file meant when it was read. */
  candidateTotal: number
  undecided: number
  /** The API refused the last commit because a row it re-resolved has no decision. */
  refused: boolean
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
  candidateTotal,
  undecided,
  refused,
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
  // `refused` is the API having re-resolved a row this screen could not see. It
  // blocks like an undecided candidate and, more to the point, it puts the skip
  // box on screen — without it a refused commit has no move left at all.
  const blocked = (undecided > 0 || refused) && !skipUndecided

  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle level={2}>Commit the import</CardTitle>
          <CardDescription>
            {run.filename} · every row lands in one transaction, or none of them does.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <RunCounts run={run} tense="plan" />
          {candidateTotal > 0 && (
            <p className="text-muted-foreground">
              Of {candidateTotal} {candidateTotal === 1 ? 'candidate' : 'candidates'}, {merges}{' '}
              merge into an existing contact and {creates} become new contacts.
            </p>
          )}
          <p className="text-muted-foreground">
            A value the file carries is written only where no stronger source holds the field: your
            own edits always win, and this run can be undone from its history page afterwards.
          </p>
        </CardContent>
      </Card>

      {(undecided > 0 || refused) && (
        <Note tone="warn">
          {refused ? (
            <>
              <p className="font-medium">
                The import was refused: a row resolves to a candidate right now.
              </p>
              <p>
                Your contacts changed after this file was read, so a row that needed no decision
                then needs one now. Go back and answer it, or skip the ones nobody has answered.
              </p>
            </>
          ) : (
            <>
              <p className="font-medium">
                {undecided} {undecided === 1 ? 'candidate has' : 'candidates have'} no decision.
              </p>
              <p>
                Go back and decide each one, or skip them: a skipped candidate is left out of the
                import entirely and its row is recorded as skipped.
              </p>
            </>
          )}
          <label className="flex items-center gap-2 text-foreground">
            <input
              type="checkbox"
              checked={skipUndecided}
              onChange={(event) => onSkipChange(event.target.checked)}
            />
            {undecided > 0
              ? `Skip the ${undecided} undecided ${undecided === 1 ? 'candidate' : 'candidates'}`
              : 'Skip any candidate nobody has decided'}
          </label>
        </Note>
      )}

      {error && <ErrorNote>{error}</ErrorNote>}

      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" onClick={onBack} disabled={pending}>
          {candidateTotal > 0 ? 'Back to the candidates' : 'Back to the preview'}
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
          <CardTitle level={2}>Imported {run.filename}</CardTitle>
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
      <DuplicateGroupsNote groups={run.duplicate_groups ?? []} />
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
