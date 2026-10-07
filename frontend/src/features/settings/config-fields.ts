/**
 * Draft values for the settings you change here instead of in config.toml (#343), and
 * the checks the page runs before it saves. The backend checks again: these only spare
 * a round trip, and the hard maximums come from the backend's own field list.
 */
import type { ConfigField } from './api'

/** A field's draft: text for numbers and dates, a pair for a window, a flag for a switch. */
export type Draft = string | boolean | [string, string]

const CLOCK = /^([01][0-9]|2[0-3]):[0-5][0-9]$/
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

export const GROUPS: { id: ConfigField['group']; title: string; description: string }[] = [
  {
    id: 'linkedin_budgets',
    title: 'LinkedIn budgets',
    description:
      'How much netkeeper may do on LinkedIn each day. Each budget has a hard maximum this page never goes past.',
  },
  {
    id: 'linkedin_hours',
    title: 'LinkedIn active hours',
    description: 'When netkeeper may use LinkedIn, and how much less it does on weekends.',
  },
  {
    id: 'campaigns',
    title: 'Campaign defaults',
    description: 'Caps and spacing for campaign email, and what new campaigns start with.',
  },
  { id: 'llm', title: 'LLM', description: 'Whether netkeeper may call the LLM.' },
  { id: 'backup', title: 'Backups', description: 'How many database backups to keep.' },
]

/** The draft a field's value in force starts as. */
export function draftOf(field: ConfigField): Draft {
  const value = field.value
  switch (field.kind) {
    case 'bool':
      return value === true
    case 'window': {
      const pair = Array.isArray(value) ? value : []
      return [String(pair[0] ?? ''), String(pair[1] ?? '')]
    }
    case 'dates':
      return Array.isArray(value) ? value.map(String).join('\n') : ''
    default:
      return value === null || value === undefined ? '' : String(value)
  }
}

/** The JSON a draft saves as; `null` for an empty automatic value. Assumes no problem. */
export function valueOf(field: ConfigField, draft: Draft): unknown {
  switch (field.kind) {
    case 'bool':
      return draft === true
    case 'window':
      return draft
    case 'dates':
      return datesOf(draft as string)
    case 'optional_int':
      return (draft as string).trim() === '' ? null : Number(draft)
    default:
      return Number(draft)
  }
}

function datesOf(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((d) => d.trim())
    .filter((d) => d !== '')
}

/** What is wrong with a draft, in a sentence, or null. */
export function draftProblem(field: ConfigField, draft: Draft): string | null {
  if (field.kind === 'bool') return null
  if (field.kind === 'window') {
    const [start, end] = draft as [string, string]
    if (!CLOCK.test(start) || !CLOCK.test(end)) return 'Enter both times as HH:MM.'
    if (start === end) return 'The start and end must differ.'
    return null
  }
  if (field.kind === 'dates') {
    const bad = datesOf(draft as string).find((d) => !ISO_DATE.test(d))
    return bad === undefined ? null : `${bad} is not a date as YYYY-MM-DD.`
  }
  const text = (draft as string).trim()
  if (text === '' && field.kind === 'optional_int') return null
  const number = Number(text)
  if (text === '' || !Number.isFinite(number)) return 'Enter a number.'
  if (field.kind !== 'float' && !Number.isInteger(number)) return 'Enter a whole number.'
  if (field.minimum !== null && number < field.minimum) return `The least is ${field.minimum}.`
  if (field.maximum !== null && number > field.maximum) {
    return `The hard maximum is ${field.maximum}.`
  }
  return null
}

/** Said before saving a number above the level that earns a warning, or null. */
export function warnAboveHint(field: ConfigField, draft: Draft): string | null {
  if (field.warn_above === null || typeof draft !== 'string') return null
  const number = Number(draft)
  if (draft.trim() === '' || !Number.isFinite(number) || number <= field.warn_above) return null
  return `Above ${field.warn_above} a day, LinkedIn is more likely to restrict your account or ask you to verify it. You can save it; the warning stays on this page and in the posture report.`
}

/** True when a draft differs from the value in force. */
export function changed(field: ConfigField, draft: Draft): boolean {
  return JSON.stringify(valueOf(field, draft)) !== JSON.stringify(field.value)
}
