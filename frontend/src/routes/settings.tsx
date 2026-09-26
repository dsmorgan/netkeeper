import { createFileRoute } from '@tanstack/react-router'

import { SettingsPage } from '@/features/settings/settings-page'
import type { GmailOutcome } from '@/features/settings/gmail-section'

/** `?gmail=connected` or `?gmail=error&reason=<code>`: where the Gmail OAuth callback lands. */
function validateSearch(search: Record<string, unknown>): GmailOutcome {
  const text = (value: unknown) => (typeof value === 'string' && value !== '' ? value : undefined)
  return { gmail: text(search.gmail), reason: text(search.reason) }
}

export const Route = createFileRoute('/settings')({
  validateSearch,
  component: Settings,
})

function Settings() {
  return <SettingsPage gmail={Route.useSearch()} />
}
