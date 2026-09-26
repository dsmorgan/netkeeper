import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { navigation } from '@/features/mailboxes/api'
import { mailbox, renderWithBackend, status } from '@/features/mailboxes/test-support'
import { resetFakeEventSource } from '@/test/fake-event-source'
import { jsonResponse } from '@/test/fetch'

import { GmailSection, type GmailOutcome } from './gmail-section'

afterEach(() => {
  resetFakeEventSource()
  vi.restoreAllMocks()
})

function renderSection(
  current: () => ReturnType<typeof status>,
  routes: Parameters<typeof renderWithBackend>[2] = {},
  outcome: GmailOutcome = {},
) {
  return renderWithBackend(<GmailSection outcome={outcome} />, current, routes)
}

describe('GmailSection', () => {
  it('asks for the OAuth client first, and cannot connect without one', async () => {
    renderSection(() => status({ client_configured: false, client_id: null }))
    expect(await screen.findByLabelText('Client ID')).toBeInTheDocument()
    expect(screen.getByLabelText('Client secret')).toHaveAttribute('type', 'password')
    expect(screen.getByRole('button', { name: 'Connect Gmail' })).toBeDisabled()
    expect(screen.getByText('Save the OAuth client first.')).toBeInTheDocument()
  })

  it('saves the client, then offers to connect', async () => {
    let current = status({ client_configured: false, client_id: null })
    const { calls } = renderSection(() => current, {
      'PUT /api/v1/mailboxes/oauth/client': () => {
        current = status()
        return jsonResponse(current)
      },
    })
    fireEvent.change(await screen.findByLabelText('Client ID'), {
      target: { value: 'abc.apps.googleusercontent.com' },
    })
    fireEvent.change(screen.getByLabelText('Client secret'), { target: { value: 's3cret' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save client' }))

    await waitFor(() =>
      expect(calls).toContainEqual({
        method: 'PUT',
        path: '/api/v1/mailboxes/oauth/client',
        body: { client_id: 'abc.apps.googleusercontent.com', client_secret: 's3cret' },
      }),
    )
    expect(await screen.findByRole('button', { name: 'Connect Gmail' })).toBeEnabled()
    expect(screen.queryByLabelText('Client ID')).not.toBeInTheDocument()
    expect(screen.queryByText('s3cret')).not.toBeInTheDocument()
  })

  it('shows why a client was refused', async () => {
    renderSection(() => status({ client_configured: false, client_id: null }), {
      'PUT /api/v1/mailboxes/oauth/client': () =>
        jsonResponse(
          { detail: 'a Google OAuth client ID ends in .apps.googleusercontent.com' },
          422,
        ),
    })
    fireEvent.change(await screen.findByLabelText('Client ID'), { target: { value: 'my-project' } })
    fireEvent.change(screen.getByLabelText('Client secret'), { target: { value: 's' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save client' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'ends in .apps.googleusercontent.com',
    )
  })

  it('connects by going to Google’s page', async () => {
    const assign = vi.spyOn(navigation, 'assign').mockImplementation(() => {})
    const { calls } = renderSection(() => status(), {
      'POST /api/v1/mailboxes/oauth/start': () =>
        jsonResponse({ authorization_url: 'https://accounts.example/auth' }),
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Connect Gmail' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://accounts.example/auth'))
    expect(calls).toContainEqual({
      method: 'POST',
      path: '/api/v1/mailboxes/oauth/start',
      body: { mailbox_id: null },
    })
  })

  it('shows a connected mailbox and checks it on demand', async () => {
    const { calls } = renderSection(() => status({ mailboxes: [mailbox()] }), {
      'POST /api/v1/mailboxes/3/check': () => jsonResponse(mailbox()),
    })
    const item = (await screen.findByText('sender@example.com')).closest('li') as HTMLElement
    expect(within(item).getByText('connected')).toBeInTheDocument()
    expect(item).toHaveTextContent('Up to 80 recipients a day')
    expect(screen.queryByRole('button', { name: 'Connect Gmail' })).not.toBeInTheDocument()

    fireEvent.click(within(item).getByRole('button', { name: 'Check now' }))
    await waitFor(() =>
      expect(calls).toContainEqual({
        method: 'POST',
        path: '/api/v1/mailboxes/3/check',
        body: null,
      }),
    )
  })

  it('re-authorizes a mailbox that needs it, preselecting its account', async () => {
    vi.spyOn(navigation, 'assign').mockImplementation(() => {})
    const { calls } = renderSection(
      () =>
        status({
          mailboxes: [mailbox({ status: 'reauth_required', status_reason: 'invalid_grant' })],
        }),
      {
        'POST /api/v1/mailboxes/oauth/start': () =>
          jsonResponse({ authorization_url: 'https://accounts.example/auth' }),
      },
    )
    const item = (await screen.findByText('sender@example.com')).closest('li') as HTMLElement
    expect(within(item).getByText('needs re-authorizing')).toBeInTheDocument()
    expect(item).toHaveTextContent(/access was revoked/)
    fireEvent.click(within(item).getByRole('button', { name: 'Re-authorize' }))
    await waitFor(() =>
      expect(calls).toContainEqual({
        method: 'POST',
        path: '/api/v1/mailboxes/oauth/start',
        body: { mailbox_id: 3 },
      }),
    )
  })

  it('disconnects only after confirming', async () => {
    let current = status({ mailboxes: [mailbox()] })
    const { calls } = renderSection(() => current, {
      'POST /api/v1/mailboxes/3/disconnect': () => {
        current = status({
          mailboxes: [mailbox({ status: 'disabled', status_reason: 'disconnected' })],
        })
        return jsonResponse(current.mailboxes[0])
      },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('forgets sender@example.com’s token')
    expect(calls.some((call) => call.path.endsWith('/disconnect'))).toBe(false)

    fireEvent.click(within(dialog).getByRole('button', { name: 'Disconnect' }))
    expect(await screen.findByText('disconnected')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Connect Gmail' })).toBeEnabled()
  })

  it('reports the callback’s outcome', async () => {
    const { unmount } = renderSection(() => status(), {}, { gmail: 'connected' })
    expect(await screen.findByText('Gmail is connected.')).toBeInTheDocument()
    unmount()

    renderSection(() => status(), {}, { gmail: 'error', reason: 'gmail_api_refused' })
    expect(await screen.findByText(/Enable the Gmail API in the Cloud project/)).toBeInTheDocument()
  })

  it('names an unknown reason as Google’s own', async () => {
    renderSection(() => status(), {}, { gmail: 'error', reason: 'temporarily_unavailable' })
    expect(await screen.findByText(/Google answered "temporarily_unavailable"/)).toBeInTheDocument()
  })
})
