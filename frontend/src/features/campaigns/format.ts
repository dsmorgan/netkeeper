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
  sample_previews: 'Sampled previews approved',
  searched_previews: 'Searched previews approved',
  test_sends: 'Test send of each email step',
  lint: 'Lint clean',
  guards: 'Guard summary acknowledged',
}

/** The checklist's order: every requirement the gate knows, whether or not it is missing. */
export const REQUIREMENTS = [
  'reviewing',
  'audience',
  'sample_previews',
  'searched_previews',
  'test_sends',
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
