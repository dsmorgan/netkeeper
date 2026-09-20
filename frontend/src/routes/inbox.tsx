import { createFileRoute } from '@tanstack/react-router'

import { PlaceholderPage } from '@/components/placeholder-page'

export const Route = createFileRoute('/inbox')({
  component: InboxPage,
})

function InboxPage() {
  return (
    <PlaceholderPage
      title="Inbox"
      phase={3}
      purpose="Detected replies across campaigns, with mark-handled and add-note."
    />
  )
}
