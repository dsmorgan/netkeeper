/**
 * The filterable columns, mirroring `x-netkeeper-fields` in the OpenAPI document.
 *
 * `netkeeper.crm.filters.FilterTree` publishes each field's kind, label, the
 * ops it takes, and an enum's values under that extension key, but
 * `openapi-typescript` keeps only the types, so the builder needs the metadata
 * in a form it can iterate. `fields.test.ts` reads the extension straight out
 * of `openapi.json` and fails when this table and the backend disagree, so the
 * duplication cannot drift silently.
 */
import type { ContactColumn, FilterField } from './types'

/** How a column's values are typed, which decides its ops and its editor. */
export type FieldKind = 'string' | 'enum' | 'int' | 'date' | 'datetime' | 'bool'

/** The comparison ops a field of each kind accepts. */
export const OPS_BY_KIND = {
  string: ['eq', 'neq', 'contains', 'starts_with', 'is_empty'],
  enum: ['eq', 'neq'],
  int: ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'between'],
  date: ['eq', 'neq', 'is_empty', 'gt', 'gte', 'lt', 'lte', 'between'],
  datetime: ['eq', 'neq', 'is_empty', 'gt', 'gte', 'lt', 'lte', 'between'],
  bool: ['eq', 'neq'],
} as const satisfies Record<FieldKind, readonly string[]>

export interface FieldSpec {
  readonly name: FilterField
  readonly kind: FieldKind
  /** The backend's own wording, so the UI and an error message agree. */
  readonly label: string
  /** The enum's allowed values, in declaration order; empty for other kinds. */
  readonly values?: readonly string[]
}

/** Every filterable column, in the order the backend declares them. */
export const FIELDS: readonly FieldSpec[] = [
  { name: 'first_name', kind: 'string', label: 'first name' },
  { name: 'last_name', kind: 'string', label: 'last name' },
  { name: 'preferred_name', kind: 'string', label: 'preferred name' },
  { name: 'headline', kind: 'string', label: 'headline' },
  { name: 'current_title', kind: 'string', label: 'title' },
  { name: 'current_company', kind: 'string', label: 'company' },
  { name: 'location', kind: 'string', label: 'location' },
  { name: 'li_public_id', kind: 'string', label: 'LinkedIn id' },
  { name: 'met', kind: 'enum', label: 'met', values: ['unknown', 'met', 'not_met', 'skip'] },
  { name: 'source', kind: 'enum', label: 'source', values: ['sync', 'archive', 'csv', 'manual'] },
  { name: 'degree', kind: 'int', label: 'degree' },
  { name: 'connected_on', kind: 'date', label: 'connected on' },
  { name: 'last_contacted_at', kind: 'datetime', label: 'last contacted' },
  { name: 'last_enriched_at', kind: 'datetime', label: 'last enriched' },
  { name: 'triaged_at', kind: 'datetime', label: 'triaged' },
  { name: 'li_disconnected_at', kind: 'datetime', label: 'disconnected on LinkedIn' },
  { name: 'archived_at', kind: 'datetime', label: 'archived' },
  { name: 'created_at', kind: 'datetime', label: 'created' },
  { name: 'updated_at', kind: 'datetime', label: 'updated' },
  { name: 'do_not_contact', kind: 'bool', label: 'do not contact' },
]

const BY_NAME = new Map<string, FieldSpec>(FIELDS.map((spec) => [spec.name, spec]))

/** The spec for `name`, or undefined for a column this build does not know. */
export function fieldSpec(name: string): FieldSpec | undefined {
  return BY_NAME.get(name)
}

/**
 * Every column the contacts query offers, in the backend's own order.
 *
 * A saved view stores a subset of these. `fields.test.ts` compares the list
 * with the enum in `openapi.json`, so a column the backend adds does not go
 * quietly missing from the view editor.
 */
export const CONTACT_COLUMNS = [
  'li_urn',
  'li_public_id',
  'li_url',
  'first_name',
  'last_name',
  'preferred_name',
  'headline',
  'current_title',
  'current_company',
  'location',
  'connected_on',
  'degree',
  'met',
  'triaged_at',
  'do_not_contact',
  'do_not_contact_reason',
  'li_missing_count',
  'li_disconnected_at',
  'last_enriched_at',
  'enrich_priority',
  'last_contacted_at',
  'notes',
  'archived_at',
  'source',
  'created_at',
  'updated_at',
] as const satisfies readonly ContactColumn[]
