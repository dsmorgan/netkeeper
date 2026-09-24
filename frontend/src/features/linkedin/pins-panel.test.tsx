import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import { PinsPanel } from './pins-panel'
import { backend, FIVE_PINS, PINS, type Call, type Handler } from './test-support'

function renderPanel(handlers: Record<string, Handler> = {}, calls: Call[] = []) {
  mockFetch(backend({ 'GET /api/v1/linkedin/pins': () => jsonResponse(PINS), ...handlers }, calls))
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <PinsPanel />
    </QueryClientProvider>,
  )
  return { calls }
}

describe('PinsPanel', () => {
  it('shows how many of the 5 are pinned', async () => {
    renderPanel()
    expect(await screen.findByText('2 of 5 pinned')).toBeInTheDocument()
    expect(screen.getByText('Rosalind Quillfeather')).toBeInTheDocument()
  })

  it('caps at 5: the add form disappears and says to unpin one first', async () => {
    renderPanel({ 'GET /api/v1/linkedin/pins': () => jsonResponse(FIVE_PINS) })
    expect(await screen.findByText('5 of 5 pinned')).toBeInTheDocument()
    expect(screen.queryByLabelText('Add a contact')).not.toBeInTheDocument()
    expect(screen.getByText('5 are already pinned; unpin one first.')).toBeInTheDocument()
  })

  it('searches and pins a contact, updating the list from the response', async () => {
    const { calls } = renderPanel({
      'POST /api/v1/contacts/query': (call) => {
        const body = call.body as { filter: unknown }
        expect(body.filter).toBeTruthy()
        return jsonResponse({
          items: [
            {
              id: 9,
              first_name: 'Hortensia',
              last_name: 'Blennerhassett',
              preferred_name: null,
              headline: null,
              current_title: null,
              current_company: null,
              location: null,
              connected_on: null,
              met: 'unknown',
              do_not_contact: false,
              archived_at: null,
              li_url: null,
              last_contacted_at: null,
              primary_email: null,
              primary_phone: null,
            },
          ],
          total: 1,
          describe: 'one match',
        })
      },
      'POST /api/v1/linkedin/pins': (call) => {
        expect(call.body).toEqual({ contact_id: 9 })
        return jsonResponse([
          ...PINS,
          { contact_id: 9, first_name: 'Hortensia', last_name: 'Blennerhassett' },
        ])
      },
    })
    await screen.findByText('2 of 5 pinned')

    fireEvent.change(screen.getByLabelText('Add a contact'), { target: { value: 'Hortensia' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Pin' }))

    expect(await screen.findByText('3 of 5 pinned')).toBeInTheDocument()
    expect(calls.some((call) => call.path === '/api/v1/linkedin/pins')).toBe(true)
  })

  it('unpins a contact', async () => {
    const { calls } = renderPanel({
      'DELETE /api/v1/linkedin/pins/1': () => jsonResponse(PINS.slice(1)),
    })
    await screen.findByText('2 of 5 pinned')

    fireEvent.click(screen.getByRole('button', { name: 'Unpin Rosalind Quillfeather' }))

    await waitFor(() => expect(screen.getByText('1 of 5 pinned')).toBeInTheDocument())
    expect(
      calls.some((call) => call.method === 'DELETE' && call.path === '/api/v1/linkedin/pins/1'),
    ).toBe(true)
  })
})
