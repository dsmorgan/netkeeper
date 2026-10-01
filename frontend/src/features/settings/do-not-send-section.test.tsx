import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse, mockFetch } from '@/test/fetch'

import type { DoNotSendEntry } from './api'
import { DoNotSendSection } from './do-not-send-section'

const ENTRIES: DoNotSendEntry[] = [
  {
    id: 2,
    email: 'name+nk1@example.test',
    reason: 'opted_out',
    contact_id: 7,
    created_at: '2026-09-30T10:00:00Z',
  },
  {
    id: 1,
    email: 'ada@example.test',
    reason: 'bounced',
    contact_id: null,
    created_at: '2026-09-29T10:00:00Z',
  },
]

function renderSection(initial: DoNotSendEntry[]) {
  let entries = [...initial]
  const deleted: number[] = []
  mockFetch((request) => {
    const { pathname } = new URL(request.url)
    if (pathname === '/api/v1/do-not-send' && request.method === 'GET') return jsonResponse(entries)
    const match = /^\/api\/v1\/do-not-send\/(\d+)$/.exec(pathname)
    if (match && request.method === 'DELETE') {
      const id = Number(match[1])
      deleted.push(id)
      entries = entries.filter((e) => e.id !== id)
      return new Response(null, { status: 204 })
    }
    return jsonResponse({ detail: 'unexpected' }, 500)
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <DoNotSendSection />
    </QueryClientProvider>,
  )
  return { deleted }
}

describe('DoNotSendSection', () => {
  it('lists each address with why it is there', async () => {
    renderSection(ENTRIES)
    expect(await screen.findByText('name+nk1@example.test')).toBeInTheDocument()
    expect(screen.getByText('opted out')).toBeInTheDocument()
    expect(screen.getByText('ada@example.test')).toBeInTheDocument()
    expect(screen.getByText('bounced')).toBeInTheDocument()
  })

  it('says so when the list is empty', async () => {
    renderSection([])
    expect(await screen.findByText('No addresses on the list.')).toBeInTheDocument()
  })

  it('removes an address only after the person confirms', async () => {
    const { deleted } = renderSection(ENTRIES)
    fireEvent.click(await screen.findByRole('button', { name: 'Remove ada@example.test' }))
    expect(deleted).toEqual([])
    expect(
      await screen.findByText(/Campaigns may send to ada@example.test again/),
    ).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(deleted).toEqual([1]))
    await waitFor(() => expect(screen.queryByText('ada@example.test')).not.toBeInTheDocument())
    expect(screen.getByText('name+nk1@example.test')).toBeInTheDocument()
  })
})
