import { AlertTriangle, Info } from 'lucide-react'
import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

import {
  RESOLUTION_CLASSES,
  RESOLUTION_LABELS,
  RUN_STATUS_CLASSES,
  RUN_STATUS_LABELS,
} from './fields'
import type { DuplicateGroup, Resolution, RunStatus } from './types'

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

/**
 * The name-and-company duplicate warning a commit's `duplicate_groups` carries (#228).
 *
 * Spec 8.2 step 4 never matches two rows against each other, only a row
 * against a contact already on record, so a person listed twice in one file
 * with no profile URL or email becomes two new contacts rather than one. This
 * names them; it does not suggest anything should have resolved differently.
 * `detail` lists each group's contacts and rows, for the run page; without it
 * this is the one-line summary the wizard's result step shows.
 */
export function DuplicateGroupsNote({
  groups,
  detail = false,
}: {
  groups: readonly DuplicateGroup[]
  detail?: boolean
}) {
  const total = groups.reduce((sum, group) => sum + group.contacts.length, 0)
  if (total === 0) return null
  return (
    <Note tone="warn" role="status">
      <p className="font-medium">
        {total} new contacts share a name and company with another row in this file.
      </p>
      <p>
        They were kept as separate contacts, as netkeeper&rsquo;s matching rules require: a name
        and a company are never enough to fold two rows of the same file together. Merging any of
        them by hand is safe, but it means this import can no longer be rolled back.
      </p>
      {detail && (
        <ul className="list-disc space-y-1 pl-5">
          {groups.map((group, index) => (
            <li key={index}>
              {group.contacts.map((contact, position) => (
                <span key={contact.contact_id}>
                  {position > 0 && ', '}
                  contact #{contact.contact_id} (row {contact.row_number})
                </span>
              ))}
            </li>
          ))}
        </ul>
      )}
    </Note>
  )
}
