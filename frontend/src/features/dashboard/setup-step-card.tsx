import { Link } from '@tanstack/react-router'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

import type { SetupStep, StepState } from './setup-steps'

const STATE_LABELS: Record<StepState, string> = {
  not_started: 'Not started',
  in_progress: 'In progress',
  done: 'Done',
  unknown: 'Not tracked',
}

/** Same color vocabulary as the import run status badge: emerald done, amber mid-flight. */
const STATE_CLASSES: Record<StepState, string> = {
  not_started: 'bg-muted text-muted-foreground',
  in_progress: 'bg-amber-500/15 text-amber-800 dark:text-amber-300',
  done: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  unknown: 'bg-sky-500/10 text-sky-700 dark:text-sky-300',
}

/** One row of the setup path: its number, real count, state, and the control that advances it. */
export function SetupStepCard({ step, index }: { step: SetupStep; index: number }) {
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>
          <span aria-hidden="true" className="text-muted-foreground">
            {index}.{' '}
          </span>
          {step.title}
        </CardTitle>
        <CardDescription>{step.detail}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-wrap items-center gap-3">
        <Badge className={cn(STATE_CLASSES[step.state])}>{STATE_LABELS[step.state]}</Badge>
        {
          // `render` clones its element and merges the button's own props (children
          // included) onto it, so the target has to be the `Link` itself: a wrapper
          // component in between would take `children` as a prop and drop it, and the
          // button would render as a link with no label. `to`'s routes carry different
          // search schemas, which is the only reason this branches at all.
          step.to === '/imports' ? (
            <Button
              size="sm"
              variant="outline"
              render={<Link to="/imports" search={step.search} />}
            >
              {step.cta}
            </Button>
          ) : (
            <Button size="sm" variant="outline" render={<Link to={step.to} />}>
              {step.cta}
            </Button>
          )
        }
      </CardContent>
    </Card>
  )
}
