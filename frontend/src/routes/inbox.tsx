import { createFileRoute } from '@tanstack/react-router'

import { InboxPage } from '@/features/inbox/inbox-page'

interface InboxSearch {
  enrollment?: number
}

export const Route = createFileRoute('/inbox')({
  validateSearch: (search: Record<string, unknown>): InboxSearch => {
    const enrollment = Number(search.enrollment)
    return Number.isInteger(enrollment) && enrollment > 0 ? { enrollment } : {}
  },
  component: Inbox,
})

function Inbox() {
  const { enrollment } = Route.useSearch()
  // Keyed so following "every enrollment" starts the filters afresh.
  return <InboxPage key={enrollment ?? 'all'} enrollment={enrollment} />
}
