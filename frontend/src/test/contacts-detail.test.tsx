import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { contactDetail, mockApi, type SeenRequest } from './contacts-fixtures'
import { jsonResponse } from './fetch'
import { renderApp } from './render'

type Detail = ReturnType<typeof contactDetail>

/** Serves one contact, and lets a test swap what the next GET answers with. */
function serveContact(
  initial: Detail,
  handler: (request: Request, body: unknown) => Response | undefined = () => undefined,
): { seen: SeenRequest[]; set: (next: Detail) => void } {
  let current = initial
  const seen = mockApi((request, body) => {
    const { pathname } = new URL(request.url)
    const answer = handler(request, body)
    if (answer) return answer
    if (/^\/api\/v1\/contacts\/\d+$/.test(pathname) && request.method === 'GET') {
      return jsonResponse(current)
    }
    if (/^\/api\/v1\/contacts\/\d+\/tags$/.test(pathname)) return jsonResponse([])
    return undefined
  })
  return { seen, set: (next: Detail) => (current = next) }
}

describe('contact detail', () => {
  it('shows the contact, its children, tags, timeline, and snapshots', async () => {
    serveContact(
      contactDetail({
        emails: [
          {
            id: 10,
            email: 'ada@example.test',
            kind: 'work',
            is_primary: true,
            status: 'ok',
            source: 'csv',
            observed_at: '2026-01-02T09:00:00Z',
          },
        ],
        phones: [
          {
            id: 11,
            number_e164: '+15550100',
            raw: '555-0100',
            kind: 'mobile',
            is_primary: true,
            source: 'csv',
            observed_at: '2026-01-02T09:00:00Z',
          },
        ],
        links: [
          {
            id: 12,
            url: 'https://example.test/ada',
            kind: 'website',
            source: 'csv',
            observed_at: '2026-01-02T09:00:00Z',
          },
        ],
        positions: [
          {
            id: 13,
            title: 'Principal Engineer',
            company: 'Tessellate Labs',
            company_urn: null,
            started_on: '2023-01-01',
            ended_on: null,
            is_current: true,
            source: 'archive',
            observed_at: '2026-01-02T09:00:00Z',
          },
        ],
        snapshots: [
          {
            id: 14,
            contact_id: 1,
            observed_at: '2026-02-01T09:00:00Z',
            headline: 'Senior Engineer at Grebe Analytics',
            current_title: 'Senior Engineer',
            current_company: 'Grebe Analytics',
            location: 'Aberdare, Wales',
            source: 'sync',
          },
        ],
        timeline: [
          {
            kind: 'interaction',
            at: '2026-02-02T09:00:00Z',
            interaction: {
              id: 15,
              contact_id: 1,
              kind: 'note',
              at: '2026-02-02T09:00:00Z',
              summary: 'Talked about ferries',
              message_id: null,
              source: 'manual',
              created_at: '2026-02-02T09:00:00Z',
              updated_at: '2026-02-02T09:00:00Z',
            },
          },
        ],
      }),
    )
    await renderApp('/contacts/1')

    expect(await screen.findByRole('heading', { name: 'Ada Ventura' })).toBeInTheDocument()
    expect(screen.getByText('ada@example.test')).toBeInTheDocument()
    expect(screen.getByText('+15550100')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'https://example.test/ada' })).toBeInTheDocument()
    expect(screen.getAllByText('Tessellate Labs').length).toBeGreaterThan(0)
    expect(screen.getByText('Talked about ferries', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('Senior Engineer at Grebe Analytics')).toBeInTheDocument()
    expect(await screen.findByText('No tags.')).toBeInTheDocument()
  })

  it('edits a field, marks it an override, and reverts it to the synced value', async () => {
    const overridden = contactDetail({
      current_company: 'Pellucid Foods',
      field_sources: { current_company: 'manual' },
      synced_values: {
        current_company: {
          value: 'Tessellate Labs',
          source: 'sync',
          observed_at: '2026-01-02T09:00:00Z',
        },
      },
      overridden_fields: ['current_company'],
    })
    const synced = contactDetail({
      current_company: 'Tessellate Labs',
      field_sources: { current_company: 'sync' },
      synced_values: {
        current_company: {
          value: 'Tessellate Labs',
          source: 'sync',
          observed_at: '2026-01-02T09:00:00Z',
        },
      },
      overridden_fields: [],
    })

    const served = serveContact(contactDetail(), (request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/1' && request.method === 'PATCH') {
        return jsonResponse(overridden)
      }
      if (pathname === '/api/v1/contacts/1/revert-field') return jsonResponse(synced)
      return undefined
    })

    await renderApp('/contacts/1')
    await screen.findByRole('heading', { name: 'Ada Ventura' })

    fireEvent.click(screen.getByRole('button', { name: 'Edit Company' }))
    fireEvent.change(screen.getByLabelText('Company value'), {
      target: { value: 'Pellucid Foods' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save Company' }))

    // The edit sticks, and says so.
    expect(await screen.findByText('Manual override')).toBeInTheDocument()
    expect(screen.getByText(/sync last reported “Tessellate Labs”/)).toBeInTheDocument()
    const patch = served.seen.find((entry) => entry.method === 'PATCH')
    expect(patch?.body).toEqual({ current_company: 'Pellucid Foods' })

    fireEvent.click(screen.getByRole('button', { name: 'Revert Company' }))
    await waitFor(() => expect(screen.queryByText('Manual override')).toBeNull())
    const revert = served.seen.find((entry) => entry.path === '/api/v1/contacts/1/revert-field')
    expect(revert?.body).toEqual({ field: 'current_company' })
  })

  it('refuses a revert that cannot work as a disabled control, not an error after the click', async () => {
    const served = serveContact(
      contactDetail({
        headline: 'Something I typed myself',
        // Edited by hand, and no automated source ever reported it.
        field_sources: { headline: 'manual' },
        synced_values: {},
        overridden_fields: [],
      }),
    )
    await renderApp('/contacts/1')
    await screen.findByRole('heading', { name: 'Ada Ventura' })

    const revert = screen.getByRole('button', { name: 'Revert Headline' })
    expect(revert).toBeDisabled()
    expect(screen.getByText('Never synced, so there is nothing to revert to')).toBeInTheDocument()

    fireEvent.click(revert)
    expect(served.seen.some((entry) => entry.path === '/api/v1/contacts/1/revert-field')).toBe(
      false,
    )
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('says so when a merged-away id brought you to the survivor', async () => {
    serveContact(contactDetail({ id: 1, resolved_from: 77 }))
    await renderApp('/contacts/77')
    // The route asks for 77; the server answers with the survivor.
    await screen.findByRole('heading', { name: 'Ada Ventura' })
    expect(await screen.findByText(/Contact 77 was merged into this one/)).toBeInTheDocument()
  })

  it('turns a 409 on a merged-away contact into a link to the survivor', async () => {
    serveContact(contactDetail(), (request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/1/archive') {
        return jsonResponse({ detail: 'merged', merged_into_id: 42 }, 409)
      }
      return undefined
    })
    await renderApp('/contacts/1')
    await screen.findByRole('heading', { name: 'Ada Ventura' })

    fireEvent.click(screen.getByRole('button', { name: 'Archive' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('This contact was merged into another one.')
    expect(within(alert).getByRole('link', { name: 'Open contact 42' })).toHaveAttribute(
      'href',
      '/contacts/42',
    )
  })

  it('adds and removes a tag', async () => {
    let carried: Array<{
      id: number
      contact_id: number
      tag_id: number
      source: string
      rule_id: null
      created_at: string
    }> = []
    const seen = mockApi((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/1' && request.method === 'GET') {
        return jsonResponse(contactDetail())
      }
      if (pathname === '/api/v1/tags') {
        return jsonResponse([
          {
            id: 5,
            name: 'founder',
            color: null,
            kind: 'manual',
            contact_count: 0,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          },
        ])
      }
      if (pathname === '/api/v1/contacts/1/tags' && request.method === 'GET') {
        return jsonResponse(carried)
      }
      if (pathname === '/api/v1/contacts/1/tags' && request.method === 'POST') {
        carried = [
          {
            id: 99,
            contact_id: 1,
            tag_id: 5,
            source: 'manual',
            rule_id: null,
            created_at: '2026-01-01T00:00:00Z',
          },
        ]
        return jsonResponse(carried[0], 201)
      }
      if (pathname === '/api/v1/contacts/1/tags/5' && request.method === 'DELETE') {
        carried = []
        return new Response(null, { status: 204 })
      }
      return undefined
    })

    await renderApp('/contacts/1')
    await screen.findByRole('heading', { name: 'Ada Ventura' })
    expect(await screen.findByText('No tags.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Add tag/ }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'founder' }))
    expect(await screen.findByRole('button', { name: 'Remove tag founder' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Remove tag founder' }))
    await waitFor(() => expect(screen.getByText('No tags.')).toBeInTheDocument())
    expect(seen.some((entry) => entry.method === 'DELETE')).toBe(true)
  })

  it('shows a plain message when the contact cannot be loaded', async () => {
    mockApi((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/9') return jsonResponse({ detail: 'no contact 9' }, 404)
      return undefined
    })
    await renderApp('/contacts/9')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/This contact could not be loaded/)
    expect(alert).toHaveTextContent(/no contact 9/)
    expect(within(alert).getByRole('link', { name: /Back to contacts/ })).toBeInTheDocument()
  })
})
