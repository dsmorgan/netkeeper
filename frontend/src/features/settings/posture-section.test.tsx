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
    { name: 'browser mode', status: 'on', value: 'attach only', warnings: [], notes: [] },
    {
      name: 'linkedin session',
      status: 'unknown',
      value: 'not probed',
      warnings: ['no browser probe was run, so the session is unknown'],
      notes: [],
    },
  ],
  warnings: ['linkedin session: no browser probe was run, so the session is unknown'],
  notes: [],
  gaps: ['two budgets are not enforced by any running code today'],
  ok: false,
  verdict: 'NOT clear: no protection is disabled, but 1 warning needs reading',
}

describe('PostureSection', () => {
  it('shows every protection with its state and detail, in the >= sm table', async () => {
    renderSection(CLEAN)
    const table = await screen.findByTestId('posture-table')

    const browserRow = within(table).getByText('browser mode').closest('tr') as HTMLElement
    expect(within(browserRow).getByText('on')).toBeInTheDocument()
    expect(within(browserRow).getByText('attach only')).toBeInTheDocument()

    const sessionRow = within(table).getByText('linkedin session').closest('tr') as HTMLElement
    expect(within(sessionRow).getByText('unknown')).toBeInTheDocument()
    expect(
      within(sessionRow).getByText('no browser probe was run, so the session is unknown'),
    ).toBeInTheDocument()
  })

  it('shows every protection with its state and detail, in the below-sm stacked blocks too (M2)', async () => {
    renderSection(CLEAN)
    const blocks = await screen.findByTestId('posture-blocks')

    const browserBlock = within(blocks).getByText('browser mode').closest('li') as HTMLElement
    expect(within(browserBlock).getByText('on')).toBeInTheDocument()
    expect(within(browserBlock).getByText('attach only')).toBeInTheDocument()

    const sessionBlock = within(blocks).getByText('linkedin session').closest('li') as HTMLElement
    expect(within(sessionBlock).getByText('unknown')).toBeInTheDocument()
    expect(
      within(sessionBlock).getByText('no browser probe was run, so the session is unknown'),
    ).toBeInTheDocument()
  })

  it('shows the profile-visit risk as a note under the profile_visits budget, verdict clear (#318)', async () => {
    const risk =
      'Profile visits are set to 150 a day, above the 100 a day netkeeper was designed around.' +
      ' More visits a day make it more likely that LinkedIn restricts your account or asks you' +
      ' to verify it. Heat still slows runs down after LinkedIn throttles a visit.'
    renderSection({
      ...CLEAN,
      protections: [
        ...CLEAN.protections,
        {
          name: 'budget profile_visits',
          status: 'on',
          value: '0/150 today, 0/750 this week (hard max 250/day, 1250/week)',
          warnings: [],
          notes: [risk],
        },
      ],
      warnings: [],
      notes: [`budget profile_visits: ${risk}`],
      ok: true,
      verdict: 'nothing is misconfigured: 3 protections, none of them disabled',
    })
    const table = await screen.findByTestId('posture-table')
    const row = within(table).getByText('budget profile_visits').closest('tr') as HTMLElement
    const notes = within(row).getByRole('list', { name: 'Notes' })
    expect(within(notes).getByText(risk)).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/^nothing is misconfigured/)
  })

  it('renders markdown in a gap, not literal asterisks or backticks (review179r2)', async () => {
    renderSection({
      ...CLEAN,
      gaps: [
        '**this report reads configuration and counters, never callers.** Run `netkeeper posture` for the terminal version.',
      ],
    })
    const bold = await screen.findByText(
      'this report reads configuration and counters, never callers.',
    )
    expect(bold.tagName).toBe('STRONG')
    expect(screen.getByText('netkeeper posture').tagName).toBe('CODE')
    expect(screen.queryByText(/\*\*/)).not.toBeInTheDocument()
    expect(screen.queryByText(/`netkeeper/)).not.toBeInTheDocument()
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
      await screen.findByText('NOT clear: no protection is disabled, but 1 warning needs reading'),
    ).toBeInTheDocument()
  })

  it('never paraphrases the verdict as "you are safe"', async () => {
    renderSection(CLEAN)
    await screen.findByText(/NOT clear/)
    expect(screen.queryByText(/you are safe/i)).not.toBeInTheDocument()
  })

  it('shows a clean report’s own verdict wording too', async () => {
    renderSection({
      ...CLEAN,
      ok: true,
      warnings: [],
      verdict: 'nothing is misconfigured: 12 protections, none of them disabled',
    })
    expect(
      await screen.findByText('nothing is misconfigured: 12 protections, none of them disabled'),
    ).toBeInTheDocument()
  })

  it('shows an error state', async () => {
    renderSection('error')
    expect(await screen.findByRole('alert')).toHaveTextContent('boom')
  })
})
