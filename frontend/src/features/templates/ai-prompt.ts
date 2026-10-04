/**
 * The "Draft with your AI assistant" helper's text work (#368): the prompt you
 * copy into an AI chat assistant you already use, and the parser for the reply
 * you paste back.
 *
 * netkeeper never talks to an AI provider. Both functions are pure: the prompt
 * is built from the form, the merge-field list (`GET /templates/merge-fields`),
 * and the lint rules' own sentences (`WHY_IT_MATTERS`), and nothing else. In
 * particular it never sees a contact: the field list it takes has no example
 * values in it, so a contact picked in the preview can't leak into the prompt.
 */
import type { MergeField, TemplateChannel } from './api'
import { WHY_IT_MATTERS, type LintRule } from './lint'

/** Who the assistant should sound like, as the form offers it. */
export const TONES = ['warm', 'friendly and casual', 'professional', 'brief and direct'] as const
export type Tone = (typeof TONES)[number]

/** How many steps the form lets you ask for. A reconnect sequence is two or three. */
export const MAX_STEPS = 5

export interface DraftRequest {
  /** What the campaign is for, in your words. */
  goal: string
  /** Who it's for, described as a group, never as named people. */
  audience: string
  tone: Tone
  steps: number
  /** Anything the messages should mention. */
  mention: string
}

/** What the prompt may know about a merge field: its token and what it means, never a value. */
export type PromptField = Pick<MergeField, 'insert' | 'description'>

/**
 * How long each message should be. Guidance for the assistant, not a lint rule:
 * the reconnect method (docs/networking-workflow.md) asks for a short, warm
 * first message and a shorter follow-up.
 */
export const LENGTH_GUIDANCE: Readonly<Record<TemplateChannel, { first: number; later: number }>> =
  {
    email: { first: 120, later: 80 },
    linkedin: { first: 80, later: 60 },
  }

const CHANNEL_NAMES: Readonly<Record<TemplateChannel, string>> = {
  email: 'email',
  linkedin: 'LinkedIn message',
}

/**
 * Whether lint `rule` applies to `channel`'s templates, so its sentence belongs
 * in that channel's prompt. A rule named `linkedin_*` is a LinkedIn rule (#377);
 * `missing_subject` is about email only; `missing_value` is about one contact's
 * data in the preview, not about the text you draft.
 */
export function ruleApplies(rule: LintRule, channel: TemplateChannel): boolean {
  if (rule === 'missing_value') return false
  if (rule === 'missing_subject') return channel === 'email'
  if (rule.startsWith('linkedin_')) return channel === 'linkedin'
  return true
}

/** The lint sentences for `channel`, in the order lint's metadata lists them. */
export function lintRulesFor(channel: TemplateChannel): string[] {
  return (Object.keys(WHY_IT_MATTERS) as LintRule[])
    .filter((rule) => ruleApplies(rule, channel))
    .map((rule) => WHY_IT_MATTERS[rule])
}

function line(label: string, value: string): string | null {
  const text = value.trim()
  return text === '' ? null : `${label}: ${text}`
}

/** The answer format the prompt asks for, and {@link parseReply} reads. */
export function answerFormat(channel: TemplateChannel, steps: number): string {
  const blocks: string[] = []
  for (let step = 1; step <= Math.max(1, steps); step++) {
    blocks.push(
      [
        `Step ${step}`,
        ...(channel === 'email' ? ['Subject: <one line>'] : []),
        'Body:',
        '<the message, over as many lines as it needs>',
        `End of step ${step}`,
      ].join('\n'),
    )
  }
  return blocks.join('\n\n')
}

/**
 * The prompt to paste into an AI chat assistant.
 *
 * Built from the form, the merge fields, and lint's own rule sentences only.
 * `fields` carries each field's token and description; an example value, even
 * an invented one, never goes in.
 */
