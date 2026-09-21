import { createFileRoute } from '@tanstack/react-router'

import { RunHistoryPage } from '@/features/imports/run-history-page'

export const Route = createFileRoute('/imports/runs/')({
  component: RunHistoryPage,
})
