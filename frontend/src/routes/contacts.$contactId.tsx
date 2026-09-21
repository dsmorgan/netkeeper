import { createFileRoute } from '@tanstack/react-router'

import { ContactDetailPage } from '@/features/contacts/contact-detail-page'

export const Route = createFileRoute('/contacts/$contactId')({
  component: ContactDetailRoute,
})

function ContactDetailRoute() {
  const { contactId } = Route.useParams()
  const id = Number(contactId)
  if (!Number.isInteger(id) || id <= 0) {
    return <p className="text-muted-foreground">That is not a contact id.</p>
  }
  return <ContactDetailPage contactId={id} />
}
