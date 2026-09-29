import { createFileRoute } from '@tanstack/react-router'

import { CampaignBuilderPage } from '@/features/campaigns/campaign-builder-page'

export const Route = createFileRoute('/campaigns/new')({
  component: CampaignBuilderPage,
})
