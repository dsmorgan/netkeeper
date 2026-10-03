/**
 * The filter for a typed name search, shared by Triage's "Jump to a contact"
 * and the list page's "Add contacts" picker (#322, #336).
 *
 * One clause per word, each matching any of `fields`, ANDed together, so "ada
 * ven" finds Ada Ventura whichever word is the first name. The filter language
 * folds case and escapes `%` and `_`. An empty search returns `null`.
 */
import type { FilterNode } from './types'

/** The name columns every contact search covers. */
export const NAME_FIELDS = ['first_name', 'last_name', 'preferred_name'] as const

export function nameSearchFilter(
  text: string,
  fields: readonly (typeof NAME_FIELDS)[number][] | readonly string[] = NAME_FIELDS,
): FilterNode | null {
  const words = text.split(/\s+/).filter((word) => word !== '')
  if (words.length === 0) return null
  const clauses = words.map((word): FilterNode => ({
    op: 'or',
    children: fields.map((field) => ({ op: 'contains', field, value: word }) as FilterNode),
  }))
  return clauses.length === 1 ? (clauses[0] as FilterNode) : { op: 'and', children: clauses }
}
