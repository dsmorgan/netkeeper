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
