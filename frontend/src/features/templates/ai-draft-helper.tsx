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
 * template's helper.
 */
import { useQuery } from '@tanstack/react-query'
import { useEffect, useId, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
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
  type DraftRequest,
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
  disabled?: boolean
  onApply: (step: AppliedStep) => void
}

type CopyState =
  | { kind: 'idle' }
  | { kind: 'copied' }
  /** The clipboard was missing or refused: the prompt is shown, selected, to copy by hand. */
  | { kind: 'manual'; prompt: string }

type PasteNote =
  { kind: 'parsed'; count: number; droppedSubject: boolean } | { kind: 'raw' } | { kind: 'empty' }

export function AiDraftHelper({ channel, disabled, onApply }: AiDraftHelperProps) {
  const ids = {
    heading: useId(),
    panel: useId(),
    goal: useId(),
    audience: useId(),
    tone: useId(),
    steps: useId(),
    mention: useId(),
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
  const [copy, setCopy] = useState<CopyState>({ kind: 'idle' })
  const [reply, setReply] = useState('')
  const [note, setNote] = useState<PasteNote | null>(null)
  const [others, setOthers] = useState<ParsedStep[]>([])
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
    )
    try {
      if (typeof navigator.clipboard?.writeText !== 'function') throw new Error('no clipboard')
      await navigator.clipboard.writeText(prompt)
      setCopy({ kind: 'copied' })
    } catch {
      setCopy({ kind: 'manual', prompt })
    }
  }

  const apply = (step: ParsedStep) => {
    const keepsSubject = channel !== 'email'
    onApply({ subject: keepsSubject ? null : (step.subject ?? ''), body: step.body })
    return keepsSubject && step.subject !== null && step.subject !== ''
  }

  const paste = () => {
    if (reply.trim() === '') {
      setNote({ kind: 'empty' })
      return
    }
    const steps = parseReply(reply)
    const first = steps?.[0]
    if (steps === null || first === undefined) {
      onApply({ subject: null, body: reply.trim() })
      setOthers([])
      setNote({ kind: 'raw' })
      return
    }
    const droppedSubject = apply(first)
    setOthers(steps.slice(1))
    setNote({ kind: 'parsed', count: steps.length, droppedSubject })
  }

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
            goes to that provider under its terms. Never paste contacts' names or details into it:
            the prompt uses placeholders like {'{{ first_name }}'} instead.{' '}
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
                value={request.goal}
                placeholder="Reconnect and say I'm looking for a new role"
                onChange={(event) => set({ goal: event.target.value })}
              />
            </div>
            <div className="grid min-w-0 gap-1">
              <Label htmlFor={ids.audience}>Who it's for</Label>
              <Input
                id={ids.audience}
                value={request.audience}
                placeholder="Former colleagues in engineering"
                onChange={(event) => set({ audience: event.target.value })}
              />
            </div>
            <div className="grid min-w-0 gap-1">
              <Label htmlFor={ids.tone}>Tone</Label>
              <Select
                id={ids.tone}
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
              value={request.mention}
              rows={2}
              onChange={(event) => set({ mention: event.target.value })}
            />
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
              value={reply}
              rows={6}
              className="font-mono text-xs"
              onChange={(event) => {
                setReply(event.target.value)
                setNote(null)
              }}
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" variant="outline" disabled={disabled} onClick={paste}>
              Paste result
            </Button>
            <p role="status" className="text-xs text-muted-foreground">
              {note === null ? '' : noteText(note, channel)}
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
          {others.length > 0 && (
            <OtherSteps steps={others} disabled={disabled} onUse={(step) => apply(step)} />
          )}
        </div>
      )}
    </section>
  )
}

function noteText(note: PasteNote, channel: TemplateChannel): string {
  if (note.kind === 'empty') return 'Paste the reply above first.'
  if (note.kind === 'raw') return 'Pasted the reply into the body as it is.'
  const filled = channel === 'email' ? 'the subject and body' : 'the body'
  const steps =
    note.count === 1 ? `Filled ${filled}.` : `Filled ${filled} from step 1 of ${note.count}.`
  return note.droppedSubject
    ? `${steps} A LinkedIn message has no subject, so the reply's subject was left out.`
    : steps
}

/** The steps after the first: one template each, so they are offered here to use or copy. */
function OtherSteps({
  steps,
  disabled,
  onUse,
}: {
  steps: readonly ParsedStep[]
  disabled?: boolean
  onUse: (step: ParsedStep) => void
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
        paste the step into its helper.
      </p>
      <ol className="space-y-2">
        {steps.map((step, index) => {
          const number = index + 2
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
                  onClick={() => onUse(step)}
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
