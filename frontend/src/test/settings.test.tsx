import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from './fetch'
import { renderApp } from './render'

// Moved out of dashboard.test.tsx when P1-24 gave the dashboard its own setup
// path: the raw /me payload was only ever settings' own thing (spec 14.3),
// and the dashboard no longer queries /me at all.
describe('settings', () => {
  it('shows the raw /me payload', async () => {
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
      if (pathname === '/api/v1/posture') {
        return jsonResponse({
          checked_at: '2026-09-24T10:00:00Z',
          timezone: 'America/New_York',
          local_time: '2026-09-24T06:00:00-04:00',
          protections: [],
          warnings: [],
          gaps: [],
          ok: false,
          verdict: 'NOT clear: no protection is disabled, but 1 warning needs reading',
        })
      }
      if (pathname === '/api/v1/mailboxes/status') {
        return jsonResponse({
          client_configured: false,
          client_id: null,
          mailboxes: [],
          reauth_required: false,
        })
      }
      return jsonResponse({ status: 'ok', version: '0.0.1-test' })
    })

    await renderApp('/settings')
    const pre = await screen.findByText(/"timezone": "America\/New_York"/)
    expect(pre.tagName).toBe('PRE')
  })
})
