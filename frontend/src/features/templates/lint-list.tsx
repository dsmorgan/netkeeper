/**
 * Lint findings as one list (the preview's warnings, an older version's lint at
 * save): severity, which part, the line where lint named one, the plain
 * sentence from `showIssue`, and why the rule matters. The editor shows its own
 * lint inline, next to each field (`inline-lint.tsx`).
 */
import { Badge } from '@/components/ui/badge'

import type { LintIssue } from './api'
import { showIssue } from './lint'

const PART_LABELS = { subject: 'Subject', body: 'Body' } as const

export function LintList({ issues, label }: { issues: readonly LintIssue[]; label: string }) {
  return (
    <ul aria-label={label} className="divide-y rounded-lg border text-sm">
      {issues.map((issue, index) => {
        const shown = showIssue(issue)
        return (
          <li key={`${issue.rule}-${issue.part}-${index}`} className="flex gap-2 px-3 py-2">
            <Badge
              variant={issue.severity === 'error' ? 'destructive' : 'outline'}
              className="mt-0.5"
            >
              {issue.severity === 'error' ? 'Error' : 'Warning'}
            </Badge>
            <div className="min-w-0 space-y-0.5">
              <p className="text-xs text-muted-foreground">
                {PART_LABELS[issue.part]}
                {shown.line !== null && <> · line {shown.line}</>}
              </p>
              <p className="font-medium">{shown.headline}</p>
              {shown.detail !== null && (
                <p className="text-xs text-muted-foreground">{shown.detail}</p>
              )}
              <p className="text-xs text-muted-foreground">Why it matters: {shown.why}</p>
            </div>
          </li>
        )
      })}
    </ul>
  )
}
