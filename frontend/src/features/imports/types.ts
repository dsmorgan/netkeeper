import type { paths } from '@/api/schema'

/**
 * The import shapes, derived from the generated client's `paths`.
 *
 * Same rule as `api/client.ts`: nothing here is hand-written, so a rename in
 * the OpenAPI export surfaces as a type error rather than as a wrong field
 * name at runtime.
 */
type JsonOf<T> = T extends { content: { 'application/json': infer Body } } ? Body : never

export type Inspection = JsonOf<paths['/api/v1/imports/inspect']['post']['responses'][200]>

/** What a column may feed: `first_name`, `email`, and the rest (spec 10.5). */
export type ImportField = Inspection['mapping'][string]

/** A header-to-field map as the wizard holds it; `''` means "leave this column out". */
export type ColumnMapping = Record<string, ImportField | ''>

export type PresetList = JsonOf<paths['/api/v1/imports/presets']['get']['responses'][200]>
export type Preset = PresetList['builtin'][number]

export type ImportRun = JsonOf<paths['/api/v1/imports/{run_id}']['get']['responses'][200]>
export type ImportRunPage = JsonOf<paths['/api/v1/imports']['get']['responses'][200]>
export type RunStatus = ImportRun['status']

export type PreviewRow = JsonOf<
  paths['/api/v1/imports/{run_id}/preview']['post']['responses'][200]
>[number]
export type PlannedChange = PreviewRow['changes'][number]
export type Resolution = PreviewRow['resolution']

export type ImportRowPage = JsonOf<paths['/api/v1/imports/{run_id}/rows']['get']['responses'][200]>
export type ImportRow = ImportRowPage['items'][number]
export type RefusedField = ImportRow['refused'][number]

export type CommitBody = JsonOf<paths['/api/v1/imports/{run_id}/commit']['post']['requestBody']>
export type Decision = NonNullable<CommitBody['decisions']>[number]

export type RollbackResult = JsonOf<
  paths['/api/v1/imports/{run_id}/rollback']['post']['responses'][200]
>
