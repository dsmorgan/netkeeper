import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { resetFakeEventSource } from '@/test/fake-event-source'
import { jsonResponse } from '@/test/fetch'

import { navigation } from './api'
import { ReauthBanner } from './reauth-banner'
import { mailbox, renderWithBackend, status } from './test-support'

afterEach(() => {
  resetFakeEventSource()
  vi.restoreAllMocks()
})

describe('ReauthBanner', () => {
  it('shows nothing while every mailbox is healthy', async () => {
    const { calls } = renderWithBackend(<ReauthBanner />, () => status({ mailboxes: [mailbox()] }))
    await waitFor(() => expect(calls).toHaveLength(1))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('says which mailbox, why, and that email steps are paused', async () => {
    renderWithBackend(<ReauthBanner />, () =>
      status({
        mailboxes: [mailbox({ status: 'reauth_required', status_reason: 'invalid_grant' })],
      }),
    )
    const banner = await screen.findByRole('alert')
    expect(within(banner).getByText('Gmail needs you to sign in again')).toBeInTheDocument()
    expect(banner).toHaveTextContent('sender@example.com')
    expect(banner).toHaveTextContent(/revoked, or the consent screen is still in Testing/)
    expect(banner).toHaveTextContent('Email steps are paused until it is authorized again.')
  })

  it('explains a client Google no longer lets use the token', async () => {
    renderWithBackend(<ReauthBanner />, () =>
      status({
        mailboxes: [mailbox({ status: 'reauth_required', status_reason: 'unauthorized_client' })],
      }),
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(/Save a Desktop app client/)
  })

  it.each([
    ['insufficientPermissions', /does not include Gmail access/],
    ['http_403', /Gmail refused the token/],
  ])('explains the Gmail API refusing the token (%s)', async (reason, text) => {
    renderWithBackend(<ReauthBanner />, () =>
      status({ mailboxes: [mailbox({ status: 'reauth_required', status_reason: reason })] }),
    )
    const banner = await screen.findByRole('alert')
    expect(banner).toHaveTextContent(text)
    expect(banner).not.toHaveTextContent('Google answered')
  })

  it('shows while the Keychain is locked, never asking for the status', async () => {
    const { calls } = renderWithBackend(
      <ReauthBanner />,
      () => status({ mailboxes: [mailbox({ status: 'reauth_required' })] }),
      {
        'GET /api/v1/mailboxes/status': () =>
          jsonResponse({ detail: 'the Keychain is locked' }, 503),
      },
    )
    expect(await screen.findByRole('alert')).toHaveTextContent('sender@example.com')
    expect(calls.map((call) => `${call.method} ${call.path}`)).toEqual(['GET /api/v1/mailboxes'])
  })

  it('re-authorizes that mailbox on Google’s page', async () => {
    const assign = vi.spyOn(navigation, 'assign').mockImplementation(() => {})
    const { calls } = renderWithBackend(
      <ReauthBanner />,
      () => status({ mailboxes: [mailbox({ status: 'reauth_required' })] }),
      {
        'POST /api/v1/mailboxes/oauth/start': () =>
          jsonResponse({ authorization_url: 'https://accounts.example/auth?x=1' }),
      },
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Re-authorize' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://accounts.example/auth?x=1'))
    expect(calls).toContainEqual({
      method: 'POST',
      path: '/api/v1/mailboxes/oauth/start',
      body: { mailbox_id: 3 },
    })
  })

  it('appears when the poll marks a mailbox, without a reload', async () => {
    let current = status({ mailboxes: [mailbox()] })
    const { calls, source } = renderWithBackend(<ReauthBanner />, () => current)
    await waitFor(() => expect(calls).toHaveLength(1))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    current = status({
      mailboxes: [mailbox({ status: 'reauth_required', status_reason: 'invalid_grant' })],
    })
    act(() => {
      source.emit('mailbox.status', {
        mailbox_id: 3,
        status: 'reauth_required',
        reason: 'invalid_grant',
      })
    })
    expect(await screen.findByRole('alert')).toHaveTextContent('sender@example.com')
  })
})
