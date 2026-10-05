import type { RunStatus } from './types'

export const RUN_STATUS_CLASSES: Record<RunStatus, string> = {
  running: 'bg-sky-500/10 text-sky-700 dark:text-sky-300',
  completed: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  aborted: 'bg-amber-500/15 text-amber-800 dark:text-amber-300',
  failed: 'bg-destructive/10 text-destructive',
}

/**
 * How a run ended, in plain words: the server's `stop_reason_text` ("outside active
 * hours"), falling back to the stored reason for one it has no words for yet (#213).
 */
export function stopReasonLabel(run: {
  stop_reason: string | null
  stop_reason_text: string | null
}): string | null {
  return run.stop_reason_text ?? run.stop_reason
}

/** A timestamp in the reader's own locale, or the raw string if it will not parse. */
export function formatWhen(iso: string | null): string {
  if (iso === null) return '—'
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) return iso
  return when.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

/** One line of a formatted progress or counts object. */
export interface Field {
  label: string
  value: string
}

/** A raw value as a short, readable string: not a name, not a URN — this is `progress`/`counts`. */
function display(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (typeof value === 'number' || typeof value === 'string') return String(value)
  return JSON.stringify(value)
}

function titleCase(key: string): string {
  return key
    .split('_')
    .map((word) => (word.length === 0 ? word : word[0]?.toUpperCase() + word.slice(1)))
    .join(' ')
}

/**
 * The order the two known progress/counts shapes are shown in (spec 9.4, 9.10's
 * `ProgressEvent`): enrichment's (`planned`, `visited`, `harvested`, `not_found`,
 * `unreadable`, `stopped`) or a connections sync's (`mode`, `pages`, `connections`,
 * `total`, `stopped`). Both are `dict[str, Any]` on the wire — untyped on purpose,
 * since a job's own progress dataclass is free to change — so this only orders
 * whichever of these keys shows up and appends anything else after, rather than
 * assuming one shape and dropping what does not fit it.
 */
const KNOWN_ORDER = [
  'mode',
  'kind',
  'planned',
  'visited',
  'pages',
  'connections',
  'harvested',
  'not_found',
  'unreadable',
  'total',
  'stopped',
]

/**
 * Per-visit and per-answer records (#405) that live in `counts`/`progress` but are
 * lists, not counts: the run detail shows them as their own table from
 * `GET /linkedin/runs/{id}/diagnostics`, never as a JSON blob in a field.
 */
const RECORD_KEYS = ['unreadable_visits', 'lost']

/** `progress`/`counts` (both `Record<string, unknown> | null`) as labeled, ordered lines. */
export function formatFields(data: Record<string, unknown> | null | undefined): Field[] {
  if (data === null || data === undefined) return []
  const keys = Object.keys(data).filter(
    (key) => !(RECORD_KEYS.includes(key) && Array.isArray(data[key])),
  )
  const ordered = [
    ...KNOWN_ORDER.filter((key) => keys.includes(key)),
    ...keys.filter((key) => !KNOWN_ORDER.includes(key)).sort(),
  ]
  return ordered.map((key) => ({ label: titleCase(key), value: display(data[key]) }))
}

/** The first few fields of `progress`/`counts`, as one line for a table cell. */
export function summarizeFields(data: Record<string, unknown> | null | undefined, max = 3): string {
  const fields = formatFields(data).slice(0, max)
  if (fields.length === 0) return '—'
  return fields.map((field) => `${field.label.toLowerCase()}: ${field.value}`).join(', ')
}
