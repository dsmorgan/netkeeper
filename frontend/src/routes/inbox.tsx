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
  // Not keyed by enrollment: removing that chip keeps the other filters (#402).
  return <InboxPage enrollment={enrollment} />
}
