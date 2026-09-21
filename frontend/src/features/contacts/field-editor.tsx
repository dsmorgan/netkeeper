import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, Pencil, Undo2, X } from 'lucide-react'
import { useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input, Textarea } from '@/components/ui/input'
import { Select } from '@/components/ui/select'

import { contactsKeys, patchContact, revertContactField, setNotes } from './api'
import { useContactWrite } from './use-contact-write'
import type {
  ContactDetail,
  ContactPatch,
  ContactSource,
  ProvenanceField,
  SyncedValueOut,
} from './types'
import { MET_LABELS, MET_VALUES } from './types'

/** The scalar fields the detail page lets you type into. */
export type EditableField = Extract<
  keyof ContactPatch,
  | 'li_public_id'
  | 'first_name'
  | 'last_name'
  | 'headline'
  | 'current_title'
  | 'current_company'
  | 'location'
  | 'connected_on'
  | 'preferred_name'
  | 'do_not_contact_reason'
>

const PROVENANCE_FIELDS = new Set<string>([
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

export interface FieldEditorProps {
  contact: ContactDetail
  field: EditableField
  label: string
  /** A date field gets a date input and stores `YYYY-MM-DD`. */
  kind?: 'text' | 'date'
}

/**
 * One editable field, with its provenance (spec 10.5, CP1 #28).
 *
 * A LinkedIn field you type into is a manual override: it sticks, and no later
 * sync or import touches it. So the row says so, shows what LinkedIn last
 * reported, and offers to put that back. When nothing was ever synced there is
 * nothing to revert to, and the control says that instead of failing after the
 * click.
 */
export function FieldEditor({ contact, field, label, kind = 'text' }: FieldEditorProps) {
  const raw = contact[field as keyof ContactDetail]
  const value = typeof raw === 'string' ? raw : raw === null ? '' : String(raw ?? '')
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value)
  const write = useContactWrite(contact.id)

  const source: ContactSource | undefined = contact.field_sources[field]
  const synced: SyncedValueOut | undefined = contact.synced_values[field]
  const overridden = contact.overridden_fields.includes(field)
  const provenance = PROVENANCE_FIELDS.has(field)
  const manual = source === 'manual'

  function start() {
    setDraft(value)
    setEditing(true)
  }

  function save() {
    const next = draft.trim() === '' ? null : draft
    write.mutate(() => patchContact(contact.id, { [field]: next } as ContactPatch), {
      onSuccess: () => setEditing(false),
    })
  }

  return (
    <div className="grid gap-1 border-b border-border/60 py-2 last:border-b-0">
      <div className="flex items-center gap-2">
        <span className="w-40 shrink-0 text-muted-foreground">{label}</span>
        {editing ? (
          <div className="flex flex-1 items-center gap-1">
            <Input
              aria-label={`${label} value`}
              type={kind === 'date' ? 'date' : 'text'}
              value={draft}
              autoFocus
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') save()
                if (event.key === 'Escape') setEditing(false)
              }}
            />
            <Button
              size="icon-sm"
              aria-label={`Save ${label}`}
              onClick={save}
              disabled={write.isPending}
            >
              <Check />
            </Button>
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label={`Cancel editing ${label}`}
              onClick={() => setEditing(false)}
            >
              <X />
            </Button>
          </div>
        ) : (
          <>
            <span className="flex-1 min-w-0 truncate">
              {value === '' ? <span className="text-muted-foreground">—</span> : value}
            </span>
            {overridden && <Badge variant="outline">Manual override</Badge>}
            <Button size="icon-xs" variant="ghost" aria-label={`Edit ${label}`} onClick={start}>
              <Pencil />
            </Button>
          </>
        )}
      </div>

      {provenance && manual && !editing && (
        <RevertControl
          contact={contact}
          field={field as ProvenanceField}
          label={label}
          synced={synced}
        />
      )}
      {write.isError && (
        <p role="alert" className="pl-40 text-destructive">
          {write.error.message}
        </p>
      )}
    </div>
  )
}

function RevertControl({
  contact,
  field,
  label,
  synced,
}: {
  contact: ContactDetail
  field: ProvenanceField
  label: string
  synced: SyncedValueOut | undefined
}) {
  const write = useContactWrite(contact.id)
  const revertable = synced !== undefined
  const reason = revertable
    ? `${synced.source} last reported ${synced.value === null ? 'nothing' : `“${synced.value}”`}`
    : 'Never synced, so there is nothing to revert to'

  return (
    <div className="flex items-center gap-2 pl-40 text-xs text-muted-foreground">
      <Button
        size="xs"
        variant="outline"
        disabled={!revertable || write.isPending}
        title={reason}
        aria-label={`Revert ${label}`}
        onClick={() => write.mutate(() => revertContactField(contact.id, field))}
      >
        <Undo2 data-icon="inline-start" />
        Revert
      </Button>
      <span>{reason}</span>
      {write.isError && (
        <span role="alert" className="text-destructive">
          {write.error.message}
        </span>
      )}
    </div>
  )
}

/** `met` is the person's own call, so it has no provenance and no revert. */
export function MetEditor({ contact }: { contact: ContactDetail }) {
  const write = useContactWrite(contact.id)
  return (
    <div className="flex items-center gap-2 border-b border-border/60 py-2">
      <span className="w-40 shrink-0 text-muted-foreground">Met</span>
      <Select
        aria-label="Met"
        value={contact.met}
        disabled={write.isPending}
        onChange={(event) =>
          write.mutate(() =>
            patchContact(contact.id, { met: event.target.value as ContactDetail['met'] }),
          )
        }
      >
        {MET_VALUES.map((value) => (
          <option key={value} value={value}>
            {MET_LABELS[value]}
          </option>
        ))}
      </Select>
    </div>
  )
}

export function DoNotContactEditor({ contact }: { contact: ContactDetail }) {
  const write = useContactWrite(contact.id)
  return (
    <div className="flex items-center gap-2 border-b border-border/60 py-2">
      <span className="w-40 shrink-0 text-muted-foreground">Do not contact</span>
      <Button
        size="sm"
        variant={contact.do_not_contact ? 'destructive' : 'outline'}
        disabled={write.isPending}
        onClick={() =>
          write.mutate(() => patchContact(contact.id, { do_not_contact: !contact.do_not_contact }))
        }
      >
        {contact.do_not_contact ? 'Do not contact' : 'Contact allowed'}
      </Button>
    </div>
  )
}

/** Notes are Markdown, replaced whole, and are never touched by an import. */
export function NotesEditor({ contact }: { contact: ContactDetail }) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState(contact.notes ?? '')
  const [dirty, setDirty] = useState(false)
  const save = useMutation({
    mutationFn: (text: string) => setNotes(contact.id, text.trim() === '' ? null : text),
    onSuccess: () => {
      setDirty(false)
      void queryClient.invalidateQueries({ queryKey: contactsKeys.detail(contact.id) })
    },
  })

  return (
    <div className="grid gap-2">
      <Textarea
        aria-label="Notes"
        rows={4}
        value={draft}
        onChange={(event) => {
          setDraft(event.target.value)
          setDirty(true)
        }}
      />
      <div className="flex items-center gap-2">
        <Button size="sm" disabled={!dirty || save.isPending} onClick={() => save.mutate(draft)}>
          Save notes
        </Button>
        {save.isError && (
          <span role="alert" className="text-destructive">
            {save.error.message}
          </span>
        )}
      </div>
    </div>
  )
}
