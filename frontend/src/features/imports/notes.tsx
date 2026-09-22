import { AlertTriangle, Info } from 'lucide-react'
import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

import {
  RESOLUTION_CLASSES,
  RESOLUTION_LABELS,
  RUN_STATUS_CLASSES,
  RUN_STATUS_LABELS,
} from './fields'
import type { Resolution, RunStatus } from './types'

/** What one row of the file is: a match, a candidate, a new contact, or skipped. */
export function OutcomeBadge({ resolution }: { resolution: Resolution }) {
  return (
    <span
      className={cn(
        'inline-flex h-5 shrink-0 items-center rounded-4xl px-2 text-xs font-medium whitespace-nowrap',
        RESOLUTION_CLASSES[resolution],
      )}
    >
      {RESOLUTION_LABELS[resolution]}
    </span>
  )
}

/** Whether a run is a draft, applied, or undone. */
export function RunStatusBadge({ status }: { status: RunStatus }) {
  return (
    <span
      className={cn(
        'inline-flex h-5 shrink-0 items-center rounded-4xl px-2 text-xs font-medium whitespace-nowrap',
        RUN_STATUS_CLASSES[status],
      )}
    >
      {RUN_STATUS_LABELS[status]}
    </span>
  )
}

/** A failure, announced. `role="alert"` so a screen reader hears it on arrival. */
export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <p
      role="alert"
      className="flex items-start gap-2 rounded-lg bg-destructive/10 px-3 py-2 text-destructive"
    >
      <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span>{children}</span>
    </p>
  )
}

/**
 * Something the person should read before deciding, but which is not an error.
 *
 * `role` is opt-in and omitted by default (unchanged behavior for every
 * existing caller). Pass `"status"` when the note carries something a screen
 * reader needs to hear as it appears rather than only find by reading —
 * archive-flow.tsx's guidance body and its `needs_review` callout are the
 * first callers, since those are the one way out of a state this pipeline
 * cannot otherwise resolve, and a plain `<div>` says nothing on its own.
 */
export function Note({
  children,
  tone = 'info',
  role,
}: {
  children: ReactNode
  tone?: 'info' | 'warn'
  role?: 'status'
}) {
  const Icon = tone === 'warn' ? AlertTriangle : Info
  return (
    <div
      role={role}
      className={cn(
        'flex items-start gap-2 rounded-lg px-3 py-2',
        tone === 'warn'
          ? 'bg-amber-500/10 text-amber-900 dark:text-amber-200'
          : 'bg-muted text-muted-foreground',
      )}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0 space-y-1">{children}</div>
    </div>
  )
}
