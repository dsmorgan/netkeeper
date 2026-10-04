/**
 * "Draft with your AI assistant" (#368): a prompt to copy into an AI chat
 * assistant you already use, and a place to paste its reply back.
 *
 * netkeeper makes no call to any AI provider and stores no key for one. You
 * carry the text both ways by copy and paste. The prompt is built from the form,
 * the merge-field list (`GET /templates/merge-fields`, asked without a contact),
 * and lint's own rule sentences, so it never holds a contact's data, even when
 * one is picked in the preview.
 *
 * A template holds one step. A reply with several steps fills this template
 * from the first, and lists the others to use here or copy into another
 * template's helper. A paste that would replace text you had offers to undo it.
 *
 * The helper sits inside the editor's form, so its fields belong to a form of
 * their own (`formId`, rendered by the editor outside its form): Enter in one of
 * them never saves the template.
 */
import { useQuery } from '@tanstack/react-query'
import { useEffect, useId, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input, Textarea } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Callout, ErrorNote, LoadingNote } from '@/features/crm/controls'

import { mergeFieldsQuery } from './api'
import type { TemplateChannel } from './api'
import {
  MAX_STEPS,
  TONES,
  buildPrompt,
  formatStep,
  parseReply,
  type CurrentText,
  type DraftRequest,
  type ParsedReply,
  type ParsedStep,
  type Tone,
} from './ai-prompt'

/** The guide, on the project's site, since the app serves no docs of its own. */
export const AI_DRAFTING_GUIDE_URL =
  'https://github.com/dsmorgan/netkeeper/blob/main/docs/ai-drafting.md'

/** What a paste put into the editor, so the editor can say so and lint at once. */
export interface AppliedStep {
  /** Null leaves the subject as it is. */
  subject: string | null
  body: string
}

interface AiDraftHelperProps {
  channel: TemplateChannel
  /** The template's text now, put in the prompt only when you tick the box. */
  current: CurrentText
  /** The id of the empty form the helper's fields belong to, outside the editor's form. */
  formId: string
  disabled?: boolean
  onApply: (step: AppliedStep) => void
  /** Whether the last paste replaced text and can still be undone. */
  canUndo: boolean
  onUndo: () => void
}

type CopyState =
  | { kind: 'idle' }
  | { kind: 'copied' }
  /** The clipboard was missing or refused: the prompt is shown, selected, to copy by hand. */
  | { kind: 'manual'; prompt: string }

type PasteNote =
  | {
      kind: 'parsed'
      /** One-based: the step now in the editor. */
      step: number
      count: number
      dropped: number
      filledSubject: boolean
      droppedSubject: boolean
    }
  | { kind: 'raw' }
  | { kind: 'empty' }

