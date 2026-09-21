import { createFileRoute } from '@tanstack/react-router'

import { CrmPage } from '@/features/crm/crm-page'

export const Route = createFileRoute('/lists')({
  component: ListsPage,
})

function ListsPage() {
  return <CrmPage />
}
