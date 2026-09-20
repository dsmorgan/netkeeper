import { createFileRoute } from '@tanstack/react-router'

import { PlaceholderPage } from '@/components/placeholder-page'

export const Route = createFileRoute('/exports')({
  component: ExportsPage,
})

function ExportsPage() {
  return (
    <PlaceholderPage
      title="Exports"
      phase={1}
      purpose="CSV, JSON, and vCard with presets, starting with nine-column."
    />
  )
}
