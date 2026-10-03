import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import { BudgetPanel } from './budget-heat-panels'
import { backend, BUDGET, RISK_WARNING } from './test-support'
import type { BudgetStatus } from './types'

function renderPanel(budget: BudgetStatus) {
  mockFetch(backend({ 'GET /api/v1/linkedin/budget': () => jsonResponse(budget) }))
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <BudgetPanel />
    </QueryClientProvider>,
  )
}

describe('BudgetPanel profile-view notice (#325)', () => {
  it('shows the notice, with no risk warning, at the default limit', async () => {
    renderPanel(BUDGET)
    const note = await screen.findByRole('note', { name: 'Profile views' })
    expect(note).toHaveTextContent('Who viewed your profile')
    expect(note).toHaveTextContent(BUDGET.profile_view_notice)
    expect(screen.queryByRole('note', { name: 'Profile-visit risk' })).toBeNull()
  })

  it('shows it beside the risk warning, not instead of it', async () => {
    renderPanel({ ...BUDGET, risk_warning: RISK_WARNING })
    expect(await screen.findByRole('note', { name: 'Profile views' })).toBeInTheDocument()
    expect(screen.getByRole('note', { name: 'Profile-visit risk' })).toBeInTheDocument()
  })
})

describe('BudgetPanel risk warning (#318)', () => {
  it('shows the warning the API returns for a daily limit above 100', async () => {
    renderPanel({ ...BUDGET, risk_warning: RISK_WARNING })
    expect(await screen.findByRole('note', { name: 'Profile-visit risk' })).toHaveTextContent(
      RISK_WARNING,
    )
  })

  it('shows no warning when the API returns none', async () => {
    renderPanel(BUDGET)
    expect(await screen.findByText('Left today')).toBeInTheDocument()
    expect(screen.queryByRole('note', { name: 'Profile-visit risk' })).toBeNull()
  })
})