export function AiDraftHelper({
  channel,
  current,
  formId,
  disabled,
  onApply,
  canUndo,
  onUndo,
}: AiDraftHelperProps) {
  const ids = {
    heading: useId(),
    panel: useId(),
    goal: useId(),
    audience: useId(),
    tone: useId(),
    steps: useId(),
    mention: useId(),
    include: useId(),
    includeHint: useId(),
    manual: useId(),
    reply: useId(),
  }
  const [open, setOpen] = useState(false)
  const [request, setRequest] = useState<DraftRequest>({
    goal: '',
    audience: '',
    tone: 'warm',
    steps: 1,
    mention: '',
  })
  const [includeCurrent, setIncludeCurrent] = useState(false)
  const [copy, setCopy] = useState<CopyState>({ kind: 'idle' })
  const [reply, setReply] = useState('')
  const [note, setNote] = useState<PasteNote | null>(null)
  const [parsed, setParsed] = useState<ParsedReply | null>(null)
  const manualRef = useRef<HTMLTextAreaElement>(null)
  // Never with a contact: the prompt must not hold anyone's data.
  const fields = useQuery({ ...mergeFieldsQuery(null), enabled: open })

  useEffect(() => {
    if (copy.kind !== 'manual') return
    manualRef.current?.focus()
    manualRef.current?.select()
  }, [copy])

  const set = (patch: Partial<DraftRequest>) => {
    setRequest((now) => ({ ...now, ...patch }))
    setCopy({ kind: 'idle' })
  }

  const copyPrompt = async () => {
    if (fields.data === undefined) return
    const prompt = buildPrompt(
      request,
      channel,
      fields.data.fields.map(({ insert, description }) => ({ insert, description })),
      includeCurrent ? current : null,
    )
    try {
      if (typeof navigator.clipboard?.writeText !== 'function') throw new Error('no clipboard')
      await navigator.clipboard.writeText(prompt)
      setCopy({ kind: 'copied' })
    } catch {
      setCopy({ kind: 'manual', prompt })
    }
  }

  /** Put step `index` of `reply` into the editor, and say so. */
  const fillFrom = (from: ParsedReply, index: number) => {
    const step = from.steps[index]
    if (step === undefined) return
    const email = channel === 'email'
    // An email step with no subject leaves the subject as it is; a LinkedIn message has none.
    onApply({ subject: email ? step.subject : null, body: step.body })
    setNote({
      kind: 'parsed',
      step: index + 1,
      count: from.steps.length,
      dropped: from.dropped,
      filledSubject: email && step.subject !== null,
      droppedSubject: !email && step.subject !== null && step.subject !== '',
    })
  }

  const paste = () => {
    if (reply.trim() === '') {
      setNote({ kind: 'empty' })
      return
    }
    const result = parseReply(reply)
    setParsed(result)
    if (result === null) {
      onApply({ subject: null, body: reply.trim() })
      setNote({ kind: 'raw' })
      return
    }
    fillFrom(result, 0)
  }

  const inUse = note?.kind === 'parsed' ? note.step - 1 : 0

  return (
    <section aria-labelledby={ids.heading} className="space-y-2 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <h4 id={ids.heading} className="text-sm font-medium">
          Draft with your AI assistant
        </h4>
        <Button
          type="button"
          size="xs"
          variant="outline"
          className="ml-auto"
          aria-expanded={open}
          aria-controls={ids.panel}
          onClick={() => setOpen((now) => !now)}
        >
          {open ? 'Hide' : 'Show'}
        </Button>
      </div>
      {open && (
        <div id={ids.panel} className="space-y-3">
          <p className="text-xs text-muted-foreground">
            Describe the campaign, copy the prompt into an AI chat assistant you already use, then
            paste its reply here. netkeeper sends nothing to any AI service; what you paste into one
            goes to that provider under its terms. What you type in this form goes into the prompt
            word for word, so never type contacts' names or details: the prompt uses placeholders
            like {'{{ first_name }}'} instead.{' '}
            <a
              href={AI_DRAFTING_GUIDE_URL}
              target="_blank"
              rel="noreferrer"
              className="underline underline-offset-2"
            >
              Read the guide
            </a>
            .
          </p>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="grid min-w-0 gap-1">
              <Label htmlFor={ids.goal}>What the campaign is for</Label>
              <Input
                id={ids.goal}
                form={formId}
                value={request.goal}
                placeholder="Reconnect and say I'm looking for a new role"
                onChange={(event) => set({ goal: event.target.value })}
              />
            </div>
            <div className="grid min-w-0 gap-1">
              <Label htmlFor={ids.audience}>Who it's for</Label>
              <Input
                id={ids.audience}
                form={formId}
                value={request.audience}
                placeholder="Former colleagues in engineering"
                onChange={(event) => set({ audience: event.target.value })}
              />
            </div>
            <div className="grid min-w-0 gap-1">
              <Label htmlFor={ids.tone}>Tone</Label>
              <Select
                id={ids.tone}
                form={formId}
                value={request.tone}
                onChange={(event) => set({ tone: event.target.value as Tone })}
              >
                {TONES.map((tone) => (
                  <option key={tone} value={tone}>
                    {tone.charAt(0).toUpperCase() + tone.slice(1)}
                  </option>
                ))}
              </Select>
            </div>
            <div className="grid min-w-0 gap-1">
              <Label htmlFor={ids.steps}>Steps</Label>
              <Select
                id={ids.steps}
                form={formId}
                value={String(request.steps)}
                onChange={(event) => set({ steps: Number(event.target.value) })}
              >
                {Array.from({ length: MAX_STEPS }, (_, index) => index + 1).map((count) => (
                  <option key={count} value={count}>
                    {count}
                  </option>
                ))}
              </Select>
            </div>
          </div>
          <div className="grid min-w-0 gap-1">
            <Label htmlFor={ids.mention}>Anything to mention</Label>
            <Textarea
              id={ids.mention}
              form={formId}
              value={request.mention}
              rows={2}
              onChange={(event) => set({ mention: event.target.value })}
            />
          </div>

          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <Checkbox
                id={ids.include}
                checked={includeCurrent}
                aria-describedby={ids.includeHint}
                onCheckedChange={(checked) => {
                  setIncludeCurrent(checked === true)
                  setCopy({ kind: 'idle' })
                }}
              />
              <Label htmlFor={ids.include}>Include the current text</Label>
            </div>
            <p id={ids.includeHint} className="text-xs text-muted-foreground">
              Adds this template's subject and body to the prompt, to improve them. They hold only
              merge-field placeholders unless you typed real names or details into them, so check
              first.
            </p>
          </div>

          {fields.isPending && <LoadingNote label="Loading merge fields…" />}
          {fields.isError && (
            <ErrorNote label="Could not load the merge fields" error={fields.error} />
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              disabled={fields.data === undefined}
              onClick={() => void copyPrompt()}
            >
              Copy prompt
            </Button>
            <p role="status" className="text-xs text-muted-foreground">
              {copy.kind === 'copied'
                ? 'Copied. Paste it into your AI chat assistant.'
                : copy.kind === 'manual'
                  ? "Your browser didn't allow copying. The prompt is selected below: copy it with Cmd+C or Ctrl+C."
                  : ''}
            </p>
          </div>
          {copy.kind === 'manual' && (
            <div className="grid min-w-0 gap-1">
              <Label htmlFor={ids.manual}>Prompt</Label>
              <Textarea
                id={ids.manual}
                ref={manualRef}
                form={formId}
                readOnly
                rows={8}
                value={copy.prompt}
                className="font-mono text-xs"
                onFocus={(event) => event.currentTarget.select()}
              />
            </div>
          )}

          <div className="grid min-w-0 gap-1">
            <Label htmlFor={ids.reply}>Assistant's reply</Label>
            <Textarea
              id={ids.reply}
              form={formId}
              value={reply}
              rows={6}
              className="font-mono text-xs"
              onChange={(event) => {
                setReply(event.target.value)
                setNote(null)
                setParsed(null)
              }}
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" variant="outline" disabled={disabled} onClick={paste}>
              Paste result
            </Button>
            {canUndo && (
              <Button type="button" variant="outline" disabled={disabled} onClick={onUndo}>
                Undo paste
              </Button>
            )}
            <p role="status" className="text-xs text-muted-foreground">
              {note === null ? '' : noteText(note)}
            </p>
          </div>
          {note?.kind === 'raw' && (
            <Callout tone="warning">
              <p>
                The reply didn't have Subject: and Body: labels, so all of it went into the body.
                Edit it there, and check the lint below.
              </p>
            </Callout>
          )}
          {parsed !== null && parsed.steps.length > 1 && (
            <OtherSteps
              steps={parsed.steps}
              inUse={inUse}
              disabled={disabled}
              onUse={(index) => fillFrom(parsed, index)}
            />
          )}
        </div>
      )}
    </section>
  )
}

function noteText(note: PasteNote): string {
  if (note.kind === 'empty') return 'Paste the reply above first.'
  if (note.kind === 'raw') return 'Pasted the reply into the body as it is.'
  const filled = note.filledSubject ? 'the subject and body' : 'the body'
  const parts = [
    note.count === 1
      ? `Filled ${filled}.`
      : `Filled ${filled} from step ${note.step} of ${note.count}.`,
  ]
  if (note.droppedSubject) {
    parts.push("A LinkedIn message has no subject, so the reply's subject was left out.")
  }
  if (note.dropped > 0) {
    parts.push(
      note.dropped === 1
        ? '1 line outside the labeled format was left out.'
        : `${note.dropped} lines outside the labeled format were left out.`,
    )
  }
  return parts.join(' ')
}

/** Every step but the one in the editor: one template each, to use here or copy. */
function OtherSteps({
  steps,
  inUse,
  disabled,
  onUse,
}: {
  steps: readonly ParsedStep[]
  /** The zero-based step now in the editor, left out of the list. */
  inUse: number
  disabled?: boolean
  onUse: (index: number) => void
}) {
  const headingId = useId()
  const [copied, setCopied] = useState<number | null>(null)
  const copyStep = async (step: ParsedStep, number: number) => {
    try {
      await navigator.clipboard.writeText(formatStep(step, number))
      setCopied(number)
    } catch {
      setCopied(null)
    }
  }
  return (
    <section aria-labelledby={headingId} className="space-y-2">
      <h5 id={headingId} className="text-xs font-medium">
        Other steps
      </h5>
      <p className="text-xs text-muted-foreground">
        A template holds one step. Save this one, then start a new template for each step below and
        paste the step into its helper, or use a step here instead.
      </p>
      <ol className="space-y-2">
        {steps.map((step, index) => {
          if (index === inUse) return null
          const number = index + 1
          return (
            <li key={number} className="space-y-1 rounded-md border p-2">
              <p className="text-xs font-medium">Step {number}</p>
              {step.subject !== null && <p className="text-xs">Subject: {step.subject}</p>}
              <pre className="font-mono text-xs break-words whitespace-pre-wrap">{step.body}</pre>
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  size="xs"
                  variant="outline"
                  disabled={disabled}
                  onClick={() => onUse(index)}
                >
                  Use step {number} here
                </Button>
                <Button
                  type="button"
                  size="xs"
                  variant="outline"
                  onClick={() => void copyStep(step, number)}
                >
                  {copied === number ? 'Copied' : `Copy step ${number}`}
                </Button>
              </div>
            </li>
          )
        })}
      </ol>
    </section>
  )
}
