import { createFileRoute } from '@tanstack/react-router'

import { CampaignDetailPage, NoSuchCampaign } from '@/features/campaigns/campaign-detail-page'

export const Route = createFileRoute('/campaigns/$campaignId')({
  component: CampaignDetail,
})

function CampaignDetail() {
  const { campaignId } = Route.useParams()
  // Checked here rather than sent, as the import run page does (#94).
  if (!/^[1-9]\d*$/.test(campaignId)) return <NoSuchCampaign />
  return <CampaignDetailPage campaignId={Number(campaignId)} />
}
