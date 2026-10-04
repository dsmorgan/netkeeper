/**
 * The "Draft with your AI assistant" helper's text work (#368): the prompt you
 * copy into an AI chat assistant you already use, and the parser for the reply
 * you paste back.
 *
 * netkeeper never talks to an AI provider. Both functions are pure: the prompt
 * is built from the form, the merge-field list (`GET /templates/merge-fields`),
 * lint's rules as instructions (`PROMPT_RULES`, keyed by rule id next to
 * `WHY_IT_MATTERS`), and, only when you tick the box, the template's current
 * text. It never sees a contact: the field list it takes has no example values
 * in it, so a contact picked in the preview can't leak into the prompt.
 */
import type { MergeField, TemplateChannel } from './api'
import { PROMPT_RULES, type LintRule } from './lint'

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

/** The template's text as it stands, for a prompt that asks to improve it. */
export interface CurrentText {
  subject: string
  body: string
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
 * `missing_subject` is about email only; a rule with no prompt sentence (like
 * `missing_value`, about one contact's data in the preview) is in no prompt.
 */
export function ruleApplies(rule: LintRule, channel: TemplateChannel): boolean {
  if (PROMPT_RULES[rule] === null) return false
  if (rule === 'missing_subject') return channel === 'email'
  if (rule.startsWith('linkedin_')) return channel === 'linkedin'
  return true
}

/** The lint rules for `channel`, as instructions, in the order lint's metadata lists them. */
export function lintRulesFor(channel: TemplateChannel): string[] {
  return (Object.keys(PROMPT_RULES) as LintRule[])
    .filter((rule) => ruleApplies(rule, channel))
    .map((rule) => PROMPT_RULES[rule])
    .filter((rule): rule is string => rule !== null)
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
 * Built from the form, the merge fields, and lint's rules only, plus `current`,
 * the template's own text, when you chose to include it. `fields` carries each
 * field's token and description; an example value, even an invented one, never
 * goes in.
 */
export function buildPrompt(
  request: DraftRequest,
  channel: TemplateChannel,
  fields: readonly PromptField[],
  current: CurrentText | null = null,
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
  ]

  const improve =
    current === null || (current.subject.trim() === '' && current.body.trim() === '')
      ? []
      : [
          'Current template to improve:',
          '',
          ...(channel === 'email' ? [`Subject: ${current.subject}`] : []),
          'Body:',
          current.body,
          '',
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
    'My app checks every message, and refuses one that breaks any of these rules:',
    ...lintRulesFor(channel).map((rule) => `- ${rule}`),
    '',
    ...improve,
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

// Each pattern is linear: no two adjacent quantifiers can match the same characters,
// so a long line of spaces can't make them backtrack. Markers are short, so a long
// line is never tested against the marker patterns at all.
const STEP_MARK = /^[\s#>*_]*step\s+(\d+)[\s*_:.)-]*$/i
const END_MARK = /^[\s*_]*end\s+of\s+step\s+\d+[\s*_.]*$/i
const LABEL = /^[\s>*_]*(subject|body)[\s*_]*:[\s*_]*(.*)$/i
const FENCE = /^\s*```/
// A short header an assistant puts above a block in place of `Step N`: "Email 2",
// "**Message 2:**", "Follow-up 1 (a week later)".
const HEADER = /^[\s#>*_]*(?:email|message|follow[- ]?up|step)[\s#]*\d+\b.{0,60}$/i

/** The longest line tested as a `Step N` or `End of step N` marker. */
export const MARKER_MAX_CHARS = 200

const isStepMark = (text: string) => text.length <= MARKER_MAX_CHARS && STEP_MARK.test(text)
const isEndMark = (text: string) => text.length <= MARKER_MAX_CHARS && END_MARK.test(text)
const isHeader = (text: string) => text.length <= MARKER_MAX_CHARS && HEADER.test(text)

/** A parsed reply: its steps, and how many non-blank lines outside the format were left out. */
export interface ParsedReply {
  steps: ParsedStep[]
  dropped: number
}

/**
 * Split an assistant's reply into steps, from the format {@link answerFormat}
 * asks for. Tolerates chatter around the format, Markdown fences and bold
 * labels, and a missing "End of step" line.
 *
 * When the reply uses "End of step" markers, only such a marker ends a body, so a
 * `Subject:` or `Step N` line inside one stays in it. Without them, a `Subject:`
 * or bare `Step N` line starts the next step only after a blank line, or a
 * `Subject:` line right after a header like "Email 2" (the header is dropped);
 * so a body that mentions one of them keeps it. Every non-blank line outside a step's
 * labels is counted in `dropped`, so the caller can say what it left out. A reply
 * with no `Body:` label is not the format, and gives null: the caller pastes it
 * into the body as it is.
 */
export function parseReply(reply: string): ParsedReply | null {
  const lines = reply.split(/\r\n|\r|\n/)
  const ended = lines.some(isEndMark)
  const steps: ParsedStep[] = []
  let dropped = 0
  let subject: string | null = null
  let body: string[] | null = null
  let afterBlank = true

  const finish = () => {
    if (body !== null) steps.push({ subject, body: trimBlankLines(body).join('\n') })
    else if (subject !== null) dropped++ // a subject with no body after it
    subject = null
    body = null
  }

  for (const text of lines) {
    if (FENCE.test(text)) continue
    const blank = text.trim() === ''
    const match = LABEL.exec(text)
    const label = match === null ? null : (match[1] ?? '').toLowerCase()
    const rest = match?.[2] ?? ''
    if (body === null) {
      if (isStepMark(text) || isEndMark(text)) finish()
      else if (label === 'subject') {
        if (subject !== null) dropped++
        subject = rest.trim()
      } else if (label === 'body') body = rest.trim() === '' ? [] : [rest]
      else if (!blank) dropped++
    } else {
      const boundary = afterBlank && !ended
      const afterHeader = !ended && body.length > 0 && isHeader(body[body.length - 1] ?? '')
      if (isEndMark(text) || (boundary && isStepMark(text))) finish()
      else if (label === 'subject' && (boundary || afterHeader)) {
        if (afterHeader) {
          body.pop()
          dropped++
        }
        finish()
        subject = rest.trim()
      } else body.push(text)
    }
    afterBlank = blank
  }
  finish()
  return steps.length === 0 ? null : { steps, dropped }
}

function trimBlankLines(lines: string[]): string[] {
  let start = 0
  let end = lines.length
  const blank = (at: number) => (lines[at] ?? '').trim() === ''
  while (start < end && blank(start)) start++
  while (end > start && blank(end - 1)) end--
  return lines.slice(start, end).map((text) => text.trimEnd())
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
