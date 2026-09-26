/**
 * The editor for one template: its fields, lint as you type, save and delete.
 *
 * Lint runs on the text in front of you, debounced, through `POST
 * /templates/lint`, so an error shows before you save. Lint never blocks a
 * save; every issue it reports is an error that blocks activating a campaign
 * with the template, and the editor says so.
 */
import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query'
import { useId, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { Input, Textarea } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Callout, ErrorNote } from '@/features/crm/controls'
import { useDebounced } from '@/features/crm/use-debounced'

import { createTemplate, deleteTemplate, lintDraft, templateKeys, updateTemplate } from './api'
import type { TemplateChannel, TemplateDraft, TemplateOut } from './api'
import { LINT_DEBOUNCE_MS, draftOf, sameDraft } from './draft'
import { hasErrors } from './lint'
import { LintList } from './lint-list'

const CHANNELS: ReadonlyArray<{ value: TemplateChannel; label: string }> = [
  { value: 'email', label: 'Email' },
  { value: 'linkedin', label: 'LinkedIn message' },
]

interface TemplateEditorProps {
  /** The current version being edited, or null for a new template. */
  template: TemplateOut | null
  draft: TemplateDraft
  onDraftChange: (draft: TemplateDraft) => void
  onSaved: (saved: TemplateOut) => void
  onDeleted: () => void
}

export function TemplateEditor({
  template,
  draft,
  onDraftChange,
  onSaved,
  onDeleted,
}: TemplateEditorProps) {
  const ids = { name: useId(), channel: useId(), subject: useId(), body: useId() }
  const [confirmDelete, setConfirmDelete] = useState(false)

  // Memoized so the debounce sees one value per edit, not a new object every render.
  const text = useMemo(
    () => ({ channel: draft.channel, subject: draft.subject, body: draft.body }),
    [draft.channel, draft.subject, draft.body],
  )
  const linted = useDebounced(text, LINT_DEBOUNCE_MS)
  const lint = useQuery({
    queryKey: [...templateKeys.all, 'lint', linted] as const,
    queryFn: ({ signal }) => lintDraft(linted, signal),
    placeholderData: keepPreviousData,
    retry: false,
  })
  const issues = lint.data ?? []
  const partHasError = (part: 'subject' | 'body') =>
    issues.some((issue) => issue.part === part && issue.severity === 'error')

  const save = useMutation({
    mutationFn: (value: TemplateDraft) =>
      template === null ? createTemplate(value) : updateTemplate(template.id, value),
    onSuccess: onSaved,
  })
  const remove = useMutation({
    mutationFn: (id: number) => deleteTemplate(id),
    onSuccess: () => {
      setConfirmDelete(false)
      onDeleted()
    },
  })

  const set = (patch: Partial<TemplateDraft>) => onDraftChange({ ...draft, ...patch })
  const unchanged = template !== null && sameDraft(draft, draftOf(template))

  return (
    <Card>
      <CardHeader>
        <CardTitle level={3}>
          {template === null ? 'New template' : `Editing version ${template.version}`}
        </CardTitle>
        <CardDescription>
          Merge fields go in double braces, like {'{{ first_name }}'}.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {template?.in_use === true && (
          <Callout tone="info" title="A campaign uses this version">
            <p>
              Saving creates version {template.version + 1}. The campaign keeps sending version{' '}
              {template.version} as it is.
            </p>
          </Callout>
        )}
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault()
            save.mutate(draft)
          }}
        >
          <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
            <div className="grid gap-1">
              <Label htmlFor={ids.name}>Name</Label>
              <Input
                id={ids.name}
                value={draft.name}
                required
                onChange={(event) => set({ name: event.target.value })}
              />
            </div>
            <div className="grid gap-1">
              <Label htmlFor={ids.channel}>Channel</Label>
              <Select
                id={ids.channel}
                value={draft.channel}
                onChange={(event) => set({ channel: event.target.value as TemplateChannel })}
              >
                {CHANNELS.map((channel) => (
                  <option key={channel.value} value={channel.value}>
                    {channel.label}
                  </option>
                ))}
              </Select>
            </div>
          </div>
          <div className="grid gap-1">
            <Label htmlFor={ids.subject}>Subject</Label>
            <Input
              id={ids.subject}
              value={draft.subject}
              aria-invalid={partHasError('subject') || undefined}
              onChange={(event) => set({ subject: event.target.value })}
            />
            <p className="text-xs text-muted-foreground">
              Required for email. A LinkedIn message has no subject line.
            </p>
          </div>
          <div className="grid gap-1">
            <Label htmlFor={ids.body}>Body</Label>
            <Textarea
              id={ids.body}
              value={draft.body}
              rows={12}
              spellCheck
              className="font-mono"
              aria-invalid={partHasError('body') || undefined}
              onChange={(event) => set({ body: event.target.value })}
            />
          </div>

          <section aria-label="Lint" className="space-y-2">
            {lint.isError ? (
              <ErrorNote label="Could not lint the template" error={lint.error} />
            ) : lint.data === undefined ? null : issues.length === 0 ? (
              <p role="status" className="text-sm text-muted-foreground">
                No lint issues.
              </p>
            ) : (
              <>
                {hasErrors(issues) && (
                  <Callout tone="warning">
                    <p>
                      You can save with lint errors, but a campaign can't use this template until
                      they're fixed.
                    </p>
                  </Callout>
                )}
                <LintList issues={issues} label="Lint issues" />
              </>
            )}
          </section>

          {save.isError && <ErrorNote label="Could not save the template" error={save.error} />}
          {remove.isError && !confirmDelete && (
            <ErrorNote label="Could not delete the template" error={remove.error} />
          )}

          <div className="flex flex-wrap gap-2">
            <Button
              type="submit"
              disabled={save.isPending || unchanged || draft.name.trim() === ''}
            >
              {save.isPending
                ? 'Saving…'
                : template === null
                  ? 'Create template'
                  : template.in_use
                    ? `Save as version ${template.version + 1}`
                    : 'Save'}
            </Button>
            {template !== null && (
              <Button
                type="button"
                variant="destructive"
                className="ml-auto"
                onClick={() => setConfirmDelete(true)}
              >
                Delete
              </Button>
            )}
          </div>
        </form>
      </CardContent>
      {template !== null && (
        <ConfirmDialog
          open={confirmDelete}
          onOpenChange={setConfirmDelete}
          title={`Delete “${template.name}”?`}
          confirmLabel="Delete template"
          onConfirm={() => remove.mutate(template.id)}
          pending={remove.isPending}
          error={remove.isError ? remove.error.message : null}
        >
          <p>
            This deletes the template and every earlier version of it. It is refused while a
            campaign uses any of them.
          </p>
        </ConfirmDialog>
      )}
    </Card>
  )
}
