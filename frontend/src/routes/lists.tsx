import { createFileRoute } from '@tanstack/react-router'

import { PlaceholderPage } from '@/components/placeholder-page'

export const Route = createFileRoute('/lists')({
  component: ListsPage,
})

function ListsPage() {
  return (
    <PlaceholderPage title="Lists" phase={1} purpose="Static and smart lists, filter builder." />
  )
}
