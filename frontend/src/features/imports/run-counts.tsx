import type { ImportRun } from './types'

const PLAN_LABELS = {
  total: 'Rows in the file',
  matched: 'Match a contact',
  created: 'New contacts',
  candidate: 'Need a decision',
  skipped: 'Nothing usable',
} as const

const DONE_LABELS = {
  total: 'Rows in the file',
  matched: 'Matched a contact',
  created: 'Contacts created',
  candidate: 'Were candidates',
  skipped: 'Skipped',
} as const

/**
 * The run's counts for the whole file, which is what the twenty previewed rows
 * cannot say. On a draft they are a plan; on a committed run, what happened.
 */
export function RunCounts({ run, tense }: { run: ImportRun; tense: 'plan' | 'done' }) {
  const labels = tense === 'plan' ? PLAN_LABELS : DONE_LABELS
  const items: ReadonlyArray<[string, number]> = [
    [labels.total, run.total_rows],
    [labels.matched, run.matched_count],
    [labels.created, run.created_count],
    [labels.candidate, run.candidate_count],
    [labels.skipped, run.skipped_count],
  ]
  return (
    <dl className="flex flex-wrap gap-x-6 gap-y-2">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="text-lg tabular-nums">{value}</dd>
        </div>
      ))}
    </dl>
  )
}
