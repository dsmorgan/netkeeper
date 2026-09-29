import { validateTree } from '@/features/crm/tree'
import type { FilterTree } from '@/features/crm/types'

/** Where a campaign's audience comes from: a list, a filter, or not chosen yet. */
export type AudienceSource =
  { kind: 'none' } | { kind: 'list'; listId: number | null } | { kind: 'filter'; tree: FilterTree }

/** The source as `POST /campaigns` and `.../enroll` take it. */
export function sourceBody(source: AudienceSource): {
  list_id?: number | null
  filter?: FilterTree | null
} {
  if (source.kind === 'list') return { list_id: source.listId }
  if (source.kind === 'filter') return { filter: source.tree }
  return {}
}

/** Why the source cannot be used yet, or null when it can. */
export function sourceProblem(source: AudienceSource): string | null {
  if (source.kind === 'list' && source.listId === null) return 'Pick a list.'
  if (source.kind === 'filter') {
    if (source.tree.where === null || source.tree.where === undefined) {
      return 'Add at least one condition to the filter.'
    }
    if (validateTree(source.tree).length > 0) return 'Finish the filter first.'
  }
  return null
}
