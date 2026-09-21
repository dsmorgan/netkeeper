import { Checkbox as CheckboxPrimitive } from '@base-ui/react/checkbox'
import { Check, Minus } from 'lucide-react'
import { cn } from 'cn'

/** A tick box. `indeterminate` renders the dash a partly-selected page needs. */
function Checkbox({ className, ...props }: CheckboxPrimitive.Root.Props) {
  return (
    <CheckboxPrimitive.Root
      data-slot="checkbox"
      className={cn(
        'peer size-4 shrink-0 rounded-[4px] border border-border bg-background shadow-xs transition-shadow outline-none',
        'focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50',
        'data-checked:border-primary data-checked:bg-primary data-checked:text-primary-foreground',
        'data-indeterminate:border-primary data-indeterminate:bg-primary data-indeterminate:text-primary-foreground',
        'disabled:pointer-events-none disabled:opacity-50 dark:border-input',
        className,
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator
        data-slot="checkbox-indicator"
        className="flex items-center justify-center text-current"
      >
        {props.indeterminate ? <Minus className="size-3" /> : <Check className="size-3" />}
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  )
}

export { Checkbox }
