import { createFileRoute } from '@tanstack/react-router'

import { ImportWizard } from '@/features/imports/import-wizard'

interface ImportSearch {
  /** A draft run to finish, linked from its history page. */
  run?: number
}

export const Route = createFileRoute('/imports/')({
  validateSearch: (search: Record<string, unknown>): ImportSearch => {
    const run = Number(search.run)
    return Number.isInteger(run) && run > 0 ? { run } : {}
  },
  component: ImportsIndex,
})

function ImportsIndex() {
  const { run } = Route.useSearch()
  return <ImportWizard resumeRunId={run} />
}
