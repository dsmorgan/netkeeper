import * as React from 'react'
import { cn } from 'cn'

/** A plain styled text input, matching the button's height and focus ring. */
function Input({ className, type = 'text', ...props }: React.ComponentProps<'input'>) {
  return (
    <input
      data-slot="input"
      type={type}
      className={cn(
        'flex h-8 w-full min-w-0 rounded-lg border border-border bg-background px-2.5 py-1 text-sm shadow-xs transition-[color,box-shadow] outline-none',
        'placeholder:text-muted-foreground selection:bg-primary selection:text-primary-foreground',
        'focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50',
        'disabled:pointer-events-none disabled:opacity-50',
        'aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20',
        'dark:border-input dark:bg-input/30',
        className,
      )}
      {...props}
    />
  )
}

/** The multi-line sibling of {@link Input}. */
function Textarea({ className, ...props }: React.ComponentProps<'textarea'>) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        'flex min-h-16 w-full rounded-lg border border-border bg-background px-2.5 py-1.5 text-sm shadow-xs transition-[color,box-shadow] outline-none',
        'placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50',
        'disabled:pointer-events-none disabled:opacity-50 dark:border-input dark:bg-input/30',
        className,
      )}
      {...props}
    />
  )
}

export { Input, Textarea }
