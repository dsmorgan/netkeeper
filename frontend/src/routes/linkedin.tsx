import { createFileRoute } from '@tanstack/react-router'

import { LinkedInPage } from '@/features/linkedin/linkedin-page'

export const Route = createFileRoute('/linkedin')({
  component: LinkedInPage,
})
