/**
 * The editor's lint, inline (#344): each finding sits under the field it is
 * about, says which line, quotes that line, and says in one sentence why it
 * matters. In the body, the lines with a finding are also marked behind the
 * text itself.
 *
 * The list for a field has an `id` the field names in `aria-describedby`, so a
 * screen reader reads the findings with the field.
 */
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

import type { LintIssue } from './api'
import type { ShownIssue } from './lint'

interface InlineLintProps {
  id: string
  /** The list's accessible name, like "Body lint". */
  label: string
  shown: readonly ShownIssue[]
  /** The field's text, to quote the line a finding is on. Omit for a one-line field. */
  text?: string
  /** Moves the cursor to a line of the field; offered for a finding with a line. */
  onGoToLine?: (line: number) => void
}

export function InlineLint({ id, label, shown, text, onGoToLine }: InlineLintProps) {
  if (shown.length === 0) return null
  const lines = text?.split('\n')
  return (
    <ul id={id} aria-label={label} className="space-y-1 text-sm">
      {shown.map((item, index) => {
        const { issue, line } = item
        const quoted = line === null || lines === undefined ? undefined : lines[line - 1]
        return (
          <li
            key={`${issue.rule}-${issue.field ?? ''}-${index}`}
            className={cn(
              'flex gap-2 rounded-md border-l-4 bg-muted/40 px-2 py-1.5',
              issue.severity === 'error' ? 'border-destructive' : 'border-amber-500',
            )}
          >
            <Badge
              variant={issue.severity === 'error' ? 'destructive' : 'outline'}
              className="mt-0.5"
            >
              {issue.severity === 'error' ? 'Error' : 'Warning'}
            </Badge>
            <div className="min-w-0 flex-1 space-y-0.5">
              {lines !== undefined && line !== null && (
                <p className="flex min-w-0 items-baseline gap-2 text-xs text-muted-foreground">
                  <span className="shrink-0">Line {line}</span>
                  {quoted !== undefined && quoted.trim() !== '' && (
                    <code className="truncate font-mono">{quoted}</code>
                  )}
                </p>
              )}
              <p className="font-medium">{item.headline}</p>
              {item.detail !== null && (
                <p className="text-xs text-muted-foreground">{item.detail}</p>
              )}
              <p className="text-xs text-muted-foreground">Why it matters: {item.why}</p>
            </div>
            {onGoToLine !== undefined && line !== null && (
              <Button
                type="button"
                size="xs"
                variant="ghost"
                className="self-start"
                onClick={() => onGoToLine(line)}
              >
                Go to line {line}
              </Button>
            )}
          </li>
        )
      })}
    </ul>
  )
}

/**
 * The body's text again, invisible, behind the textarea, with each line that has a
 * finding tinted. It wraps exactly as the textarea does (same font, padding,
 * border and scrollbar gutter) and scrolls with it, so the tint sits on the line.
 * Decorative: the findings themselves are in {@link InlineLint}.
 */
export function LineMarks({
  text,
  marks,
  scrollTop,
  className,
}: {
  text: string
  marks: ReadonlyMap<number, LintIssue['severity']>
  scrollTop: number
  className?: string
}) {
  return (
    <div
      aria-hidden
      data-testid="line-marks"
      className={cn(
        'pointer-events-none absolute inset-0 overflow-hidden rounded-lg border border-transparent px-2.5 py-1.5 text-sm break-words whitespace-pre-wrap text-transparent [scrollbar-gutter:stable]',
        className,
      )}
    >
      <div style={{ transform: `translateY(${-scrollTop}px)` }}>
        {text.split('\n').map((line, index) => {
          const severity = marks.get(index + 1)
          return (
            <div
              key={index}
              data-line={index + 1}
              data-severity={severity}
              className={cn(
                severity === 'error' && 'rounded-sm bg-destructive/15',
                severity === 'warning' && 'rounded-sm bg-amber-500/20',
              )}
            >
              {line === '' ? ' ' : line}
            </div>
          )
        })}
      </div>
    </div>
  )
}
