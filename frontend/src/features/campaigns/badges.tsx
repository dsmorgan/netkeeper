import { cn } from '@/lib/utils'

import type { CampaignStatus, EnrollmentStatus } from './api'
import { CAMPAIGN_STATUS_LABELS, ENROLLMENT_STATUS_LABELS } from './format'

const PILL =
  'inline-flex h-5 shrink-0 items-center rounded-4xl px-2 text-xs font-medium whitespace-nowrap'

const CAMPAIGN_CLASSES: Record<CampaignStatus, string> = {
  draft: 'bg-muted text-muted-foreground',
  reviewing: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
  active: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
  paused: 'bg-sky-500/15 text-sky-700 dark:text-sky-300',
  completed: 'bg-muted text-foreground',
  archived: 'bg-muted text-muted-foreground',
}

export function CampaignStatusBadge({ status }: { status: CampaignStatus }) {
  return (
    <span className={cn(PILL, CAMPAIGN_CLASSES[status])}>{CAMPAIGN_STATUS_LABELS[status]}</span>
  )
}

const ENROLLMENT_CLASSES: Partial<Record<EnrollmentStatus, string>> = {
  active: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
  replied: 'bg-sky-500/15 text-sky-700 dark:text-sky-300',
  bounced: 'bg-destructive/10 text-destructive',
  opted_out: 'bg-destructive/10 text-destructive',
}

export function EnrollmentStatusBadge({ status }: { status: EnrollmentStatus }) {
  return (
    <span className={cn(PILL, ENROLLMENT_CLASSES[status] ?? 'bg-muted text-muted-foreground')}>
      {ENROLLMENT_STATUS_LABELS[status]}
    </span>
  )
}
