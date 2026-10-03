import type { CampaignStatus, EnrollmentStatus, Missing, StepCondition, StepMode } from './api'

export const CAMPAIGN_STATUS_LABELS: Record<CampaignStatus, string> = {
  draft: 'Draft',
  reviewing: 'Reviewing',
  active: 'Active',
  paused: 'Paused',
  completed: 'Completed',
  archived: 'Archived',
}

export const ENROLLMENT_STATUSES: readonly EnrollmentStatus[] = [
  'pending',
  'active',
  'paused',
  'replied',
  'completed',
  'bounced',
  'opted_out',
  'removed',
]

export const ENROLLMENT_STATUS_LABELS: Record<EnrollmentStatus, string> = {
  pending: 'Pending',
  active: 'Active',
  paused: 'Paused',
  replied: 'Replied',
  completed: 'Completed',
  bounced: 'Bounced',
  opted_out: 'Opted out',
  removed: 'Removed',
}

export const MODE_LABELS: Record<StepMode, string> = {
  draft: 'Draft (you send it)',
  send: 'Send',
  prefill: 'Prefill (you send it)',
  auto_send: 'Auto-send',
}

export const CONDITION_LABELS: Record<StepCondition, string> = {
  always: 'Always',
  no_reply: 'Only if no reply',
}

/** Each gate requirement's name as the checklist shows it (spec 11.8). */
export const REQUIREMENT_LABELS: Record<string, string> = {
  reviewing: 'Review started',
  audience: 'Audience enrolled',
  step_approvals: 'Each step approved',
  message_approvals: 'Personal-line messages approved one by one',
  test_sends: 'Test send of each email step',
  mailbox: 'Mailbox ok',
  lint: 'Lint clean',
  guards: 'Guard summary acknowledged',
}

/** The checklist's order: every requirement the gate knows, whether or not it is missing. */
export const REQUIREMENTS = [
  'reviewing',
  'audience',
  'step_approvals',
  'message_approvals',
  'test_sends',
  'mailbox',
  'lint',
  'guards',
] as const

/** One `missing` entry as a sentence, with the steps or enrollments it names. */
export function missingText(entry: Missing): string {
  const parts = [entry.detail]
  const steps = entry.step_positions ?? []
  const enrollments = entry.enrollment_ids ?? []
  if (steps.length > 0) parts.push(`steps ${steps.join(', ')}`)
  if (enrollments.length > 0) {
    parts.push(`${enrollments.length} ${enrollments.length === 1 ? 'enrollment' : 'enrollments'}`)
  }
  return parts.join(': ')
}

/** A timestamp in the reader's own zone; a dash for none. */
export function formatWhen(iso: string | null | undefined): string {
  if (iso === null || iso === undefined) return '—'
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) return iso
  return when.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

/** Enrollment counts as "3 pending, 1 replied"; "nobody enrolled" for none. */
export function countsText(counts: Partial<Record<EnrollmentStatus, number>>): string {
  const parts = ENROLLMENT_STATUSES.filter((s) => (counts[s] ?? 0) > 0).map(
    (s) => `${counts[s]} ${ENROLLMENT_STATUS_LABELS[s].toLowerCase()}`,
  )
  return parts.length === 0 ? 'nobody enrolled' : parts.join(', ')
}

/**
 * A scheduled start as the campaign page says it, "Tue Oct 6, 09:00", in the reader's
 * own zone (#338). A dash for none.
 */
export function formatStart(iso: string | null | undefined): string {
  if (iso === null || iso === undefined) return '—'
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) return iso
  const weekday = when.toLocaleDateString('en-US', { weekday: 'short' })
  const date = when.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  const time = when.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
  return `${weekday} ${date}, ${time}`
}

/** An ISO time as a `datetime-local` input's value, in the reader's own zone. */
export function toLocalInput(iso: string): string {
  const when = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}` +
    `T${pad(when.getHours())}:${pad(when.getMinutes())}`
  )
}

/** A `datetime-local` input's value as an ISO time with its zone; null when it is not one. */
export function fromLocalInput(value: string): string | null {
  if (value === '') return null
  const when = new Date(value)
  return Number.isNaN(when.getTime()) ? null : when.toISOString()
}

/** A step's timing as the steps table says it (#338). */
export function timingText(step: {
  position: number
  delay_days: number
  send_time?: string | null
}): string {
  const time = step.send_time ?? null
  if (step.position === 1 && step.delay_days === 0) {
    return time === null ? 'at the start' : `at the start, not before ${time}`
  }
  const after = step.position === 1 ? 'after the start' : 'after the step before'
  const days = `${step.delay_days} ${step.delay_days === 1 ? 'day' : 'days'} ${after}`
  return time === null ? `${days}, next suggested slot` : `${days}, at ${time}`
}
