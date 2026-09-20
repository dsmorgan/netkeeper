import { createFileRoute } from '@tanstack/react-router'

import { PlaceholderPage } from '@/components/placeholder-page'

export const Route = createFileRoute('/campaigns')({
  component: CampaignsPage,
})

function CampaignsPage() {
  return (
    <PlaceholderPage
      title="Campaigns"
      phase={3}
      purpose="Builder, review gate, progress per step, replies, waiting-for-you prefill list."
    />
  )
}
