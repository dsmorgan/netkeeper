import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

import { fieldLabel, rowLabel } from './fields'
import { ErrorNote, Note } from './notes'
import type { ColumnMapping, Decision, ImportRow, ImportRun, PreviewRow } from './types'

interface CandidatesStepProps {
  run: ImportRun
  rows: ImportRow[]
  total: number
  /** Preview rows by row number: the closest contact's current values, where known. */
  previews: Map<number, PreviewRow>
  mapping: ColumnMapping
  decisions: Record<number, Decision>
  onDecide: (decision: Decision) => void
  onDecideRestAsNew: () => void
  onLoadMore: () => void
  canLoadMore: boolean
  pending: boolean
  error: string | null
  onBack: () => void
  onContinue: () => void
}

/** What the file carries for this row, so a merge is a decision and not a guess. */
function Incoming({ raw, mapping }: { raw: Record<string, string>; mapping: ColumnMapping }) {
  const facts = Object.entries(mapping)
    .filter(([header, field]) => field && (raw[header] ?? '').trim() !== '')
    .map(([header, field]) => [fieldLabel(field as string), raw[header]?.trim() ?? ''] as const)
  if (facts.length === 0) return null
  return (
    <dl className="flex flex-wrap gap-x-4 text-muted-foreground">
      {facts.map(([label, text]) => (
        <div key={label} className="flex gap-1">
          <dt>{label}:</dt>
          <dd className="text-foreground">{text}</dd>
        </div>
      ))}
    </dl>
  )
}

/** The closest contact's values today, taken from the preview of this row. */
function existingSummary(preview: PreviewRow | undefined): string | null {
  if (preview === undefined) return null
  const held = preview.changes
    .filter((change) => change.before !== null && change.before !== '')
    .map((change) => `${fieldLabel(change.field)} “${change.before}”`)
  return held.length === 0 ? null : held.join(', ')
}

/**
 * Step 4: one decision per candidate — merge into a contact, or create a new one.
 *
 * The commit endpoint answers 409 while a candidate is undecided, so the count
 * of undecided rows is carried forward and the commit button stays out of reach
 * until it is zero or the person chooses to skip them.
 */
export function CandidatesStep({
  run,
  rows,
  total,
  previews,
  mapping,
  decisions,
  onDecide,
  onDecideRestAsNew,
  onLoadMore,
  canLoadMore,
  pending,
  error,
  onBack,
  onContinue,
}: CandidatesStepProps) {
  const decided = Object.keys(decisions).length
  const undecided = total - decided

  if (total === 0) {
    return (
      <div className="flex max-w-4xl flex-col gap-4">
        <Card>
          <CardHeader>
            <CardTitle>No candidates</CardTitle>
            <CardDescription>
              Every row of {run.filename} either matches one contact or is plainly a new person, so
              there is nothing to decide.
            </CardDescription>
          </CardHeader>
        </Card>
        <div className="flex gap-2">
          <Button variant="outline" onClick={onBack}>
            Back to the preview
          </Button>
          <Button onClick={onContinue}>Go to commit</Button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex max-w-4xl flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Decide the candidates</CardTitle>
          <CardDescription>
            These rows look like someone you already have, but not closely enough to be sure. Each
            one needs a decision before the import can be committed.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <p role="status">
            {decided} of {total} decided
            {undecided > 0 ? `, ${undecided} to go` : ''}.
          </p>
          {undecided > 0 && (
            <Button variant="outline" onClick={onDecideRestAsNew}>
              Create a new contact for the rest
            </Button>
          )}
        </CardContent>
      </Card>

      {pending && <p role="status">Loading the candidates…</p>}
      {error && <ErrorNote>{error}</ErrorNote>}

      {rows.map((row) => {
        const chosen = decisions[row.row_number]
        const name = `decision-row-${row.row_number}`
        const existing = existingSummary(previews.get(row.row_number))
        return (
          <Card key={row.row_number}>
            <CardHeader>
              <CardTitle>
                Row {row.row_number}: {rowLabel(row.raw, mapping)}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <Incoming raw={row.raw} mapping={mapping} />
              <fieldset className="space-y-1.5">
                <legend className="sr-only">
                  What to do with row {row.row_number}, {rowLabel(row.raw, mapping)}
                </legend>
                {row.candidate_ids.map((contactId, index) => (
                  <label key={contactId} className="flex items-start gap-2">
                    <input
                      type="radio"
                      name={name}
                      className="mt-1"
                      checked={chosen?.kind === 'merge_into' && chosen.contact_id === contactId}
                      onChange={() =>
                        onDecide({
                          row_number: row.row_number,
                          kind: 'merge_into',
                          contact_id: contactId,
                        })
                      }
                    />
                    <span>
                      Merge into contact #{contactId}
                      {index === 0 && existing !== null && (
                        <span className="block text-muted-foreground">today: {existing}</span>
                      )}
                    </span>
                  </label>
                ))}
                <label className="flex items-start gap-2">
                  <input
                    type="radio"
                    name={name}
                    className="mt-1"
                    checked={chosen?.kind === 'create_new'}
                    onChange={() =>
                      onDecide({
                        row_number: row.row_number,
                        kind: 'create_new',
                        contact_id: null,
                      })
                    }
                  />
                  <span>Create a new contact</span>
                </label>
              </fieldset>
            </CardContent>
          </Card>
        )
      })}

      {canLoadMore && (
        <div>
          <Button variant="outline" onClick={onLoadMore} disabled={pending}>
            Load the next candidates ({rows.length} of {total} shown)
          </Button>
        </div>
      )}

      {undecided > 0 && (
        <Note tone="warn">
          <p>
            {undecided} {undecided === 1 ? 'candidate is' : 'candidates are'} still undecided. The
            commit stays unavailable until each one has an answer, or until you choose to skip them
            on the next screen.
          </p>
        </Note>
      )}

      <div className="flex gap-2">
        <Button variant="outline" onClick={onBack}>
          Back to the preview
        </Button>
        <Button onClick={onContinue}>Go to commit</Button>
      </div>
    </div>
  )
}
