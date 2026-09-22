/**
 * The wizard's two shapes (spec 10.5, P1-21).
 *
 * A CSV goes through mapping, preview, and candidate review before anything is
 * written. The archive zip, and a lone `messages.csv` or `Invitations.csv`,
 * have none of that — `POST /imports/archive` runs them straight through in
 * one step — so they get their own, shorter nav rather than the CSV steps with
 * some skipped: skipping steps in silence is exactly what a person cannot tell
 * apart from the wizard being broken.
 */
export const CSV_STEPS = ['upload', 'mapping', 'preview', 'candidates', 'commit'] as const
export const ARCHIVE_STEPS = ['upload', 'review', 'result'] as const

export type CsvStep = (typeof CSV_STEPS)[number]
export type ArchiveStep = (typeof ARCHIVE_STEPS)[number]
export type Step = CsvStep | ArchiveStep

export const STEP_LABELS: Record<Step, string> = {
  upload: 'Upload',
  mapping: 'Map columns',
  preview: 'Preview',
  candidates: 'Candidates',
  commit: 'Commit',
  review: 'Review',
  result: 'Result',
}
