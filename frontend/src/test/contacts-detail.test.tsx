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

  it('renders a message summary in the timeline as text, whatever it contains (issue #75)', async () => {
    // Someone else wrote this. The archive importer strips only the tags
    // LinkedIn's own editor emits and keeps any other bracketed text verbatim,
    // so a summary can still arrive tag-shaped: the timeline has to escape it.
    const body = 'See <img src=x onerror="alert(1)"> and <script>alert(2)</script> soon'
    serveContact(
      contactDetail({
        timeline: [
          {
            kind: 'interaction',
            at: '2026-02-02T09:00:00Z',
            interaction: {
              id: 15,
              contact_id: 1,
              kind: 'li_in',
              at: '2026-02-02T09:00:00Z',
              summary: body,
              message_id: null,
              source: 'archive',
              created_at: '2026-02-02T09:00:00Z',
              updated_at: '2026-02-02T09:00:00Z',
            },
          },
        ],
      }),
    )
    const { container } = await renderApp('/contacts/1')

    const entry = (await screen.findByText(body, { exact: false })).closest('li')
    expect(entry).not.toBeNull()
    expect(entry).toHaveTextContent(body)
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
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

  it('puts its cards in the outline under the contact’s name', async () => {
    serveContact(contactDetail())
    await renderApp('/contacts/1')
    await screen.findByRole('heading', { name: 'Ada Ventura', level: 2 })
    const main = within(screen.getByRole('main'))
    const cards = main.getAllByRole('heading', { level: 3 }).map((node) => node.textContent)
    expect(cards).toEqual(
      expect.arrayContaining(['Fields', 'Tags', 'Record', 'Notes', 'Contact details', 'Timeline']),
    )
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

  it('shows a merged 409 as a link from every field editor, not as raw text', async () => {
    // The field, met, do-not-contact and notes editors used to print the error
    // message, so a merge in another tab read as "save: merged" (#88).
    serveContact(contactDetail(), (request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/1' && request.method === 'PATCH') {
        return jsonResponse({ detail: 'merged', merged_into_id: 42 }, 409)
      }
      if (pathname === '/api/v1/contacts/1/notes') {
        return jsonResponse({ detail: 'merged', merged_into_id: 42 }, 409)
      }
      return undefined
    })
    await renderApp('/contacts/1')
    await screen.findByRole('heading', { name: 'Ada Ventura' })

    const expectSurvivorLink = async () => {
      const alerts = await screen.findAllByRole('alert')
      const alert = alerts[alerts.length - 1] as HTMLElement
      expect(alert).toHaveTextContent('This contact was merged into another one.')
      expect(alert).not.toHaveTextContent('save:')
      expect(within(alert).getByRole('link', { name: 'Open contact 42' })).toHaveAttribute(
        'href',
        '/contacts/42',
      )
    }

    fireEvent.click(screen.getByRole('button', { name: 'Edit Headline' }))
    fireEvent.change(screen.getByLabelText('Headline value'), { target: { value: 'Rigger' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save Headline' }))
    await expectSurvivorLink()

    fireEvent.click(
      within(screen.getByRole('group', { name: 'Met' })).getByRole('button', { name: 'Met' }),
    )
    await waitFor(() => expect(screen.getAllByRole('alert')).toHaveLength(2))
    await expectSurvivorLink()

    fireEvent.change(screen.getByLabelText('Notes'), { target: { value: 'Met at the fair.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save notes' }))
    await waitFor(() => expect(screen.getAllByRole('alert')).toHaveLength(3))
    await expectSurvivorLink()
  })

  it('sets met, not met, and clear from the contact page (issue #322)', async () => {
    let current = contactDetail()
    const { seen } = serveContact(current, (request, body) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/1' && request.method === 'PATCH') {
        const { met } = body as { met: 'met' | 'not_met' | 'unknown' }
        current = {
          ...current,
          met,
          met_source: 'manual',
          triaged_at: met === 'unknown' ? null : '2026-09-20T12:00:00Z',
        }
        return jsonResponse(current)
      }
      if (pathname === '/api/v1/contacts/1' && request.method === 'GET') {
        return jsonResponse(current)
      }
      return undefined
    })
    await renderApp('/contacts/1')
    await screen.findByRole('heading', { name: 'Ada Ventura' })
    const group = screen.getByRole('group', { name: 'Met' })
    const button = (name: string) => within(group).getByRole('button', { name })

    expect(button('Clear')).toBeDisabled()
    expect(screen.getByText('Not triaged yet.')).toBeInTheDocument()

    fireEvent.click(button('Met'))
    await waitFor(() => expect(button('Met')).toHaveAttribute('aria-pressed', 'true'))
    expect(screen.getByText('Set by you.')).toBeInTheDocument()

    fireEvent.click(button('Not met'))
    await waitFor(() => expect(button('Not met')).toHaveAttribute('aria-pressed', 'true'))
    expect(button('Met')).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(button('Clear'))
    await waitFor(() => expect(button('Clear')).toBeDisabled())
    expect(screen.getByText('Not triaged yet.')).toBeInTheDocument()

    const patches = seen.filter((entry) => entry.method === 'PATCH').map((entry) => entry.body)
    expect(patches).toEqual([{ met: 'met' }, { met: 'not_met' }, { met: 'unknown' }])
  })

  it('disables the Met control for an archived contact and says why', async () => {
    serveContact(contactDetail({ archived_at: '2026-03-01T00:00:00Z' }))
    await renderApp('/contacts/1')
    await screen.findByRole('heading', { name: 'Ada Ventura' })
    const group = screen.getByRole('group', { name: 'Met' })
    expect(within(group).getByRole('button', { name: 'Met' })).toBeDisabled()
    expect(within(group).getByRole('button', { name: 'Not met' })).toBeDisabled()
    expect(screen.getByText('Unarchive this contact to change Met.')).toBeInTheDocument()
  })

  it('puts the Met control above the text fields', async () => {
    serveContact(contactDetail())
    await renderApp('/contacts/1')
    await screen.findByRole('heading', { name: 'Ada Ventura' })
    const group = screen.getByRole('group', { name: 'Met' })
    const firstField = screen.getByRole('button', { name: 'Edit Preferred name' })
    expect(
      group.compareDocumentPosition(firstField) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('says when netkeeper suggested the answer and it is waiting for review', async () => {
    serveContact(contactDetail({ met: 'met', met_source: 'automatic' }))
    await renderApp('/contacts/1')
    await screen.findByRole('heading', { name: 'Ada Ventura' })
    expect(screen.getByText('Suggested by netkeeper, waiting for your review.')).toBeInTheDocument()
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

  it('renders only an http or https link as an href, anything else as plain text', async () => {
    const unsafe = [
      'javascript:alert(1)',
      ' JavaScript:alert(1)',
      'data:text/html;base64,PHNjcmlwdD4=',
      'vbscript:msgbox(1)',
      '//evil.example.test/x',
    ]
    const links = ['https://example.test/ada', ...unsafe].map((url, index) => ({
      id: 100 + index,
      url,
      kind: 'website' as const,
      source: 'csv' as const,
      observed_at: '2026-01-02T09:00:00Z',
    }))
    serveContact(contactDetail({ links }))
    await renderApp('/contacts/1')

    expect(await screen.findByRole('link', { name: 'https://example.test/ada' })).toHaveAttribute(
      'href',
      'https://example.test/ada',
    )
    for (const url of unsafe) {
      const shown = screen.getByText(url.trim())
      expect(shown.closest('a')).toBeNull()
      expect(screen.queryByRole('link', { name: url.trim() })).toBeNull()
    }
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

  it('offers confirm and reject for a contact read off a card, and says what each did', async () => {
    const waiting = contactDetail({
      li_urn: null,
      needs_review_at: '2026-09-24T12:00:00Z',
    })
    const { seen, set } = serveContact(waiting, (request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/1/reject' && request.method === 'POST') {
        const rejected = { ...waiting, archived_at: '2026-09-24T12:05:00Z' }
        set(rejected)
        return jsonResponse(rejected)
      }
      if (pathname === '/api/v1/contacts/1/confirm' && request.method === 'POST') {
        const confirmed = { ...waiting, archived_at: '2026-09-24T12:05:00Z', needs_review_at: null }
        set(confirmed)
        return jsonResponse(confirmed)
      }
      return undefined
    })
    await renderApp('/contacts/1')
    await screen.findByRole('heading', { name: 'Ada Ventura' })

    const notice = screen.getByRole('region', { name: 'Needs review' })
    expect(notice).toHaveTextContent(/won.t enrich it, add it to a campaign, or count it/)
    expect(screen.getAllByTestId('needs-review-badge')).toHaveLength(1)

    fireEvent.click(within(notice).getByRole('button', { name: /Reject Ada Ventura/ }))
    await waitFor(() =>
      expect(
        seen.some((entry) => entry.path === '/api/v1/contacts/1/reject' && entry.method === 'POST'),
      ).toBe(true),
    )
    // Rejected is archived, not deleted, and still waiting: confirm stays offered.
    expect(await screen.findByText(/Rejected: it.s archived, not deleted/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Reject Ada Ventura/ })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Confirm Ada Ventura' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Needs review' })).toBeNull())
    expect(screen.queryByTestId('needs-review-badge')).toBeNull()
  })

  it('shows no review notice for an ordinary contact', async () => {
    serveContact(contactDetail())
    await renderApp('/contacts/1')
    await screen.findByRole('heading', { name: 'Ada Ventura' })
    expect(screen.queryByRole('region', { name: 'Needs review' })).toBeNull()
    expect(screen.queryByTestId('needs-review-badge')).toBeNull()
  })
})