export function buildPrompt(
  request: DraftRequest,
  channel: TemplateChannel,
  fields: readonly PromptField[],
): string {
  const steps = Math.min(MAX_STEPS, Math.max(1, Math.round(request.steps)))
  const kind = CHANNEL_NAMES[channel]
  const length = LENGTH_GUIDANCE[channel]
  const sequence =
    steps === 1 ? `one ${kind}` : `a sequence of ${steps} ${kind}s: a first message and follow-ups`

  const about = [
    line('What the campaign is for', request.goal),
    line('Who it is for', request.audience),
    `Tone: ${request.tone}`,
    line('Mention', request.mention),
  ].filter((item): item is string => item !== null)

  const rules = [
    'Plain text only: no HTML, no Markdown, no attachments, no images, no emoji.',
    `Keep it short and personal: at most ${length.first} words for the first message` +
      (steps > 1 ? ` and ${length.later} words for each follow-up.` : '.'),
    ...(channel === 'email'
      ? ['Each email has a short, specific subject line.']
      : ['A LinkedIn message has no subject line. Write only the body.']),
    'Write a placeholder wherever a detail about the recipient or me goes, exactly as shown, like {{ first_name }}. Never write a real name, company, or other personal detail in its place.',
    'Use only the placeholders listed below. Any other {{ name }} is an error.',
    'Every message names at least one placeholder about the recipient, such as {{ first_name }}.',
  ]

  const fieldLines = fields.map((field) => `- {{ ${field.insert} }}: ${field.description}`)

  return [
    `Help me write ${sequence} to reconnect with people in my professional network.`,
    '',
    ...about,
    '',
    'Rules:',
    ...rules.map((rule) => `- ${rule}`),
    '',
    'Placeholders you may use:',
    ...fieldLines,
    '',
    'My app checks every message and refuses one that breaks any of these:',
    ...lintRulesFor(channel).map((rule) => `- ${rule}`),
    '',
    'Answer in exactly this format, with nothing before or after it:',
    '',
    answerFormat(channel, steps),
  ].join('\n')
}

/** One step of a pasted reply. `subject` is null when the reply gave none. */
export interface ParsedStep {
  subject: string | null
  body: string
}

const STEP_MARK = /^[\s#>*_]*step\s+(\d+)\s*[*_]*\s*[:.)-]?\s*[*_]*\s*$/i
const END_MARK = /^[\s*_]*end\s+of\s+step\s+\d+[\s*_.]*$/i
const LABEL = /^[\s>*_]*(subject|body)\s*[*_]*\s*:\s*[*_]*\s?(.*)$/i
const FENCE = /^\s*```/

/**
 * Split an assistant's reply into steps, from the format {@link answerFormat}
 * asks for. Tolerates chatter before the first step, Markdown fences and bold
 * labels, and a missing "End of step" line. A reply with no `Body:` label in it
 * is not that format, and gives null: the caller pastes it into the body as it is.
 */
export function parseReply(reply: string): ParsedStep[] | null {
  const lines = reply.split(/\r\n|\r|\n/).filter((text) => !FENCE.test(text))
  const steps: ParsedStep[] = []
  let subject: string | null = null
  let body: string[] | null = null

  const finish = () => {
    if (body !== null) {
      steps.push({ subject, body: trimBlankLines(body).join('\n') })
    }
    subject = null
    body = null
  }

  for (const text of lines) {
    if (STEP_MARK.test(text)) {
      finish()
      continue
    }
    if (END_MARK.test(text)) {
      finish()
      continue
    }
    const match = LABEL.exec(text)
    const label = match === null ? null : (match[1] ?? '').toLowerCase()
    const rest = match?.[2] ?? ''
    if (body === null) {
      if (label === 'subject') subject = rest.trim()
      else if (label === 'body') body = rest.trim() === '' ? [] : [rest]
      continue
    }
    // Inside a body: a new subject label means the next step began without a marker.
    if (label === 'subject') {
      finish()
      subject = rest.trim()
      continue
    }
    body.push(text)
  }
  finish()
  return steps.length === 0 ? null : steps
}

function trimBlankLines(lines: string[]): string[] {
  let start = 0
  let end = lines.length
  const blank = (at: number) => (lines[at] ?? '').trim() === ''
  while (start < end && blank(start)) start++
  while (end > start && blank(end - 1)) end--
  return lines.slice(start, end).map((text) => text.replace(/\s+$/, ''))
}

/** One step written back out in the reply format, to copy into another template's helper. */
export function formatStep(step: ParsedStep, number: number): string {
  return [
    `Step ${number}`,
    ...(step.subject === null ? [] : [`Subject: ${step.subject}`]),
    'Body:',
    step.body,
    `End of step ${number}`,
  ].join('\n')
}
