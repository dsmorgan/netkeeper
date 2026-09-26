/**
 * The template editor (P3-10): lint shows before you save, the preview renders
 * a missing field as empty text with a warning, and the version history opens
 * an older version read-only.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { mockApi, renderWithClient, requestsTo, type RouteHandler } from '@/features/crm/harness'
import { jsonResponse } from '@/test/fetch'

import type { LintIssue, TemplateOut } from './api'
import { TemplatesPage } from './templates-page'

const WAIT = { timeout: 3000 }

function template(overrides: Partial<TemplateOut> = {}): TemplateOut {
  return {
    id: 1,
    name: 'reconnect',
    channel: 'email',
    subject: 'Hi {{ first_name }}',
    body: 'Hi {{ first_name }} at {{ company }}.',
    version: 1,
    previous_id: null,
    current: true,
    lint: [],
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-01T10:00:00Z',
    ...overrides,
  }
}

const LOOP: LintIssue = {
  rule: 'unsupported',
  severity: 'error',
  part: 'body',
  message: 'line 2: `for` is not available in a message template',
  field: 'for',
}

/** A lint that refuses a loop, as the backend does, and passes anything else. */
const lintRoute: RouteHandler = ({ body }) => {
  const text = (body as { body: string }).body
  return jsonResponse(text.includes('{% for') ? [LOOP] : [])
}

function routes(rows: TemplateOut[], extra: Record<string, RouteHandler> = {}) {
  const byId: Record<string, RouteHandler> = {}
  for (const row of rows) byId[`GET /api/v1/templates/${row.id}`] = () => jsonResponse(row)
  return {
    'GET /api/v1/templates': () => jsonResponse(rows.filter((row) => row.current)),
    'POST /api/v1/templates/lint': lintRoute,
    ...byId,
    ...extra,
  }
}

describe('lint', () => {
  it('shows a lint error, with its line and a plain sentence, before anything is saved', async () => {
    const seen = mockApi(routes([]))
    renderWithClient(<TemplatesPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'New template' }))

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'loopy' } })
    fireEvent.change(screen.getByLabelText('Body'), {
      target: { value: 'Hi {{ first_name }}\n{% for x in y %}{% endfor %}' },
    })

    const issues = await screen.findByRole('list', { name: 'Lint issues' }, WAIT)
    expect(within(issues).getByText("Loops aren't supported in templates")).toBeInTheDocument()
    expect(within(issues).getByText('Body · line 2')).toBeInTheDocument()
    expect(
      within(issues).getByText('`for` is not available in a message template'),
    ).toBeInTheDocument()
    expect(screen.getByText(/a campaign can't use this template/)).toBeInTheDocument()
    expect(screen.getByLabelText('Body')).toHaveAttribute('aria-invalid', 'true')

    expect(requestsTo(seen, 'POST', '/api/v1/templates')).toEqual([])
    const linted = requestsTo(seen, 'POST', '/api/v1/templates/lint').at(-1)
    expect(linted?.body).toEqual({
      channel: 'email',
      subject: '',
      body: 'Hi {{ first_name }}\n{% for x in y %}{% endfor %}',
    })
  })

  it('clears once the text is fixed', async () => {
    mockApi(routes([template({ body: '{% for x in y %}{% endfor %}' })]))
    renderWithClient(<TemplatesPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    await screen.findByRole('list', { name: 'Lint issues' }, WAIT)

    fireEvent.change(screen.getByLabelText('Body'), { target: { value: 'Hi {{ first_name }}' } })
    expect(await screen.findByText('No lint issues.', undefined, WAIT)).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Lint issues' })).not.toBeInTheDocument()
  })
})

