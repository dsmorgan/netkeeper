import { createFileRoute } from '@tanstack/react-router'

import { ContactsTablePage } from '@/features/contacts/contacts-table-page'
import { validateContactsSearch, type ContactsSearch } from '@/features/contacts/search'

export const Route = createFileRoute('/contacts/')({
  // The filter, sort, and page live in the URL, so a view is a link (spec 10.1).
  validateSearch: validateContactsSearch,
  component: ContactsRoute,
})

function ContactsRoute() {
  const search = Route.useSearch()
  const navigate = Route.useNavigate()
  return (
    <ContactsTablePage
      search={search}
      onNavigate={(
        update: (previous: ContactsSearch) => ContactsSearch,
        options?: { replace?: boolean },
      ) => void navigate({ search: update, replace: options?.replace })}
    />
  )
}
