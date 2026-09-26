import { createFileRoute } from '@tanstack/react-router'

import { ContactsTablePage } from '@/features/contacts/contacts-table-page'
import { validateContactsSearch, type ContactsSearch } from '@/features/contacts/search'

export const Route = createFileRoute('/contacts/')({
  // The filter, sort, and page live in the URL, so a view is a link (spec 10.1).
  validateSearch: validateContactsSearch,
  component: ContactsRoute,
})

function ContactsRoute() {
  // Read again rather than trusted: the router lays a route's validated search
  // over the raw one, so a parameter validation dropped (`?met=bogus`,
  // `?page=0`) would otherwise reach the table as typed and break it.
  const search = validateContactsSearch(Route.useSearch())
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
