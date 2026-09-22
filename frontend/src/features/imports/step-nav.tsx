import { Check } from 'lucide-react'

import { cn } from '@/lib/utils'

import { CSV_STEPS, STEP_LABELS, type Step } from './steps'

/**
 * Where the import has got to, and how far is left.
 *
 * `steps` defaults to the CSV pipeline's five steps; the archive flow passes
 * `ARCHIVE_STEPS` so its own, shorter shape is what shows (spec 10.5, P1-21).
 */
export function StepNav({
  current,
  steps = CSV_STEPS,
}: {
  current: Step
  steps?: readonly Step[]
}) {
  const position = steps.indexOf(current)
  return (
    <nav aria-label="Import steps">
      <ol className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        {steps.map((step, index) => {
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
