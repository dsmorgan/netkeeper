/**
 * Tags, and the rule editor's live preview.
 *
 * The preview is where the backend's two guards become visible: a pattern it
 * refuses comes back as a sentence to show, and a pattern it ran comes back
 * with a count and the number of searches that ran out of time.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse } from '@/test/fetch'

import { mockApi, renderWithClient, requestsTo, type RouteHandler } from './harness'
import { TagsPanel } from './tags-panel'

const TAG = {
  id: 1,
  name: 'founder',
  color: null,
  kind: 'manual',
  contact_count: 12,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const RULE = {
  id: 5,
  tag_id: 1,
  field: 'title',
  pattern: 'founder',
  enabled: true,
  position: 0,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

function baseRoutes(overrides: Record<string, () => Response> = {}) {
  return {
    'GET /api/v1/tags': () => jsonResponse([TAG]),
    'GET /api/v1/autotag-rules': () => jsonResponse([RULE]),
    ...overrides,
  }
}

describe('tags', () => {
  it('lists them with their contact counts', async () => {
    mockApi(baseRoutes())
    renderWithClient(<TagsPanel />)
    expect(await screen.findByRole('button', { name: 'Delete founder' })).toBeInTheDocument()
    expect(screen.getByText('12 contacts')).toBeInTheDocument()
  })

  it('creates one and reloads the list', async () => {
    const seen = mockApi(
      baseRoutes({
        'POST /api/v1/tags': () => jsonResponse({ ...TAG, id: 2, name: 'investor' }, 201),
      }),
    )
    renderWithClient(<TagsPanel />)
    await screen.findByRole('button', { name: 'Delete founder' })

    fireEvent.change(screen.getByLabelText('New tag'), { target: { value: 'investor' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add tag' }))

    await waitFor(() => {
      expect(requestsTo(seen, 'POST', '/api/v1/tags')[0]?.body).toEqual({
        kind: 'manual',
        color: null,
        name: 'investor',
      })
    })
  })

  it('shows the server’s reason when a name is refused', async () => {
    mockApi(
      baseRoutes({
        'POST /api/v1/tags': () =>
          jsonResponse({ detail: 'a tag named "founder" already exists' }, 409),
      }),
    )
    renderWithClient(<TagsPanel />)
    await screen.findByRole('button', { name: 'Delete founder' })

    fireEvent.change(screen.getByLabelText('New tag'), { target: { value: 'Founder' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add tag' }))

    expect(await screen.findByText(/already exists/)).toBeInTheDocument()
  })

  it('says so plainly when there are none', async () => {
    mockApi(baseRoutes({ 'GET /api/v1/tags': () => jsonResponse([]) }))
    renderWithClient(<TagsPanel />)
    expect(await screen.findByText('No tags yet')).toBeInTheDocument()
  })

  it('reports a failed load instead of an empty screen', async () => {
    mockApi(
      baseRoutes({ 'GET /api/v1/tags': () => jsonResponse({ detail: 'database is locked' }, 500) }),
    )
    renderWithClient(<TagsPanel />)
    expect(await screen.findByText('database is locked')).toBeInTheDocument()
  })

  it('shows a loading state before the first answer', () => {
    mockApi(baseRoutes({ 'GET /api/v1/tags': () => new Promise<Response>(() => {}) as never }))
    renderWithClient(<TagsPanel />)
    expect(screen.getByText('Loading tags…')).toBeInTheDocument()
  })
})

describe('the rule editor', () => {
  it('counts the matches as the pattern changes', async () => {
    let count = 214
    mockApi(
      baseRoutes({
        'POST /api/v1/autotag-rules/preview': () =>
          jsonResponse({ count, contact_ids: [], timeouts: 0 }),
      }),
    )
    renderWithClient(<TagsPanel />)
    await screen.findByRole('button', { name: 'Delete founder' })

    fireEvent.change(screen.getByLabelText('Pattern'), { target: { value: 'founder' } })
    expect(await screen.findByText('214')).toBeInTheDocument()

    count = 3
    fireEvent.change(screen.getByLabelText('Pattern'), { target: { value: 'chief' } })
    expect(await screen.findByText('3')).toBeInTheDocument()
  })

  it('renders the reason a nested unbounded repeat is refused', async () => {
    mockApi(
      baseRoutes({
        'POST /api/v1/autotag-rules/preview': () =>
          jsonResponse(
            {
              detail:
                'pattern may run slowly: an unbounded repeat (+, *, {n,}) inside another one, ' +
                'as in (a+)+, can take exponential time; rewrite it without the nesting',
            },
            422,
          ),
      }),
    )
    renderWithClient(<TagsPanel />)
    await screen.findByRole('button', { name: 'Delete founder' })

    fireEvent.change(screen.getByLabelText('Pattern'), { target: { value: '(a+)+' } })

    expect(await screen.findByText(/rewrite it without the nesting/)).toBeInTheDocument()
    expect(screen.getByText('The server refused this pattern')).toBeInTheDocument()
  })

  it("drops pydantic's 'Value error, ' from a schema guard's refusal", async () => {
    mockApi(
      baseRoutes({
        'POST /api/v1/autotag-rules/preview': () =>
          jsonResponse(
            {
              detail: [
                {
                  type: 'value_error',
                  loc: ['body', 'pattern'],
                  msg: 'Value error, pattern may run slowly: an unbounded repeat inside another one',
                },
              ],
            },
            422,
          ),
      }),
    )
    renderWithClient(<TagsPanel />)
    await screen.findByRole('button', { name: 'Delete founder' })

    fireEvent.change(screen.getByLabelText('Pattern'), { target: { value: '(a+)+' } })

    const reason = await screen.findByText(/^pattern may run slowly/)
    expect(reason).toHaveTextContent(
      'pattern may run slowly: an unbounded repeat inside another one',
    )
    expect(screen.queryByText(/Value error/)).toBeNull()
  })

  it('renders the reason counted repeats are refused', async () => {
    mockApi(
      baseRoutes({
        'POST /api/v1/autotag-rules/preview': () =>
          jsonResponse(
            {
              detail:
                'pattern may use too much memory: nested counted repeats multiply out to more ' +
                'than 1000 copies, as in (?:a{100}){100}; lower the counts',
            },
            422,
          ),
      }),
    )
    renderWithClient(<TagsPanel />)
    await screen.findByRole('button', { name: 'Delete founder' })

    fireEvent.change(screen.getByLabelText('Pattern'), {
      target: { value: '(?:(?:a{100}){100}){100}' },
    })

    expect(await screen.findByText(/lower the counts/)).toBeInTheDocument()
  })

  it('shows the timeouts, because the count understates without them', async () => {
    mockApi(
      baseRoutes({
        'POST /api/v1/autotag-rules/preview': () =>
          jsonResponse({ count: 9, contact_ids: [], timeouts: 4 }),
      }),
    )
    renderWithClient(<TagsPanel />)
    await screen.findByRole('button', { name: 'Delete founder' })

    fireEvent.change(screen.getByLabelText('Pattern'), { target: { value: 'chief.*officer' } })

    expect(await screen.findByText('Some contacts were not searched in time')).toBeInTheDocument()
    expect(screen.getByText(/may understate the matches/)).toBeInTheDocument()
  })

  it('keeps the timeout note away when nothing timed out', async () => {
    mockApi(
      baseRoutes({
        'POST /api/v1/autotag-rules/preview': () =>
          jsonResponse({ count: 9, contact_ids: [], timeouts: 0 }),
      }),
    )
    renderWithClient(<TagsPanel />)
    await screen.findByRole('button', { name: 'Delete founder' })

    fireEvent.change(screen.getByLabelText('Pattern'), { target: { value: 'chief' } })
    await screen.findByText('9')
    expect(screen.queryByText('Some contacts were not searched in time')).toBeNull()
  })

  it('creates a rule with the tag, field, and pattern chosen', async () => {
    const seen = mockApi(
      baseRoutes({
        'POST /api/v1/autotag-rules/preview': () =>
          jsonResponse({ count: 1, contact_ids: [], timeouts: 0 }),
        'POST /api/v1/autotag-rules': () => jsonResponse(RULE, 201),
      }),
    )
    renderWithClient(<TagsPanel />)
    await screen.findByRole('button', { name: 'Delete founder' })

    fireEvent.change(screen.getByLabelText('Field'), { target: { value: 'headline' } })
    fireEvent.change(screen.getByLabelText('Pattern'), { target: { value: '\\bfounder\\b' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add rule' }))

    await waitFor(() => {
      expect(requestsTo(seen, 'POST', '/api/v1/autotag-rules')[0]?.body).toEqual({
        tag_id: 1,
        field: 'headline',
        pattern: '\\bfounder\\b',
        enabled: true,
      })
    })
  })

  it('explains what a timed-out search means after a run', async () => {
    mockApi(
      baseRoutes({
        'POST /api/v1/autotag-rules/run': () =>
          jsonResponse({ contacts: 900, added: 12, removed: 1, updated: 0, timeouts: 6 }),
      }),
    )
    renderWithClient(<TagsPanel />)
    await screen.findByRole('button', { name: 'Delete founder' })

    fireEvent.click(screen.getByRole('button', { name: 'Run all rules now' }))

    expect(await screen.findByText('Some searches timed out')).toBeInTheDocument()
    expect(screen.getByText(/adds no tag and removes none/)).toBeInTheDocument()
  })
})

describe('editing a rule', () => {
  const INVESTOR = { ...TAG, id: 2, name: 'investor' }
  const SECOND = { ...RULE, id: 6, tag_id: 2, pattern: 'angel', position: 1 }

  function routes(overrides: Record<string, RouteHandler> = {}) {
    return {
      'GET /api/v1/tags': () => jsonResponse([TAG, INVESTOR]),
      'GET /api/v1/autotag-rules': () => jsonResponse([RULE, SECOND]),
      'POST /api/v1/autotag-rules/preview': () =>
        jsonResponse({ count: 7, contact_ids: [], timeouts: 0 }),
      ...overrides,
    }
  }

  it('changes the pattern, field, and tag in place, with the live preview', async () => {
    const seen = mockApi(
      routes({
        'PATCH /api/v1/autotag-rules/5': ({ body }) =>
          jsonResponse({ ...RULE, ...(body as object) }),
      }),
    )
    renderWithClient(<TagsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit rule 5' }))

    const form = within(screen.getByRole('form', { name: 'Edit rule 5' }))
    // It opens on the rule as it stands, and previews it straight away.
    expect(form.getByLabelText('Pattern')).toHaveValue('founder')
    expect(await form.findByText('7')).toBeInTheDocument()

    fireEvent.change(form.getByLabelText('Tag'), { target: { value: '2' } })
    fireEvent.change(form.getByLabelText('Field'), { target: { value: 'headline' } })
    fireEvent.change(form.getByLabelText('Pattern'), { target: { value: ' co-?founder ' } })
    fireEvent.click(form.getByRole('button', { name: 'Save rule' }))

    await waitFor(() =>
      expect(requestsTo(seen, 'PATCH', '/api/v1/autotag-rules/5')[0]?.body).toEqual({
        tag_id: 2,
        field: 'headline',
        pattern: 'co-?founder',
        enabled: true,
      }),
    )
    // No delete-and-recreate: the rule keeps its id, its place, and its credit.
    expect(requestsTo(seen, 'DELETE', '/api/v1/autotag-rules/5')).toHaveLength(0)
    expect(requestsTo(seen, 'POST', '/api/v1/autotag-rules')).toHaveLength(0)
    await waitFor(() => expect(screen.queryByRole('form', { name: 'Edit rule 5' })).toBeNull())
    expect(requestsTo(seen, 'GET', '/api/v1/autotag-rules').length).toBeGreaterThan(1)
  })

  it('keeps a disabled rule disabled when its pattern is edited', async () => {
    const seen = mockApi(
      routes({
        'GET /api/v1/autotag-rules': () => jsonResponse([{ ...RULE, enabled: false }]),
        'PATCH /api/v1/autotag-rules/5': ({ body }) =>
          jsonResponse({ ...RULE, ...(body as object) }),
      }),
    )
    renderWithClient(<TagsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit rule 5' }))
    const form = within(screen.getByRole('form', { name: 'Edit rule 5' }))
    fireEvent.change(form.getByLabelText('Pattern'), { target: { value: 'cofounder' } })
    fireEvent.click(form.getByRole('button', { name: 'Save rule' }))
    await waitFor(() =>
      expect(requestsTo(seen, 'PATCH', '/api/v1/autotag-rules/5')[0]?.body).toMatchObject({
        enabled: false,
      }),
    )
  })

  it('stays open with the reason when the server refuses the edit', async () => {
    mockApi(
      routes({
        'PATCH /api/v1/autotag-rules/5': () =>
          jsonResponse({ detail: 'pattern is not a valid regular expression' }, 422),
      }),
    )
    renderWithClient(<TagsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit rule 5' }))
    const form = within(screen.getByRole('form', { name: 'Edit rule 5' }))
    fireEvent.change(form.getByLabelText('Pattern'), { target: { value: '(' } })
    fireEvent.click(form.getByRole('button', { name: 'Save rule' }))

    expect(await form.findByText(/not a valid regular expression/)).toBeInTheDocument()
    expect(form.getByLabelText('Pattern')).toHaveValue('(')
  })

  it('cancels without saving', async () => {
    const seen = mockApi(routes())
    renderWithClient(<TagsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit rule 5' }))
    const form = within(screen.getByRole('form', { name: 'Edit rule 5' }))
    fireEvent.change(form.getByLabelText('Pattern'), { target: { value: 'typo' } })
    fireEvent.click(form.getByRole('button', { name: 'Cancel' }))

    expect(screen.queryByRole('form', { name: 'Edit rule 5' })).toBeNull()
    expect(screen.getByText('founder', { selector: 'code' })).toBeInTheDocument()
    expect(requestsTo(seen, 'PATCH', '/api/v1/autotag-rules/5')).toHaveLength(0)
  })
})

describe('ordering rules', () => {
  const SECOND = { ...RULE, id: 6, pattern: 'co-?founder', position: 1 }
  const THIRD = { ...RULE, id: 7, pattern: 'cofounder', position: 2 }

  it('moves a rule up or down by sending the whole new order', async () => {
    let order = [RULE, SECOND, THIRD]
    const seen = mockApi({
      'GET /api/v1/tags': () => jsonResponse([TAG]),
      'GET /api/v1/autotag-rules': () => jsonResponse(order),
      'POST /api/v1/autotag-rules/reorder': ({ body }) => {
        const ids = (body as { rule_ids: number[] }).rule_ids
        order = ids.map((id) => [RULE, SECOND, THIRD].find((rule) => rule.id === id) as typeof RULE)
        return jsonResponse(order)
      },
    })
    renderWithClient(<TagsPanel />)

    // The ends cannot move past the ends.
    expect(await screen.findByRole('button', { name: 'Move rule 5 up' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Move rule 7 down' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: 'Move rule 7 up' }))
    await waitFor(() =>
      expect(requestsTo(seen, 'POST', '/api/v1/autotag-rules/reorder')[0]?.body).toEqual({
        rule_ids: [5, 7, 6],
      }),
    )
    // The list reloads in the new order.
    await waitFor(() => {
      const patterns = screen.getAllByText(/founder/, { selector: 'code' })
      expect(patterns.map((node) => node.textContent)).toEqual([
        'founder',
        'cofounder',
        'co-?founder',
      ])
    })

    fireEvent.click(screen.getByRole('button', { name: 'Move rule 5 down' }))
    await waitFor(() =>
      expect(requestsTo(seen, 'POST', '/api/v1/autotag-rules/reorder')[1]?.body).toEqual({
        rule_ids: [7, 5, 6],
      }),
    )
  })

  it('says which rule gets the credit, since that is what the order decides', async () => {
    mockApi(baseRoutes())
    renderWithClient(<TagsPanel />)
    expect(
      await screen.findByText(/the one higher in this list gets the credit/),
    ).toBeInTheDocument()
  })
})
