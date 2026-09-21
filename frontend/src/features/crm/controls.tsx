/**
 * The small controls this feature reuses, in the base-nova shapes.
 *
 * `NativeSelect` is a real `<select>` rather than the popup listbox in
 * `components/ui/select.tsx`. The filter builder puts a picker on every row —
 * twenty fields, up to eight ops — and a native control gets the keyboard, the
 * platform's own long-list behaviour, and the screen reader's list semantics
 * for free, which a popup would have to re-earn.
 */
import type * as React from 'react'
import { AlertTriangle, ChevronDown, Info, TriangleAlert } from 'lucide-react'

import { cn } from '@/lib/utils'

export function NativeSelect({ className, ...props }: React.ComponentProps<'select'>) {
  return (
    <span data-slot="native-select" className={cn('relative inline-flex max-w-full', className)}>
      <select
        className={cn(
          'h-8 w-full appearance-none rounded-lg border border-input bg-transparent py-1 pr-7 pl-2.5',
          'text-sm transition-colors outline-none',
          'focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50',
          'disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30',
        )}
        {...props}
      />
      <ChevronDown
        aria-hidden
        className="pointer-events-none absolute top-1/2 right-2 size-3.5 -translate-y-1/2 text-muted-foreground"
      />
    </span>
  )
}

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
