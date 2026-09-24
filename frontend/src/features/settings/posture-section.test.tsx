import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import { PostureSection } from './posture-section'
import type { Posture } from './api'

function renderSection(posture: Posture | 'error') {
  mockFetch((request) => {
    const { pathname } = new URL(request.url)
    if (pathname === '/api/v1/posture') {
      if (posture === 'error') return jsonResponse({ detail: 'boom' }, 500)
      return jsonResponse(posture)
    }
    return jsonResponse({ status: 'ok', version: '0.0.1-test' })
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <PostureSection />
    </QueryClientProvider>,
  )
}

const CLEAN: Posture = {
  checked_at: '2026-09-24T10:00:00Z',
  timezone: 'America/New_York',
  local_time: '2026-09-24T06:00:00-04:00',
  protections: [
    { name: 'browser mode', status: 'on', value: 'attach only', warnings: [] },
    {
      name: 'linkedin session',
      status: 'unknown',
      value: 'not probed',
      warnings: ['no browser probe was run, so the session is unknown'],
    },
  ],
  warnings: ['linkedin session: no browser probe was run, so the session is unknown'],
  gaps: ['two budgets are not enforced by any running code today'],
  ok: false,
  verdict: 'NOT clear: no protection is disabled, but 1 warning needs reading',
}

describe('PostureSection', () => {
  it('shows every protection with its state and detail', async () => {
    renderSection(CLEAN)

    const browserRow = (await screen.findByText('browser mode')).closest('tr')
    expect(browserRow).not.toBeNull()
    expect(within(browserRow as HTMLElement).getByText('on')).toBeInTheDocument()
    expect(within(browserRow as HTMLElement).getByText('attach only')).toBeInTheDocument()

    const sessionRow = screen.getByText('linkedin session').closest('tr') as HTMLElement
    expect(within(sessionRow).getByText('unknown')).toBeInTheDocument()
    expect(
      within(sessionRow).getByText('no browser probe was run, so the session is unknown'),
    ).toBeInTheDocument()
  })

  it('shows the gaps list', async () => {
    renderSection(CLEAN)
    expect(await screen.findByText('Not covered by this report')).toBeInTheDocument()
    expect(
      screen.getByText('two budgets are not enforced by any running code today'),
    ).toBeInTheDocument()
  })

  it('shows the verdict verbatim, exactly as the backend sends it', async () => {
    renderSection(CLEAN)
    expect(
      await screen.findByText(
        'NOT clear: no protection is disabled, but 1 warning needs reading',
      ),
    ).toBeInTheDocument()
  })

  it('never paraphrases the verdict as "you are safe"', async () => {
    renderSection(CLEAN)
    await screen.findByText(/NOT clear/)
    expect(screen.queryByText(/you are safe/i)).not.toBeInTheDocument()
  })

  it('shows a clean report’s own verdict wording too', async () => {
    renderSection({ ...CLEAN, ok: true, warnings: [], verdict: 'nothing is misconfigured: 12 protections, none of them disabled' })
    expect(
      await screen.findByText('nothing is misconfigured: 12 protections, none of them disabled'),
    ).toBeInTheDocument()
  })

  it('shows an error state', async () => {
    renderSection('error')
    expect(await screen.findByRole('alert')).toHaveTextContent('boom')
  })
})
