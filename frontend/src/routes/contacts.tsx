import { createFileRoute } from '@tanstack/react-router'

import { PlaceholderPage } from '@/components/placeholder-page'

export const Route = createFileRoute('/contacts')({
  component: ContactsPage,
})

function ContactsPage() {
  return (
    <PlaceholderPage
      title="Contacts"
      phase={1}
      purpose="Table with saved views; detail with fields, tags, timeline, snapshots, messages, LLM brief."
    />
  )
}
