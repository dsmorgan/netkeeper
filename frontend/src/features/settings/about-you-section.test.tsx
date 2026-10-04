import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import { AboutYouSection } from './about-you-section'
import type { SelfContact, SelfContactIn } from './api'

const CURRENT: SelfContact = {
  first_name: 'Ada',
  last_name: 'Fixture',
  current_company: 'Example Co',
  current_title: '',
  location: '',
  exists: true,
}

function renderSection(current: SelfContact = CURRENT) {
  const puts: SelfContactIn[] = []
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url)
    if (pathname !== '/api/v1/settings/self-contact') return jsonResponse({}, 500)
    if (request.method === 'GET') return jsonResponse(current)
    expect(request.method).toBe('PUT')
    expect(request.headers.get('X-Netkeeper-Client')).not.toBeNull() // the CSRF header
    const body = (await request.json()) as SelfContactIn
    puts.push(body)
    return jsonResponse({ ...current, ...body, exists: true })
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AboutYouSection />
    </QueryClientProvider>,
  )
  return { puts }
}

describe('AboutYouSection', () => {
  it('shows your details and the merge field each one fills in a test send', async () => {
    renderSection()
    const form = within(await screen.findByRole('form', { name: 'About you' }))
    expect(form.getByLabelText('First name')).toHaveValue('Ada')
    expect(form.getByLabelText('Company')).toHaveValue('Example Co')
    expect(form.getByLabelText('Title')).toHaveValue('')
    expect(form.getByLabelText('First name')).toHaveAccessibleDescription(
      'Fills {{ first_name }} in a test send',
    )
    expect(screen.getByText(/never in your contacts or a campaign/)).toBeVisible()
  })

  it('saves every field', async () => {
    const { puts } = renderSection()
    const form = within(await screen.findByRole('form', { name: 'About you' }))
    fireEvent.change(form.getByLabelText('Title'), { target: { value: 'Engineer' } })
    fireEvent.change(form.getByLabelText('First name'), { target: { value: 'Augusta' } })
    fireEvent.click(form.getByRole('button', { name: 'Save your details' }))
    await waitFor(() => expect(puts).toHaveLength(1))
    expect(puts[0]).toEqual({
      first_name: 'Augusta',
      last_name: 'Fixture',
      current_company: 'Example Co',
      current_title: 'Engineer',
      location: '',
    })
    expect(await screen.findByRole('status')).toHaveTextContent('Saved.')
  })
})
