import { createFileRoute } from '@tanstack/react-router'

import { TriagePage } from '@/features/triage/triage-page'

export const Route = createFileRoute('/triage')({
  component: TriagePage,
})
