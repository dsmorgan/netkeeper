import { createFileRoute } from '@tanstack/react-router'

import { PlaceholderPage } from '@/components/placeholder-page'

export const Route = createFileRoute('/templates')({
  component: TemplatesPage,
})

function TemplatesPage() {
  return (
    <PlaceholderPage
      title="Templates"
      phase={3}
      purpose="Editor with lint and live preview against a chosen contact."
    />
  )
}
