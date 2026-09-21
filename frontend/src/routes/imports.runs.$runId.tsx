import { createFileRoute } from '@tanstack/react-router'

import { RunDetailPage } from '@/features/imports/run-detail-page'

export const Route = createFileRoute('/imports/runs/$runId')({
  component: RunDetail,
})

function RunDetail() {
  const { runId } = Route.useParams()
  return <RunDetailPage runId={Number(runId)} />
}
