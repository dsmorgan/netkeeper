import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from './fetch'
import { renderApp } from './render'

describe('dashboard', () => {
  it('shows status, version, and the current user when the backend answers', async () => {
    const seen: Request[] = []
    mockFetch((request) => {
      seen.push(request)
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/health') {
        return jsonResponse({ status: 'ok', version: '0.0.1-test' })
      }
      if (pathname === '/api/v1/me') {
        return jsonResponse({
          id: 1,
          kind: 'local',
          display_name: 'Test User',
          email: null,
          timezone: 'UTC',
        })
      }
      return new Response('not found', { status: 404 })
    })

    await renderApp('/')
    const main = within(screen.getByRole('main'))

    expect(await main.findByText('0.0.1-test')).toBeInTheDocument()
    expect(main.getByText('ok')).toBeInTheDocument()
    expect(await main.findByText('Test User')).toBeInTheDocument()
    expect(main.getByText('UTC')).toBeInTheDocument()

    // The CSRF marker (spec 14.2) rides on every request.
    expect(seen.length).toBeGreaterThanOrEqual(2)
    for (const request of seen) {
      expect(request.headers.get('X-Netkeeper-Client')).toBe('1')
    }
  })

  it('shows a plain unreachable state, not a crash, when the backend is down', async () => {
    // The default handler rejects every fetch.
    await renderApp('/')
    const main = within(screen.getByRole('main'))

    expect(await main.findByText(/backend unreachable/i)).toBeInTheDocument()
    expect(within(screen.getByRole('banner')).getByText('Backend unreachable')).toBeInTheDocument()
    expect(main.getByText('Disconnected')).toBeInTheDocument()
  })

  it('shows the raw /me payload on the settings page', async () => {
    mockFetch((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/me') {
        return jsonResponse({
          id: 7,
          kind: 'local',
          display_name: null,
          email: null,
          timezone: 'America/New_York',
        })
      }
      return jsonResponse({ status: 'ok', version: '0.0.1-test' })
    })

    await renderApp('/settings')
    const pre = await screen.findByText(/"timezone": "America\/New_York"/)
    expect(pre.tagName).toBe('PRE')
  })
})
