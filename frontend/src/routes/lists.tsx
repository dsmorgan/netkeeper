import { createFileRoute } from '@tanstack/react-router'

import { CrmPage } from '@/features/crm/crm-page'
import { crmSearch, DEFAULT_CRM_TAB, validateCrmSearch } from '@/features/crm/crm-tabs'

export const Route = createFileRoute('/lists')({
  // The open tab is in the URL, so the dashboard can link to "Tags and rules".
  validateSearch: validateCrmSearch,
  component: ListsPage,
})

function ListsPage() {
  // Read again rather than trusted: the router lays a route's validated search
  // over the raw one, so an unknown `?tab=` would otherwise reach the page.
  const tab = validateCrmSearch(Route.useSearch()).tab ?? DEFAULT_CRM_TAB
  const navigate = Route.useNavigate()
  return (
    <CrmPage
      tab={tab}
      // Switching tabs replaces the entry: the tabs are one page, and Back
      // should leave it rather than walk back through them.
      onTabChange={(next) => void navigate({ search: crmSearch(next), replace: true })}
    />
  )
}
