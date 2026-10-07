/**
 * The editor for one template: its fields, the merge-field helper, lint as you
 * type, save and delete.
 *
 * Lint runs on the text in front of you, debounced, through `POST
 * /templates/lint`, so an error shows before you save. Lint never blocks a
 * save; an error blocks activating a campaign with the template, and the
 * editor says so; a warning does not. Each finding shows inline, under the
 * field it is about, with its line and why it matters, and the body marks the
 * lines that have one (#344). While the result on screen is for older text
 * (during the debounce, and while the request is out), the findings are dimmed
 * and marked busy, and the editor says it is checking.
 *
 * The merge-field helper inserts `{{ field }}` at the cursor of the subject or
 * the body, whichever was focused last (the body until you focus one), and
 * leaves the cursor after the insert.
 *
 * The "Draft with your AI assistant" helper (#368) builds a prompt to copy into
 * an AI chat assistant and fills the subject and body from the reply you paste
 * back. A paste lints at once, skipping the typing debounce. A paste that
 * replaces text you had can be undone until the next edit or paste.
 */
import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query'
import { useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { Input, Textarea } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Callout, ErrorNote } from '@/features/crm/controls'
import { useDebounced } from '@/features/crm/use-debounced'
import { cn } from '@/lib/utils'

import { createTemplate, deleteTemplate, lintDraft, templateKeys, updateTemplate } from './api'
import type { ContactRow, TemplateChannel, TemplateDraft, TemplateOut } from './api'
import { AiDraftHelper, type AppliedStep } from './ai-draft-helper'
import { LINT_DEBOUNCE_MS, draftOf, sameDraft } from './draft'
import { InlineLint, LineMarks } from './inline-lint'
import { hasErrors, issuesFor, lineRange, severityByLine } from './lint'
import { MergeFieldHelper, type InsertTarget } from './merge-field-helper'

const CHANNELS: ReadonlyArray<{ value: TemplateChannel; label: string }> = [
  { value: 'email', label: 'Email' },
  { value: 'linkedin', label: 'LinkedIn message' },
]

interface TemplateEditorProps {
  /** The current version being edited, or null for a new template. */
  template: TemplateOut | null
  draft: TemplateDraft
  onDraftChange: (draft: TemplateDraft) => void
  /** `sent` is the draft the save sent, which the draft may have moved on from since. */
  onSaved: (saved: TemplateOut, sent: TemplateDraft) => void
  onDeleted: () => void
  /** The contact picked in the preview, whose values the merge-field examples show. */
  sampleContact?: ContactRow | null
}

/** Where a field's cursor goes once an insert has rendered. */
interface PendingCaret {
  target: InsertTarget
  at: number
}

