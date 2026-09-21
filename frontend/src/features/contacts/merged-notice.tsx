import { Link } from '@tanstack/react-router'

import { mergedInto } from './api'

/**
 * A failed write, in the terms the person can act on.
 *
 * A write to a contact that was merged away answers `409 merged` naming the
 * survivor (spec 8.2). That is not an error to shrug at: it says where the
 * person went, so it renders as a link to them.
 */
export function WriteError({ error }: { error: Error | null }) {
  if (!error) return null
  const survivor = mergedInto(error)
  return (
    <p role="alert" className="text-destructive">
      {survivor === null ? (
        error.message
      ) : (
        <>
          This contact was merged into another one.{' '}
          <Link
            to="/contacts/$contactId"
            params={{ contactId: String(survivor) }}
            className="underline underline-offset-4"
          >
            Open contact {survivor}
          </Link>{' '}
          and make the change there.
        </>
      )}
    </p>
  )
}