describe('preview', () => {
  it('renders a missing field as empty text with a warning, not an error', async () => {
    const seen = mockApi(
      routes([template()], {
        'GET /api/v1/contacts': () =>
          jsonResponse({
            items: [{ id: 7, first_name: 'Robin', last_name: 'Example', current_company: null }],
            total: 1,
            describe: '',
          }),
        'GET /api/v1/templates/1/preview': () =>
          jsonResponse({
            subject: 'Hi Robin',
            body: 'Hi Robin at .\n<b>not bold</b>',
            issues: [
              {
                rule: 'missing_value',
                severity: 'warning',
                part: 'body',
                message: '`company` has no value here, so it renders empty',
                field: 'company',
              },
            ],
          }),
      }),
    )
    renderWithClient(<TemplatesPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))

    fireEvent.change(await screen.findByLabelText('Contact'), { target: { value: 'rob' } })
    fireEvent.click(await screen.findByRole('button', { name: /Robin Example/ }, WAIT))

    const body = await screen.findByTestId('preview-body')
    expect(body.textContent).toBe('Hi Robin at .\n<b>not bold</b>')
    expect(body.querySelector('b')).toBeNull() // plain text, never HTML
    expect(screen.getByTestId('preview-subject')).toHaveTextContent('Hi Robin')

    const warnings = screen.getByRole('list', { name: 'Preview issues' })
    expect(within(warnings).getByText('Warning')).toBeInTheDocument()
    expect(
      within(warnings).getByText('`company` has no value here, so it renders empty'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    expect(requestsTo(seen, 'GET', '/api/v1/contacts')[0]?.search).toContain('q=rob')
    expect(requestsTo(seen, 'GET', '/api/v1/templates/1/preview')[0]?.search).toBe('?contact_id=7')
  })

  it('says it shows the saved version while the editor has changes', async () => {
    mockApi(routes([template()]))
    renderWithClient(<TemplatesPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    await screen.findByLabelText('Body')
    expect(screen.queryByText(/The preview shows the saved version/)).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Body'), { target: { value: 'Yo {{ first_name }}' } })
    expect(screen.getByText(/The preview shows the saved version/)).toBeInTheDocument()
  })
})

describe('versions', () => {
  const v1 = template({ id: 1, version: 1, current: false, body: 'Old {{ first_name }}' })
  const v2 = template({ id: 2, version: 2, previous_id: 1, current: false })
  const v3 = template({ id: 3, version: 3, previous_id: 2, body: 'New {{ first_name }}' })

  it('lists every version, newest first, and opens an older one read-only', async () => {
    mockApi(routes([v1, v2, v3]))
    renderWithClient(<TemplatesPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))

    const list = await screen.findByRole('list', { name: 'Versions' })
    const rows = within(list).getAllByRole('listitem')
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining('Version 3Current'),
      expect.stringContaining('Version 2Read-only'),
      expect.stringContaining('Version 1Read-only'),
    ])
    expect(screen.getByRole('heading', { name: 'Editing version 3' })).toBeInTheDocument()

    fireEvent.click(within(list).getByRole('button', { name: 'View version 1' }))
    expect(await screen.findByTestId('version-body')).toHaveTextContent('Old {{ first_name }}')
    expect(screen.queryByLabelText('Body')).not.toBeInTheDocument() // no editor
    expect(screen.getByText(/An older version can't be edited/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Back to the current version' }))
    expect(screen.getByLabelText('Body')).toHaveValue('New {{ first_name }}')
  })

  it('says so when an edit to a template in use is saved as a new version', async () => {
    const saved = template({ id: 9, version: 2, previous_id: 1, body: 'Yo {{ first_name }}' })
    const seen = mockApi(
      routes([template(), saved], {
        // The list holds the newest version of each: the old one until the edit lands.
        'GET /api/v1/templates': ({ seen: all }) =>
          jsonResponse(
            requestsTo(all, 'PATCH', '/api/v1/templates/1').length ? [saved] : [template()],
          ),
        'PATCH /api/v1/templates/1': () => jsonResponse(saved),
      }),
    )
    renderWithClient(<TemplatesPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    fireEvent.change(await screen.findByLabelText('Body'), {
      target: { value: 'Yo {{ first_name }}' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByText('Saved as version 2')).toBeInTheDocument()
    expect(screen.getByText(/Version 1 stays as it was for that campaign/)).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Editing version 2' })).toBeInTheDocument(),
    )
    expect(requestsTo(seen, 'PATCH', '/api/v1/templates/1')[0]?.body).toEqual({
      name: 'reconnect',
      channel: 'email',
      subject: 'Hi {{ first_name }}',
      body: 'Yo {{ first_name }}',
    })
  })
})