export function TemplateEditor({
  template,
  draft,
  onDraftChange,
  onSaved,
  onDeleted,
  sampleContact = null,
}: TemplateEditorProps) {
  const ids = {
    name: useId(),
    channel: useId(),
    subject: useId(),
    subjectHint: useId(),
    subjectLint: useId(),
    body: useId(),
    bodyLint: useId(),
    aiForm: useId(),
  }
  const [confirmDelete, setConfirmDelete] = useState(false)
  const subjectRef = useRef<HTMLInputElement>(null)
  const bodyRef = useRef<HTMLTextAreaElement>(null)
  // The field an insert goes into, and the fields you have been in: one you never
  // focused has no cursor of yours, so an insert there goes at the end.
  const [pickedTarget, setTarget] = useState<InsertTarget>('body')
  // A LinkedIn template has no subject to insert into.
  const noSubject = draft.channel === 'linkedin'
  const target: InsertTarget = noSubject ? 'body' : pickedTarget
  const focused = useRef(new Set<InsertTarget>())
  const caret = useRef<PendingCaret | null>(null)
  const [bodyScroll, setBodyScroll] = useState(0)

  // Memoized so the debounce sees one value per edit, not a new object every render.
  const text = useMemo(
    () => ({ channel: draft.channel, subject: draft.subject, body: draft.body }),
    [draft.channel, draft.subject, draft.body],
  )
  const debounced = useDebounced(text, LINT_DEBOUNCE_MS)
  // Text a paste put in, linted at once rather than after the debounce.
  const [pasted, setPasted] = useState<typeof text | null>(null)
  // What the last paste replaced, and the draft it left, so it can be undone while
  // the draft is still exactly that.
  const [undo, setUndo] = useState<{ before: AppliedText; after: TemplateDraft } | null>(null)
  const canUndo = undo !== null && sameDraft(draft, undo.after)
  const linted = pasted !== null && sameText(pasted, text) ? text : debounced
  const lint = useQuery({
    queryKey: [...templateKeys.all, 'lint', linted] as const,
    queryFn: ({ signal }) => lintDraft(linted, signal),
    placeholderData: keepPreviousData,
    retry: false,
  })
  const issues = lint.data ?? []
  // The text on screen has moved on from what `issues` describes: still debouncing, or the
  // answer for the new text is not in yet and `keepPreviousData` is showing the old one.
  const stale = text !== linted || lint.isPlaceholderData
  const checking = !lint.isError && (stale || lint.data === undefined)
  const partHasError = (part: 'subject' | 'body') =>
    issues.some((issue) => issue.part === part && issue.severity === 'error')
  const subjectIssues = issuesFor(issues, 'subject')
  const bodyIssues = issuesFor(issues, 'body')
  const shownStale = stale && lint.data !== undefined

  const save = useMutation({
    mutationFn: (value: TemplateDraft) =>
      template === null ? createTemplate(value) : updateTemplate(template.id, value),
    onSuccess: (saved, sent) => onSaved(saved, sent),
  })
  // A create, or a save that makes a new version, opens the saved row in a fresh editor,
  // so anything typed while it is out would be lost: hold the fields still until it lands.
  // An in-place save keeps the editor, and an edit typed during it stays in the draft.
  const locked = save.isPending && (template === null || template.in_use)
  const remove = useMutation({
    mutationFn: (id: number) => deleteTemplate(id),
    onSuccess: () => {
      setConfirmDelete(false)
      onDeleted()
    },
  })

  const set = (patch: Partial<TemplateDraft>) => {
    setUndo(null) // an edit ends the chance to undo a paste
    const next = { ...draft, ...patch }
    // A LinkedIn message has no subject: it is dropped, so it is never linted, saved or sent.
    onDraftChange(next.channel === 'linkedin' ? { ...next, subject: '' } : next)
  }

  const fieldOf = (which: InsertTarget) =>
    which === 'subject' ? subjectRef.current : bodyRef.current
  const markFocused = (which: InsertTarget) => {
    focused.current.add(which)
    setTarget(which)
  }

  // Put the cursor after an insert once the new text is on screen.
  useLayoutEffect(() => {
    const pending = caret.current
    if (pending === null) return
    caret.current = null
    const field = pending.target === 'subject' ? subjectRef.current : bodyRef.current
    field?.focus()
    field?.setSelectionRange(pending.at, pending.at)
  }, [draft.subject, draft.body])

  const insert = (expression: string) => {
    const value = draft[target]
    const field = fieldOf(target)
    const known = focused.current.has(target) && field !== null
    const start = known ? (field.selectionStart ?? value.length) : value.length
    const end = known ? (field.selectionEnd ?? start) : value.length
    const token = `{{ ${expression} }}`
    // Through the browser's own editing, so the insert is one step of the field's undo
    // history; the input event it fires reaches onChange like typing does. Setting the
    // value directly would leave undo replaying history that no longer matches the text.
    if (field !== null && !field.readOnly) {
      field.focus()
      field.setSelectionRange(start, end)
      if (insertText(token)) return
    }
    caret.current = { target, at: start + token.length }
    set({ [target]: value.slice(0, start) + token + value.slice(end) })
  }

  const applyPaste = ({ subject, body }: AppliedStep) => {
    const next = {
      ...draft,
      body,
      ...(subject === null || noSubject ? {} : { subject }),
    }
    const overwrites =
      (draft.subject !== '' && next.subject !== draft.subject) ||
      (draft.body !== '' && next.body !== draft.body)
    setUndo(
      overwrites ? { before: { subject: draft.subject, body: draft.body }, after: next } : null,
    )
    setPasted({ channel: next.channel, subject: next.subject, body: next.body })
    onDraftChange(next)
  }

  const undoPaste = () => {
    if (undo === null || !canUndo) return
    const next = { ...draft, ...undo.before }
    setUndo(null)
    setPasted({ channel: next.channel, subject: next.subject, body: next.body })
    onDraftChange(next)
  }

  const goToLine = (line: number) => {
    const field = bodyRef.current
    if (field === null) return
    const [start, end] = lineRange(draft.body, line)
    markFocused('body')
    field.focus()
    field.setSelectionRange(start, end)
  }
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
            <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-1">
              <Label htmlFor={ids.name}>Name</Label>
              <Input
                id={ids.name}
                value={draft.name}
                readOnly={locked}
                required
                onChange={(event) => set({ name: event.target.value })}
              />
            </div>
            <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-1">
              <Label htmlFor={ids.channel}>Channel</Label>
              <Select
                id={ids.channel}
                value={draft.channel}
                disabled={locked}
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
          <AiDraftHelper
            channel={draft.channel}
            current={{ subject: draft.subject, body: draft.body }}
            formId={ids.aiForm}
            disabled={locked}
            onApply={applyPaste}
            canUndo={canUndo}
            onUndo={undoPaste}
          />
          <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-1">
            <Label htmlFor={ids.subject}>Subject</Label>
            <Input
              id={ids.subject}
              ref={subjectRef}
              value={draft.subject}
              readOnly={locked}
              disabled={noSubject}
              aria-invalid={partHasError('subject') || undefined}
              aria-describedby={
                subjectIssues.length > 0 ? `${ids.subjectHint} ${ids.subjectLint}` : ids.subjectHint
              }
              onFocus={() => markFocused('subject')}
              onChange={(event) => set({ subject: event.target.value })}
            />
            <p id={ids.subjectHint} className="text-xs text-muted-foreground">
              {noSubject ? 'LinkedIn messages have no subject.' : 'Required for email.'}
            </p>
            <Findings stale={shownStale}>
              <InlineLint id={ids.subjectLint} label="Subject lint" shown={subjectIssues} />
            </Findings>
          </div>
          <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-1">
            <Label htmlFor={ids.body}>Body</Label>
            <div className="relative rounded-lg bg-background dark:bg-input/30">
              <LineMarks
                text={draft.body}
                marks={shownStale ? new Map() : severityByLine(bodyIssues)}
                scrollTop={bodyScroll}
                className="font-mono"
              />
              <Textarea
                id={ids.body}
                ref={bodyRef}
                value={draft.body}
                readOnly={locked}
                rows={12}
                spellCheck
                className="relative bg-transparent font-mono [scrollbar-gutter:stable] dark:bg-transparent"
                aria-invalid={partHasError('body') || undefined}
                aria-describedby={bodyIssues.length > 0 ? ids.bodyLint : undefined}
                onFocus={() => markFocused('body')}
                onScroll={(event) => setBodyScroll(event.currentTarget.scrollTop)}
                onChange={(event) => set({ body: event.target.value })}
              />
            </div>
            <Findings stale={shownStale}>
              <InlineLint
                id={ids.bodyLint}
                label="Body lint"
                shown={bodyIssues}
                text={draft.body}
                onGoToLine={goToLine}
              />
            </Findings>
          </div>

          <MergeFieldHelper
            contact={sampleContact}
            target={target}
            disabled={locked}
            onInsert={insert}
          />

          <section aria-label="Lint" aria-busy={checking || undefined} className="space-y-2">
            {checking && (
              <p role="status" className="text-xs text-muted-foreground">
                Checking…
              </p>
            )}
            <Findings stale={shownStale}>
              {lint.isError ? (
                <ErrorNote label="Could not lint the template" error={lint.error} />
              ) : lint.data === undefined ? null : issues.length === 0 ? (
                <p role="status" className="text-sm text-muted-foreground">
                  No lint issues.
                </p>
              ) : (
                <p role="status" className="text-sm text-muted-foreground">
                  {issues.length} lint {issues.length === 1 ? 'finding' : 'findings'}, shown under
                  the subject and body.
                </p>
              )}
              {!lint.isError && hasErrors(issues) && (
                <Callout tone="warning">
                  <p>
                    You can save with lint errors, but a campaign can't use this template until
                    they're fixed.
                  </p>
                </Callout>
              )}
            </Findings>
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
        {/* The helper's own form: its fields belong here, so Enter in one never saves. */}
        <form id={ids.aiForm} hidden onSubmit={(event) => event.preventDefault()} />
      </CardContent>
      {template !== null && (
        <ConfirmDialog
          open={confirmDelete}
          onOpenChange={setConfirmDelete}
          title={`Delete “${template.name}”?`}
          confirmLabel="Delete template"
          onConfirm={() => remove.mutateAsync(template.id)}
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

interface AppliedText {
  subject: string
  body: string
}

function sameText(
  a: Pick<TemplateDraft, 'channel' | 'subject' | 'body'>,
  b: Pick<TemplateDraft, 'channel' | 'subject' | 'body'>,
): boolean {
  return a.channel === b.channel && a.subject === b.subject && a.body === b.body
}

/** Lint findings, dimmed and marked while the text has moved on from what they describe. */
function Findings({ stale, children }: { stale: boolean; children: ReactNode }) {
  return (
    <div
      data-stale={stale || undefined}
      className={cn('min-w-0 space-y-2 transition-opacity', stale && 'opacity-50')}
    >
      {children}
    </div>
  )
}

/**
 * Insert `text` at the focused field's selection the way typing would, so it can be
 * undone. False when the browser does not do it (`execCommand` is deprecated, and
 * missing in some environments), and the caller sets the value itself.
 */
function insertText(text: string): boolean {
  try {
    return (
      typeof document.execCommand === 'function' && document.execCommand('insertText', false, text)
    )
  } catch {
    return false
  }
}
