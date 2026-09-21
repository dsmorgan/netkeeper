import { createFileRoute } from '@tanstack/react-router'

import { ExportsPage } from '@/features/crm/exports-page'

export const Route = createFileRoute('/exports')({
  component: Exports,
})

function Exports() {
  return <ExportsPage />
}
