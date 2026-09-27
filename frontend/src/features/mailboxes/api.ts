/**
 * Every call about the Gmail mailbox (P3-01, spec 11.5), through the generated client.
 *
 * Authorizing leaves the app: `startAuthorization` answers Google's URL, the
 * page navigates there, and Google sends the browser back to the backend's
 * callback, which redirects to `/settings?gmail=connected` (or
 * `?gmail=error&reason=<code>`).
 */
import { queryOptions } from '@tanstack/react-query'

import { api } from '@/api/client'
import { detailMessage } from '@/api/errors'
import type { components } from '@/api/schema'

export type Mailbox = components['schemas']['MailboxOut']
export type MailboxStatus = components['schemas']['MailboxStatusOut']

export const mailboxKeys = {
  all: ['mailboxes'] as const,
  list: () => [...mailboxKeys.all, 'list'] as const,
  status: () => [...mailboxKeys.all, 'status'] as const,
}

function failure(error: unknown, response: Response, what: string): Error {
  return new Error(detailMessage(error) ?? `${what} returned ${response.status}`)
}

/** Whether a client is stored, every mailbox, and whether any needs re-authorizing. */
export const mailboxStatusQuery = queryOptions({
  queryKey: mailboxKeys.status(),
  queryFn: async ({ signal }): Promise<MailboxStatus> => {
    const { data, error, response } = await api.GET('/api/v1/mailboxes/status', { signal })
    if (data === undefined) throw failure(error, response, 'GET /mailboxes/status')
    return data
  },
})

/**
 * Every mailbox, from the database alone. The banner reads this rather than the
 * status, which also reads the OAuth client from the Keychain: a locked Keychain
 * must not hide a mailbox that needs re-authorizing.
 */
export const mailboxListQuery = queryOptions({
  queryKey: mailboxKeys.list(),
  queryFn: async ({ signal }): Promise<Mailbox[]> => {
    const { data, error, response } = await api.GET('/api/v1/mailboxes', { signal })
    if (data === undefined) throw failure(error, response, 'GET /mailboxes')
    return data
  },
})

export async function saveClient(body: { client_id: string; client_secret: string }) {
  const { data, error, response } = await api.PUT('/api/v1/mailboxes/oauth/client', { body })
  if (data === undefined) throw failure(error, response, 'PUT /mailboxes/oauth/client')
  return data
}

/** Google's authorization URL; `mailboxId` preselects that account when re-authorizing. */
export async function startAuthorization(mailboxId: number | null): Promise<string> {
  const { data, error, response } = await api.POST('/api/v1/mailboxes/oauth/start', {
    body: { mailbox_id: mailboxId },
  })
  if (data === undefined) throw failure(error, response, 'POST /mailboxes/oauth/start')
  return data.authorization_url
}

export async function checkMailbox(mailboxId: number): Promise<Mailbox> {
  const { data, error, response } = await api.POST('/api/v1/mailboxes/{mailbox_id}/check', {
    params: { path: { mailbox_id: mailboxId } },
  })
  if (data === undefined) throw failure(error, response, 'POST /mailboxes/{id}/check')
  return data
}

export async function disconnectMailbox(mailboxId: number): Promise<Mailbox> {
  const { data, error, response } = await api.POST('/api/v1/mailboxes/{mailbox_id}/disconnect', {
    params: { path: { mailbox_id: mailboxId } },
  })
  if (data === undefined) throw failure(error, response, 'POST /mailboxes/{id}/disconnect')
  return data
}

/** Leaves the app for Google's page. A seam, so a test can see where it would go. */
export const navigation = {
  assign(url: string): void {
    window.location.assign(url)
  },
}

/** What a status reason or a callback's `reason` means, for a person. */
export function reasonText(reason: string | null | undefined): string | null {
  if (reason === null || reason === undefined || reason === '') return null
  return REASONS[reason] ?? `Google answered "${reason}".`
}

const REASONS: Record<string, string> = {
  invalid_grant:
    'Google no longer accepts the token: access was revoked, or the consent screen is still in Testing and its 7 days are up.',
  invalid_client:
    'Google does not know the OAuth client any more: it was deleted, or its secret was reset.',
  token_missing: 'The Keychain has no token for this mailbox.',
  client_missing: 'No OAuth client is stored. Add the client ID and secret first.',
  disconnected: 'Disconnected. Its token is gone from the Keychain.',
  state_mismatch:
    'The answer from Google did not match an authorization started here in the last 10 minutes. Start again.',
  access_denied: 'Access was not allowed on Google’s page.',
  scope_not_granted: 'Gmail access was unticked on Google’s page. Start again and leave it ticked.',
  gmail_api_refused:
    'The Gmail API refused the token. Enable the Gmail API in the Cloud project (docs/gmail-setup.md, step 2).',
  other_mailbox_connected:
    'Another Gmail account is already connected. Disconnect it first to connect a different one.',
  keychain: 'The Keychain refused. Unlock it and try again.',
  unavailable: 'Google could not be reached. Try again in a moment.',
  no_code: 'Google sent no authorization code back. Start again.',
}
