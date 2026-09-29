import { createFileRoute } from '@tanstack/react-router'

import { CampaignListPage } from '@/features/campaigns/campaign-list-page'

export const Route = createFileRoute('/campaigns/')({
  component: CampaignListPage,
})
