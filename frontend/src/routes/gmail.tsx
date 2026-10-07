import { createFileRoute } from '@tanstack/react-router'

import { GmailPage } from '@/features/gmail/gmail-page'

export const Route = createFileRoute('/gmail')({
  component: GmailPage,
})
