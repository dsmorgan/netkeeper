/**
 * A saved template version rendered for one contact you pick.
 *
 * The subject and body are shown as plain text, never as HTML: a merge value
 * is imported data, and a contact named `<img src=x onerror=...>` must read as
 * exactly that. A field the contact has no value for renders empty and shows
 * as a warning under the preview; only a template that cannot render at all
 * is an error.
 */
import { useQuery } from '@tanstack/react-query'
import { useId, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { displayName } from '@/features/contacts/format'
import { Callout, ErrorNote, LoadingNote } from '@/features/crm/controls'
import { useDebounced } from '@/features/crm/use-debounced'

import { contactSearchQuery, previewQuery } from './api'
import type { ContactRow } from './api'
import { LintList } from './lint-list'

interface PreviewPanelProps {
  /** The saved version to render, or null for a template not saved yet. */
  templateId: number | null
  /** Set when the editor holds changes the saved version does not have. */
  unsaved: boolean
  /** The contact picked to preview for; the merge-field helper shows their values too. */
  contact: ContactRow | null
  onContactChange: (contact: ContactRow | null) => void
}

export function PreviewPanel({
  templateId,
  unsaved,
  contact,
  onContactChange: setContact,
}: PreviewPanelProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle level={3}>Preview</CardTitle>
        <CardDescription>
          Pick a contact to see this template filled in for them, as plain text.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {contact === null ? (
          <ContactPicker onPick={setContact} />
        ) : (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span>
              Previewing for <span className="font-medium">{displayName(contact)}</span>
            </span>
            <Button size="sm" variant="ghost" onClick={() => setContact(null)}>
              Change contact
            </Button>
          </div>
        )}
        {templateId === null ? (
          <Callout>
            <p>Save the template to preview it.</p>
          </Callout>
        ) : (
          <>
            {unsaved && (
              <Callout tone="warning">
                <p>The preview shows the saved version. Save to preview your changes.</p>
              </Callout>
            )}
            {contact !== null && <Rendered templateId={templateId} contactId={contact.id} />}
          </>
        )}
      </CardContent>
    </Card>
  )
}

function Rendered({ templateId, contactId }: { templateId: number; contactId: number }) {
  const preview = useQuery(previewQuery(templateId, contactId))
  if (preview.isPending) return <LoadingNote label="Rendering…" />
  if (preview.isError)
    return <ErrorNote label="Could not render the preview" error={preview.error} />
  const { subject, body, issues } = preview.data
  return (
    <div className="space-y-3">
      <section aria-label="Rendered message" className="space-y-2 rounded-lg border p-3">
        {subject !== null && (
          <div>
            <p className="text-xs text-muted-foreground">Subject</p>
            <p
              data-testid="preview-subject"
              className="font-medium break-words whitespace-pre-wrap"
            >
              {subject}
            </p>
          </div>
        )}
        <div>
          <p className="text-xs text-muted-foreground">Body</p>
          <pre
            data-testid="preview-body"
            className="font-sans text-sm break-words whitespace-pre-wrap"
          >
            {body}
          </pre>
        </div>
      </section>
      {issues.length > 0 && <LintList issues={issues} label="Preview issues" />}
    </div>
  )
}

function ContactPicker({ onPick }: { onPick: (contact: ContactRow) => void }) {
  const inputId = useId()
  const [q, setQ] = useState('')
  const settled = useDebounced(q.trim(), 250)
  const results = useQuery({ ...contactSearchQuery(settled), enabled: settled !== '' })

  return (
    <div className="space-y-2">
      <div className="grid gap-1">
        <Label htmlFor={inputId}>Contact</Label>
        <Input
          id={inputId}
          type="search"
          value={q}
          placeholder="Search by name, company, or title"
          onChange={(event) => setQ(event.target.value)}
        />
      </div>
      {settled !== '' && results.isPending && <LoadingNote label="Searching…" />}
      {results.isError && <ErrorNote label="Could not search the contacts" error={results.error} />}
      {results.data !== undefined &&
        (results.data.length === 0 ? (
          <p className="text-sm text-muted-foreground">No contact matches “{settled}”.</p>
        ) : (
          <ul aria-label="Matching contacts" className="divide-y rounded-lg border">
            {results.data.map((row) => (
              <li key={row.id}>
                <button
                  type="button"
                  className="flex w-full flex-col items-start px-3 py-2 text-left text-sm hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
                  onClick={() => onPick(row)}
                >
                  <span className="font-medium">{displayName(row)}</span>
                  {(row.current_title || row.current_company) && (
                    <span className="text-xs text-muted-foreground">
                      {[row.current_title, row.current_company].filter(Boolean).join(' · ')}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        ))}
    </div>
  )
}
