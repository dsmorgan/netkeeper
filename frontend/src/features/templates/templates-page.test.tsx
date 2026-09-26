/**
 * The template editor (P3-10): lint shows before you save, the preview renders
 * a missing field as empty text with a warning, and the version history opens
 * an older version read-only.
 */
import {
  Link,
  Outlet,
  RouterProvider,
  createBrowserHistory,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  type RouterHistory,
} from '@tanstack/react-router'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { mockApi, renderWithClient, requestsTo, type RouteHandler } from '@/features/crm/harness'
import { jsonResponse } from '@/test/fetch'

import type { LintIssue, TemplateOut } from './api'
import { TemplatesPage } from './templates-page'

const WAIT = { timeout: 3000 }

/**
 * The page at `/templates` under a router of its own, with a link to one other
 * page, so leaving it is a real navigation the unsaved-draft guard can stop.
 */
async function renderPage(
  history: RouterHistory = createMemoryHistory({ initialEntries: ['/templates'] }),
) {
  const root = createRootRoute({
    component: () => (
      <>
        <Link to="/contacts">Elsewhere</Link>
        <Outlet />
      </>
    ),
  })
  const routeTree = root.addChildren([
    createRoute({ getParentRoute: () => root, path: '/templates', component: TemplatesPage }),
    createRoute({
      getParentRoute: () => root,
      path: '/contacts',
      component: () => <p>Somewhere else</p>,
    }),
  ])
  const router = createRouter({ routeTree, history })
  const utils = renderWithClient(<RouterProvider router={router} />)
  await screen.findByRole('heading', { name: 'Templates' })
  return { ...utils, router }
}

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
    await renderPage()
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
    await renderPage()
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
    await renderPage()
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
    await renderPage()
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
    await renderPage()
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
    await renderPage()
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

describe('unsaved draft', () => {
  const other = template({ id: 2, name: 'follow-up', body: 'Following up, {{ first_name }}.' })

  async function openAndEdit() {
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    fireEvent.change(await screen.findByLabelText('Body'), {
      target: { value: 'Unsaved {{ first_name }}' },
    })
  }

  it('asks before opening another template, and Cancel keeps the draft', async () => {
    mockApi(routes([template(), other]))
    await openAndEdit()

    fireEvent.click(screen.getByRole('button', { name: 'follow-up' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Discard unsaved changes?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(screen.getByLabelText('Body')).toHaveValue('Unsaved {{ first_name }}')
  })

  it('opens the other template once you discard', async () => {
    mockApi(routes([template(), other]))
    await openAndEdit()

    fireEvent.click(screen.getByRole('button', { name: 'follow-up' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Discard unsaved changes?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Discard changes' }))

    await waitFor(() =>
      expect(screen.getByLabelText('Body')).toHaveValue('Following up, {{ first_name }}.'),
    )
  })

  it('asks before starting a new template', async () => {
    mockApi(routes([template()]))
    await openAndEdit()

    fireEvent.click(screen.getByRole('button', { name: 'New template' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Discard unsaved changes?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Discard changes' }))

    expect(await screen.findByRole('heading', { name: 'New template' }, WAIT)).toBeInTheDocument()
    expect(screen.getByLabelText('Body')).toHaveValue('')
  })

  it('asks about a new template that has text in it', async () => {
    mockApi(routes([template()]))
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'New template' }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'half-written' } })

    fireEvent.click(screen.getByRole('button', { name: 'reconnect' }))
    expect(
      await screen.findByRole('alertdialog', { name: 'Discard unsaved changes?' }),
    ).toBeInTheDocument()
  })

  it('switches without asking when nothing is unsaved', async () => {
    mockApi(routes([template(), other]))
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    await screen.findByLabelText('Body')

    fireEvent.click(screen.getByRole('button', { name: 'follow-up' }))
    await waitFor(() =>
      expect(screen.getByLabelText('Body')).toHaveValue('Following up, {{ first_name }}.'),
    )
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('asks before leaving the page, and stays on Cancel', async () => {
    mockApi(routes([template()]))
    await openAndEdit()

    fireEvent.click(screen.getByRole('link', { name: 'Elsewhere' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Discard unsaved changes?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(screen.queryByText('Somewhere else')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Body')).toHaveValue('Unsaved {{ first_name }}')
  })

  it('leaves the page once you discard', async () => {
    mockApi(routes([template()]))
    await openAndEdit()

    fireEvent.click(screen.getByRole('link', { name: 'Elsewhere' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Discard unsaved changes?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Discard changes' }))

    expect(await screen.findByText('Somewhere else', undefined, WAIT)).toBeInTheDocument()
  })

  it('leaves without asking when nothing is unsaved', async () => {
    mockApi(routes([template()]))
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    await screen.findByLabelText('Body')

    fireEvent.click(screen.getByRole('link', { name: 'Elsewhere' }))
    expect(await screen.findByText('Somewhere else', undefined, WAIT)).toBeInTheDocument()
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('holds the tab open through beforeunload only while a draft is unsaved', async () => {
    window.history.replaceState(null, '', '/templates')
    mockApi(routes([template()]))
    await renderPage(createBrowserHistory())
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    const body = await screen.findByLabelText('Body')

    const unload = () => {
      const event = new Event('beforeunload', { cancelable: true })
      act(() => {
        window.dispatchEvent(event)
      })
      return event.defaultPrevented
    }
    expect(unload()).toBe(false)

    fireEvent.change(body, { target: { value: 'Unsaved {{ first_name }}' } })
    await waitFor(() => expect(unload()).toBe(true))

    fireEvent.change(body, { target: { value: template().body } })
    await waitFor(() => expect(unload()).toBe(false))
  })
})
