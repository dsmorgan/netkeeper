import { AlertTriangle } from 'lucide-react'

import { formatWhen } from './fields'
import type { LinkedInStatus } from './types'

/**
 * The session flag, as a banner nothing else on the page can outshine (spec
 * 9.7). `netkeeper` never clears a `checkpoint` flag itself — a live session
 * cookie is not proof a checkpoint is resolved — and the API this page is
 * built on (#175) has no clear-flag route yet, so there is no clear button
 * here either: only `netkeeper linkedin clear-flag` can end this banner. A UI
 * clear button is a follow-up for once that route exists, and it should stay
 * a confirmed act, never automatic, even then.
 */
export function SessionBanner({ status }: { status: LinkedInStatus }) {
  if (status.session_flag === null) return null

  const when =
    status.session_flagged_at === null ? '' : ` (raised ${formatWhen(status.session_flagged_at)})`

  return (
    <div
      role="alert"
      className="flex items-start gap-3 rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-destructive"
    >
      <AlertTriangle className="mt-0.5 size-5 shrink-0" aria-hidden="true" />
      <div className="space-y-1">
        <h2 className="font-heading text-sm font-semibold">
          {status.session_flag === 'checkpoint'
            ? 'LinkedIn asked for a checkpoint'
            : 'Logged out of LinkedIn'}
        </h2>
        <p className="text-sm">
          {status.session_flag === 'checkpoint' ? (
            <>
              Open LinkedIn yourself in the netkeeper Chrome profile and resolve the checkpoint,
              then clear the flag with{' '}
              <code className="font-mono text-xs">netkeeper linkedin clear-flag</code>.{when}
            </>
          ) : (
            <>Log back in to LinkedIn in the netkeeper Chrome profile.{when}</>
          )}
        </p>
        <p className="text-xs text-destructive/80">
          No run will touch the browser again until this is cleared.
        </p>
      </div>
    </div>
  )
}
