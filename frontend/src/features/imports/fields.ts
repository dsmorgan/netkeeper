import type { ColumnMapping, ImportField, PreviewRow, Resolution, RunStatus } from './types'

/** Every field a column may feed, in the order the mapping screen offers them. */
export const IMPORT_FIELDS: readonly ImportField[] = [
  'first_name',
  'last_name',
  'email',
  'phone',
  'current_title',
  'current_company',
  'headline',
  'location',
  'connected_on',
  'li_url',
  'li_public_id',
  'li_urn',
  'link',
]

export const FIELD_LABELS: Record<ImportField, string> = {
  first_name: 'First name',
  last_name: 'Last name',
  email: 'Email',
  phone: 'Phone',
  current_title: 'Job title',
  current_company: 'Company',
  headline: 'Headline',
  location: 'Location',
  connected_on: 'Connected on',
  li_url: 'LinkedIn URL',
  li_public_id: 'LinkedIn public id',
  li_urn: 'LinkedIn URN',
  link: 'Website',
}

/**
 * The fields that write a column of `contacts`, so two columns feeding the same
 * one is worth a warning. The rest (`email`, `phone`, `link`) add child rows,
 * where several columns are ordinary.
 */
export const SCALAR_FIELDS: ReadonlySet<ImportField> = new Set<ImportField>([
  'li_urn',
  'li_public_id',
  'li_url',
  'first_name',
  'last_name',
  'headline',
  'current_title',
  'current_company',
  'location',
  'connected_on',
])

/** Scalar fields more than one column feeds; the last column read would win. */
export function duplicateScalarFields(mapping: ColumnMapping): ImportField[] {
  const seen = new Set<ImportField>()
  const twice = new Set<ImportField>()
  for (const field of Object.values(mapping)) {
    if (!field || !SCALAR_FIELDS.has(field)) continue
    if (seen.has(field)) twice.add(field)
    seen.add(field)
  }
  return [...twice]
}

/** A field's label, falling back to its raw name if the API grows a field first. */
export function fieldLabel(field: string): string {
  return FIELD_LABELS[field as ImportField] ?? field
}

export const RESOLUTION_LABELS: Record<Resolution, string> = {
  matched: 'Matches a contact',
  created: 'New contact',
  candidate: 'Needs a decision',
  skipped: 'Skipped',
}

/** Badge colouring per outcome; `candidate` is the one that wants attention. */
export const RESOLUTION_CLASSES: Record<Resolution, string> = {
  matched: 'bg-sky-500/10 text-sky-700 dark:text-sky-300',
  created: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  candidate: 'bg-amber-500/15 text-amber-800 dark:text-amber-300',
  skipped: 'bg-muted text-muted-foreground',
}

/**
 * A readable name for one row of the file, built from the columns the mapping
 * points at a person's name, then an email, then the first cell that has
 * anything in it. Rows are reviewed one by one, so "row 14" alone is useless.
 */
export function rowLabel(raw: Record<string, string>, mapping: ColumnMapping): string {
  const cell = (wanted: ImportField): string | undefined => {
    for (const [header, field] of Object.entries(mapping)) {
      if (field !== wanted) continue
      const value = raw[header]?.trim()
      if (value) return value
    }
    return undefined
  }
  const name = [cell('first_name'), cell('last_name')].filter(Boolean).join(' ')
  if (name) return name
  const fallback = cell('email') ?? cell('li_url') ?? cell('phone')
  if (fallback) return fallback
  const anything = Object.values(raw).find((value) => value.trim() !== '')
  return anything?.trim() ?? 'an empty row'
}

/** The mapping of a run or an inspection, as the wizard's `ColumnMapping`. */
export function asColumnMapping(
  headers: readonly string[],
  mapped: Record<string, string>,
): ColumnMapping {
  const mapping: ColumnMapping = {}
  for (const header of headers) {
    mapping[header] = (mapped[header] as ImportField | undefined) ?? ''
  }
  return mapping
}

/** Headers no field claims. Their cells are kept on the row but never written. */
export function unmappedHeaders(headers: readonly string[], mapping: ColumnMapping): string[] {
  return headers.filter((header) => !mapping[header])
}

/**
 * Rows that resolved but still had a cell thrown away, with the reason.
 *
 * A cell the importer cannot use — a `Provider Id` that is not a `urn:li:…`, a
 * date it cannot read — does not stop the row, so its reason is the only trace.
 * Skipped rows carry their reason in their own outcome already.
 */
export function droppedCells(rows: readonly PreviewRow[]) {
  return rows.filter((row) => row.problem !== null && row.resolution !== 'skipped')
}

/** Every change in `rows` that provenance would refuse, newest question first. */
export function refusedChanges(rows: readonly PreviewRow[]) {
  return rows.flatMap((row) =>
    row.changes.filter((change) => change.refused).map((change) => ({ row, change })),
  )
}

export const RUN_STATUS_LABELS: Record<RunStatus, string> = {
  draft: 'Draft',
  committed: 'Committed',
  rolled_back: 'Rolled back',
}

export const RUN_STATUS_CLASSES: Record<RunStatus, string> = {
  draft: 'bg-amber-500/15 text-amber-800 dark:text-amber-300',
  committed: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  rolled_back: 'bg-muted text-muted-foreground',
}

/** A timestamp in the reader's own locale, or the raw string if it will not parse. */
export function formatWhen(iso: string | null): string {
  if (iso === null) return '\u2014'
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) return iso
  return when.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}
