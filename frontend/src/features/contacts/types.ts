import type { components } from '@/api/schema'

export type ContactRow = components['schemas']['ContactRow']
export type ContactDetail = components['schemas']['ContactDetail']
export type ContactPage = components['schemas']['ContactPage']
export type ContactPatch = components['schemas']['ContactPatch']
export type ContactCreate = components['schemas']['ContactCreate']
export type DuplicateContact = components['schemas']['DuplicateContact']
export type ContactMet = components['schemas']['ContactMet']
export type ContactSource = components['schemas']['ContactSource']
/**
 * The filter tree, in the two shapes the schema publishes: `-Input` is what a
 * request sends, `-Output` what a saved view comes back as. They are the same
 * shape; the export splits them because the tree is now both.
 */
export type FilterTree = components['schemas']['FilterTree-Input']
export type FilterNode = components['schemas']['FilterNode-Input']
export type FilterTreeOut = components['schemas']['FilterTree-Output']
export type SortKey = components['schemas']['SortKey']
export type SortField = SortKey['field']
export type SortDirection = SortKey['direction']
/** The scalar columns `POST /contacts/query` may be asked for. */
export type ContactColumn = NonNullable<components['schemas']['ContactQuery']['columns']>[number]
export type BulkAction = components['schemas']['BulkIn']['action']
export type BulkSelection = components['schemas']['BulkSelection']
export type BulkCountOut = components['schemas']['BulkCountOut']
export type TagOut = components['schemas']['TagOut']
export type SavedView = components['schemas']['SavedViewOut']
export type ContactList = components['schemas']['ListOut']
export type ContactTagOut = components['schemas']['ContactTagOut']
/** The LinkedIn fields that carry provenance, and so a revert. */
export type ProvenanceField = components['schemas']['RevertFieldIn']['field']
export type TimelineEntry = components['schemas']['ContactDetail']['timeline'][number]
export type SnapshotOut = components['schemas']['SnapshotOut']
export type SyncedValueOut = components['schemas']['SyncedValueOut']
export type ContactEmailOut = components['schemas']['ContactEmailOut']
export type ContactPhoneOut = components['schemas']['ContactPhoneOut']
export type ContactLinkOut = components['schemas']['ContactLinkOut']
export type ContactPositionOut = components['schemas']['ContactPositionOut']

export const MET_VALUES = [
  'unknown',
  'met',
  'not_met',
  'skip',
] as const satisfies readonly ContactMet[]

export const MET_LABELS: Record<ContactMet, string> = {
  unknown: 'Unknown',
  met: 'Met',
  not_met: 'Not met',
  skip: 'Skipped',
}
