import { Check } from 'lucide-react'

import { cn } from '@/lib/utils'

import { STEPS, STEP_LABELS, type Step } from './steps'

/** Where the import has got to, and how far is left. */
export function StepNav({ current }: { current: Step }) {
  const position = STEPS.indexOf(current)
  return (
    <nav aria-label="Import steps">
      <ol className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        {STEPS.map((step, index) => {
          const done = index < position
          const here = index === position
          return (
            <li key={step} className="flex items-center gap-2">
              {index > 0 && (
                <span aria-hidden="true" className="text-muted-foreground/50">
                  →
                </span>
              )}
              <span
                aria-current={here ? 'step' : undefined}
                className={cn(
                  'flex items-center gap-1.5 rounded-md px-2 py-1',
                  here && 'bg-muted font-medium text-foreground',
                  !here && done && 'text-muted-foreground',
                  !here && !done && 'text-muted-foreground/60',
                )}
              >
                {done ? (
                  <Check className="size-3.5" aria-hidden="true" />
                ) : (
                  <span aria-hidden="true" className="tabular-nums">
                    {index + 1}.
                  </span>
                )}
                {STEP_LABELS[step]}
                {done && <span className="sr-only">(done)</span>}
              </span>
            </li>
          )
        })}
      </ol>
    </nav>
  )
}
