/**
 * The merge-field helper (#344): every field a template may name, each with a
 * short description and an example value. Activating a field inserts
 * `{{ field }}` at the cursor of the subject or the body, whichever you were in
 * last.
 *
 * The list comes from `GET /templates/merge-fields`, which builds it from the
 * names lint allows, so nothing here names a field. The examples are the
 * contact picked in the preview when there is one, and invented placeholders
 * otherwise.
 */
import { useQuery } from '@tanstack/react-query'
import { useId } from 'react'

import { Button } from '@/components/ui/button'
import { displayName } from '@/features/contacts/format'
import { ErrorNote, LoadingNote } from '@/features/crm/controls'

import { mergeFieldsQuery } from './api'
import type { ContactRow, MergeField } from './api'

export type InsertTarget = 'subject' | 'body'

const GROUPS: ReadonlyArray<{ group: MergeField['group']; label: string }> = [
  { group: 'contact', label: 'Contact' },
  { group: 'personal', label: 'Personal line' },
  { group: 'campaign', label: 'Campaign' },
]

const TARGET_LABELS: Record<InsertTarget, string> = { subject: 'subject', body: 'body' }

interface MergeFieldHelperProps {
  /** The contact picked in the preview, whose values are the examples; null for placeholders. */
  contact: ContactRow | null
  /** Where an insert goes: the field focused last. */
  target: InsertTarget
  disabled?: boolean
  /** Called with the text that goes between the braces, like `first_name`. */
  onInsert: (insert: string) => void
}

export function MergeFieldHelper({ contact, target, disabled, onInsert }: MergeFieldHelperProps) {
  const headingId = useId()
  const fields = useQuery(mergeFieldsQuery(contact?.id ?? null))

  return (
    <section aria-labelledby={headingId} className="space-y-2 rounded-lg border p-3">
      <div className="space-y-0.5">
        <h4 id={headingId} className="text-sm font-medium">
          Merge fields
        </h4>
        <p className="text-xs text-muted-foreground">
          Inserts at the cursor in the {TARGET_LABELS[target]}.{' '}
          {contact === null
            ? 'Examples are made up; pick a contact in the preview to see their values.'
            : `Examples are ${displayName(contact)}'s values.`}
        </p>
      </div>
      {fields.isPending && <LoadingNote label="Loading merge fields…" />}
      {fields.isError && <ErrorNote label="Could not load the merge fields" error={fields.error} />}
      {fields.data !== undefined &&
        GROUPS.map(({ group, label }) => {
          const inGroup = fields.data.fields.filter((field) => field.group === group)
          if (inGroup.length === 0) return null
          return (
            <div key={group} className="space-y-1">
              <p className="text-xs font-medium text-muted-foreground">{label}</p>
              <ul aria-label={`${label} fields`} className="divide-y rounded-md border">
                {inGroup.map((field) => (
                  <FieldRow
                    key={field.name}
                    field={field}
                    disabled={disabled}
                    onInsert={onInsert}
                  />
                ))}
              </ul>
            </div>
          )
        })}
    </section>
  )
}

function FieldRow({
  field,
  disabled,
  onInsert,
}: {
  field: MergeField
  disabled?: boolean
  onInsert: (insert: string) => void
}) {
  const describedBy = useId()
  const token = `{{ ${field.insert} }}`
  return (
    <li className="flex flex-wrap items-start gap-x-3 gap-y-1 px-2 py-1.5 text-sm">
      <Button
        type="button"
        size="xs"
        variant="outline"
        className="font-mono"
        aria-label={`Insert ${token}`}
        aria-describedby={describedBy}
        disabled={disabled}
        onClick={() => onInsert(field.insert)}
      >
        {token}
      </Button>
      <div id={describedBy} className="min-w-0 flex-1 space-y-0.5">
        <p className="text-xs">{field.description}</p>
        <p className="text-xs text-muted-foreground">
          <Example field={field} />
        </p>
      </div>
    </li>
  )
}

function Example({ field }: { field: MergeField }) {
  if (field.example === null) {
    return <>No value for this contact, so it renders empty.</>
  }
  if (field.example_source === 'placeholder') {
    return (
      <>
        Example: <span className="italic">{field.example}</span>
      </>
    )
  }
  return (
    <>
      This contact: <span className="font-medium text-foreground">{field.example}</span>
    </>
  )
}
