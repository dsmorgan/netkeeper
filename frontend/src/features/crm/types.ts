/**
 * The CRM types this feature works in, all taken from the generated client.
 *
 * Nothing here is hand-written: every alias points at `src/api/schema.d.ts`,
 * which `make gen-client` regenerates from the backend's OpenAPI document and
 * CI diffs against it. A backend rename therefore breaks `tsc`, not a user.
 */
import type { components, operations } from '@/api/schema'

type Schemas = components['schemas']
type ExportQuery = NonNullable<operations['export_contacts']['parameters']['query']>

/** A filter as the API reads it (`FilterTree-Input`); what the builder produces. */
export type FilterTree = Schemas['FilterTree-Input']
/** One predicate in that tree, discriminated on `op`. */
export type FilterNode = Schemas['FilterNode-Input']
/** Every `op` the filter language defines, placeholders included. */
export type FilterOp = FilterNode['op']
/** One `ORDER BY` term a saved view stores. */
export type SortKey = Schemas['SortKey']
/** A column a filter can compare, as `SortKey` spells the set. */
export type FilterField = SortKey['field']

export type TagOut = Schemas['TagOut']
export type TagKind = Schemas['TagKind']
export type AutotagRuleOut = Schemas['AutotagRuleOut']
export type AutotagRulePreviewOut = Schemas['AutotagRulePreviewOut']
export type RuleField = Schemas['RuleField']

export type ListOut = Schemas['ListOut']
export type ListKind = Schemas['ListKind']
export type ContactSummaryOut = Schemas['ContactSummaryOut']
export type SavedViewOut = Schemas['SavedViewOut']
/** A column a saved view can show, as `ContactQuery` spells the set. */
export type ContactColumn = NonNullable<Schemas['ContactQuery']['columns']>[number]

export type BulkCountOut = Schemas['BulkCountOut']
export type BulkSelection = Schemas['BulkSelection']
export type BulkAction = Schemas['BulkIn']['action']
export type ContactMet = Schemas['ContactMet']
export type EmailStatus = Schemas['EmailStatus']

/** The export presets `/exports` accepts. */
export type ExportPreset = NonNullable<ExportQuery['preset']>
/** The export formats `/exports` accepts. */
export type ExportFormat = NonNullable<ExportQuery['format']>
