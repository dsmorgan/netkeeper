import { createFileRoute } from '@tanstack/react-router'

import { PlaceholderPage } from '@/components/placeholder-page'

export const Route = createFileRoute('/triage')({
  component: TriagePage,
})

function TriagePage() {
  return (
    <PlaceholderPage
      title="Triage"
      phase={1}
      purpose="One contact at a time with an evidence panel and keyboard decisions: met, not met, skip."
    />
  )
}
