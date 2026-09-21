import * as React from 'react'
import { cn } from 'cn'

/**
 * A styled native `<select>`.
 *
 * The popup selects in this app sit inside table toolbars and dialogs, where a
 * native control keeps keyboard and screen-reader behavior for free and costs
 * no portal; the shadcn popup select is reserved for places that need custom
 * item rendering.
 */
function Select({ className, ...props }: React.ComponentProps<'select'>) {
  return (
    <select
      data-slot="select"
      className={cn(
        'h-8 rounded-lg border border-border bg-background px-2 text-sm shadow-xs transition-[color,box-shadow] outline-none',
        'focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50',
        'disabled:pointer-events-none disabled:opacity-50 dark:border-input dark:bg-input/30',
        className,
      )}
      {...props}
    />
  )
}

export { Select }
