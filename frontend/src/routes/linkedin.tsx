import { createFileRoute } from '@tanstack/react-router'

import { PlaceholderPage } from '@/components/placeholder-page'

export const Route = createFileRoute('/linkedin')({
  component: LinkedInPage,
})

function LinkedInPage() {
  return (
    <PlaceholderPage
      title="LinkedIn"
      phase={2}
      purpose="Runs, live progress, budget and heat, pins, preflight, browser launch instructions."
    />
  )
}
