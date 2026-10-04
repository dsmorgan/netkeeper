import { createFileRoute } from '@tanstack/react-router'

import type { EnrollmentStatus } from '@/features/campaigns/api'
import { CampaignDetailPage, NoSuchCampaign } from '@/features/campaigns/campaign-detail-page'
import { ENROLLMENT_STATUSES } from '@/features/campaigns/format'

/** The enrollment table's status filter (#350), kept in the URL so Back and reload keep it. */
export interface CampaignSearch {
  status?: EnrollmentStatus
}

function statusOf(value: unknown): EnrollmentStatus | undefined {
  return ENROLLMENT_STATUSES.find((s) => s === value)
}

export const Route = createFileRoute('/campaigns/$campaignId')({
  validateSearch: (search: Record<string, unknown>): CampaignSearch => {
    const status = statusOf(search.status)
    return status === undefined ? {} : { status }
  },
  component: CampaignDetail,
})

function CampaignDetail() {
  const { campaignId } = Route.useParams()
  // Checked again: the router keeps a raw value the validator left out (#350).
  const status = statusOf(Route.useSearch().status)
  const navigate = Route.useNavigate()
  // Checked here rather than sent, as the import run page does (#94).
  if (!/^[1-9]\d*$/.test(campaignId)) return <NoSuchCampaign />
  return (
    <CampaignDetailPage
      campaignId={Number(campaignId)}
      status={status ?? ''}
      onStatus={(next) =>
        void navigate({
          search: next === '' ? {} : { status: next },
          resetScroll: false,
        })
      }
    />
  )
}
