import { createFileRoute } from '@tanstack/react-router'

import { PlaceholderPage } from '@/components/placeholder-page'

export const Route = createFileRoute('/imports')({
  component: ImportsPage,
})

function ImportsPage() {
  return (
    <PlaceholderPage
      title="Imports"
      phase={1}
      purpose="Upload a CSV or the LinkedIn archive, map columns, review candidates, commit."
    />
  )
}
