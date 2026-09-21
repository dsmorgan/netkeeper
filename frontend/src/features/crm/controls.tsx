/**
 * The small standing notes this feature reuses, in the base-nova shapes.
 *
 * The pickers here are `@/components/ui/select`, which is already a styled
 * native `<select>` for the same reasons the filter builder wants one: a row
 * offers twenty fields and up to eight ops, and a native control keeps the
 * keyboard, the platform's long-list behaviour, and the list semantics a
 * screen reader reads, at no cost in portals.
 */
import type * as React from 'react'
import { AlertTriangle, Info, TriangleAlert } from 'lucide-react'

import { cn } from '@/lib/utils'

interface CalloutProps extends React.ComponentProps<'div'> {
  tone?: 'info' | 'warning' | 'danger'
  title?: string
}

const CALLOUT_ICONS = {
  info: Info,
  warning: TriangleAlert,
  danger: AlertTriangle,
} as const

/** A short standing note: a caveat, a refusal, or a count that needs explaining. */
export function Callout({ tone = 'info', title, className, children, ...props }: CalloutProps) {
  const Icon = CALLOUT_ICONS[tone]
  return (
    <div
      data-slot="callout"
      role={tone === 'danger' ? 'alert' : 'note'}
      className={cn(
        'flex gap-2 rounded-lg border px-3 py-2 text-sm',
        tone === 'info' && 'border-border bg-muted/40 text-muted-foreground',
        tone === 'warning' && 'border-amber-500/40 bg-amber-500/10 text-foreground',
        tone === 'danger' && 'border-destructive/40 bg-destructive/10 text-foreground',
        className,
      )}
      {...props}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div className="min-w-0 space-y-1">
        {title !== undefined && <p className="font-medium text-foreground">{title}</p>}
        <div className="[&_p]:leading-snug">{children}</div>
      </div>
    </div>
  )
}

/** Nothing to show yet, said plainly and with the next step. */
export function EmptyState({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div
      data-slot="empty-state"
      className="rounded-lg border border-dashed px-4 py-6 text-center text-sm text-muted-foreground"
    >
      <p className="font-medium text-foreground">{title}</p>
      {children !== undefined && <div className="mt-1">{children}</div>}
    </div>
  )
}

/** A neutral "still loading" line; `role="status"` so a test and a reader can find it. */
export function LoadingNote({ label }: { label: string }) {
  return (
    <p role="status" className="px-1 py-4 text-sm text-muted-foreground">
      {label}
    </p>
  )
}

/** A failed read, with the server's own words where it had any. */
export function ErrorNote({ label, error }: { label: string; error: unknown }) {
  const detail = error instanceof Error ? error.message : String(error)
  return (
    <Callout tone="danger" title={label}>
      <p>{detail}</p>
    </Callout>
  )
}
