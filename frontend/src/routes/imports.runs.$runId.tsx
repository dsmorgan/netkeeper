import { createFileRoute } from '@tanstack/react-router'

import { NoSuchRun, RunDetailPage } from '@/features/imports/run-detail-page'

export const Route = createFileRoute('/imports/runs/$runId')({
  component: RunDetail,
})

function RunDetail() {
  const { runId } = Route.useParams()
  // Checked here rather than sent: the backend's answer to `/imports/abc` is a
  // validation message about parsing integers, not something to show (#94).
  if (!/^[1-9]\d*$/.test(runId)) return <NoSuchRun runId={runId} />
  return <RunDetailPage runId={Number(runId)} />
}
