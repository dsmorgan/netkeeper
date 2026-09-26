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
    in_use: false,
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

/**
 * A lint route that holds each answer until the test releases it, by the body text it
 * was asked about, so a test can decide the order answers arrive in.
 */
function heldLint() {
  const held = new Map<string, (issues: LintIssue[]) => void>()
  const route: RouteHandler = ({ body }) =>
    new Promise((resolve) => {
      held.set((body as { body: string }).body, (issues) => resolve(jsonResponse(issues)))
    })
  /** Resolves once a lint request for `text` has gone out. */
  const asked = (text: string) => waitFor(() => expect(held.has(text)).toBe(true), WAIT)
  const release = async (text: string, issues: LintIssue[]) => {
    await asked(text)
    act(() => held.get(text)?.(issues))
  }
  return { route, asked, release }
}

describe('lint while you type', () => {
  it('dims the old result and says it is checking until the new one is in', async () => {
    const lint = heldLint()
    mockApi(
      routes([template({ body: '{% for x in y %}{% endfor %}' })], {
        'POST /api/v1/templates/lint': lint.route,
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    expect(await screen.findByText('Checking…')).toBeInTheDocument()
    await lint.release('{% for x in y %}{% endfor %}', [LOOP])
    const issues = await screen.findByRole('list', { name: 'Lint issues' })
    await waitFor(() => expect(screen.queryByText('Checking…')).not.toBeInTheDocument())
    const section = screen.getByRole('region', { name: 'Lint' })
    expect(section).not.toHaveAttribute('aria-busy')
    expect(issues.closest('[data-stale]')).toBeNull()

    fireEvent.change(screen.getByLabelText('Body'), { target: { value: 'Hi {{ first_name }}' } })
    // The loop is gone from the text, but its answer is still on screen: dimmed, and busy.
    expect(screen.getByText('Checking…')).toBeInTheDocument()
    expect(section).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByRole('list', { name: 'Lint issues' }).closest('[data-stale]')).not.toBeNull()

    // Past the debounce, with the request out: still the old answer, so still stale.
    await lint.asked('Hi {{ first_name }}')
    expect(screen.getByText('Checking…')).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Lint issues' }).closest('[data-stale]')).not.toBeNull()

    await lint.release('Hi {{ first_name }}', [])
    expect(await screen.findByText('No lint issues.', undefined, WAIT)).toBeInTheDocument()
    expect(screen.queryByText('Checking…')).not.toBeInTheDocument()
    expect(section).not.toHaveAttribute('aria-busy')
    expect(screen.getByText('No lint issues.').closest('[data-stale]')).toBeNull()
  })

  it('shows the answer for the latest text when an older answer arrives after it', async () => {
    const lint = heldLint()
    mockApi(routes([], { 'POST /api/v1/templates/lint': lint.route }))
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'New template' }))
    await lint.release('', [])

    const looped = 'Hi {{ first_name }}\n{% for x in y %}{% endfor %}'
    fireEvent.change(screen.getByLabelText('Body'), { target: { value: looped } })
    // The loop's request goes out; fix the text before it answers.
    await lint.asked(looped)
    const fixed = 'Hi {{ first_name }}'
    fireEvent.change(screen.getByLabelText('Body'), { target: { value: fixed } })

    await lint.release(fixed, [])
    await lint.release(looped, [LOOP]) // late, and for text that is gone

    expect(await screen.findByText('No lint issues.', undefined, WAIT)).toBeInTheDocument()
    // Give the late answer every chance to land before checking it changed nothing.
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)))
    expect(screen.getByText('No lint issues.')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Lint issues' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Body')).not.toHaveAttribute('aria-invalid')
    expect(screen.queryByText('Checking…')).not.toBeInTheDocument()
  })

  it('shows a 422 from lint, the text being too long, as the server said it', async () => {
    mockApi(
      routes([], {
        'POST /api/v1/templates/lint': () =>
          jsonResponse({ detail: 'body is longer than 20000 characters' }, 422),
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'New template' }))
    fireEvent.change(screen.getByLabelText('Body'), { target: { value: 'x'.repeat(20_001) } })

    const alert = await screen.findByRole('alert', undefined, WAIT)
    expect(alert).toHaveTextContent('Could not lint the template')
    expect(alert).toHaveTextContent('body is longer than 20000 characters')
    expect(screen.queryByText('Checking…')).not.toBeInTheDocument()
    // Lint never blocks a save; the save answers for itself.
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'long' } })
    expect(screen.getByRole('button', { name: 'Create template' })).toBeEnabled()
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
    const used = template({ in_use: true })
    const saved = template({ id: 9, version: 2, previous_id: 1, body: 'Yo {{ first_name }}' })
    const seen = mockApi(
      routes([used, saved], {
        // The list holds the newest version of each: the old one until the edit lands.
        'GET /api/v1/templates': ({ seen: all }) =>
          jsonResponse(requestsTo(all, 'PATCH', '/api/v1/templates/1').length ? [saved] : [used]),
        'PATCH /api/v1/templates/1': () => jsonResponse(saved),
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /^reconnect/ }))
    fireEvent.change(await screen.findByLabelText('Body'), {
      target: { value: 'Yo {{ first_name }}' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save as version 2' }))

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

describe('save and delete', () => {
  it('saves an edit in place when no campaign uses the template, with no version notice', async () => {
    let saved = template()
    const seen = mockApi(
      routes([], {
        'GET /api/v1/templates': () => jsonResponse([saved]),
        'GET /api/v1/templates/1': () => jsonResponse(saved),
        'PATCH /api/v1/templates/1': () => {
          saved = template({ body: 'Yo {{ first_name }}', updated_at: '2026-09-02T10:00:00Z' })
          return jsonResponse(saved)
        },
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    fireEvent.change(await screen.findByLabelText('Body'), {
      target: { value: 'Yo {{ first_name }}' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    // Saved, and the saved text is the draft again: nothing left to save.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled(), WAIT)
    expect(requestsTo(seen, 'PATCH', '/api/v1/templates/1')).toHaveLength(1)
    expect(screen.getByRole('heading', { name: 'Editing version 1' })).toBeInTheDocument()
    expect(screen.getByLabelText('Body')).toHaveValue('Yo {{ first_name }}')
    expect(screen.queryByText(/Saved as version/)).not.toBeInTheDocument()
    const history = screen.getByRole('list', { name: 'Versions' })
    expect(within(history).getAllByRole('listitem')).toHaveLength(1)

    // Nothing is unsaved any more, so starting another template does not ask.
    fireEvent.click(screen.getByRole('button', { name: 'New template' }))
    expect(await screen.findByRole('heading', { name: 'New template' })).toBeInTheDocument()
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('counts a save the server normalized as saved, with no discard prompt after it', async () => {
    let saved = template()
    mockApi(
      routes([], {
        'GET /api/v1/templates': () => jsonResponse([saved]),
        'GET /api/v1/templates/1': () => jsonResponse(saved),
        // The server trims the name and stores a blank subject as none.
        'PATCH /api/v1/templates/1': () => {
          saved = template({ name: 'renamed', subject: null })
          return jsonResponse(saved)
        },
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'renamed ' } })
    fireEvent.change(screen.getByLabelText('Subject'), { target: { value: '   ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled(), WAIT)
    expect(screen.getByLabelText('Name')).toHaveValue('renamed')
    expect(screen.getByLabelText('Subject')).toHaveValue('')
    expect(screen.queryByText(/The preview shows the saved version/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'New template' }))
    expect(await screen.findByRole('heading', { name: 'New template' })).toBeInTheDocument()
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('stops guarding once a dirty template is deleted', async () => {
    mockApi(
      routes([template()], {
        'DELETE /api/v1/templates/1': () => new Response(null, { status: 204 }),
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    fireEvent.change(await screen.findByLabelText('Body'), { target: { value: 'Unsaved' } })
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Delete “reconnect”?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete template' }))
    expect(await screen.findByText('No template open', undefined, WAIT)).toBeInTheDocument()

    // The draft went with the template, so leaving has nothing to ask about.
    fireEvent.click(screen.getByRole('link', { name: 'Elsewhere' }))
    expect(await screen.findByText('Somewhere else', undefined, WAIT)).toBeInTheDocument()
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('deletes a template after you confirm, without asking about the draft', async () => {
    let deleted = false
    const seen = mockApi(
      routes([template()], {
        'GET /api/v1/templates': () => jsonResponse(deleted ? [] : [template()]),
        'DELETE /api/v1/templates/1': () => {
          deleted = true
          return new Response(null, { status: 204 })
        },
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    fireEvent.change(await screen.findByLabelText('Body'), { target: { value: 'Unsaved' } })

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Delete “reconnect”?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete template' }))

    expect(await screen.findByText('No template open', undefined, WAIT)).toBeInTheDocument()
    expect(await screen.findByText('No templates yet', undefined, WAIT)).toBeInTheDocument()
    expect(requestsTo(seen, 'DELETE', '/api/v1/templates/1')).toHaveLength(1)
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('keeps the template and shows why when a campaign uses it (409)', async () => {
    mockApi(
      routes([template()], {
        'DELETE /api/v1/templates/1': () =>
          jsonResponse({ detail: 'a campaign uses template 1' }, 409),
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Delete “reconnect”?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete template' }))

    expect(await within(dialog).findByRole('alert', undefined, WAIT)).toHaveTextContent(
      'a campaign uses template 1',
    )
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(screen.getByRole('heading', { name: 'Editing version 1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^reconnect/ })).toBeInTheDocument()
  })
})

describe('in use', () => {
  it('says before you save that an edit to a template in use makes a new version', async () => {
    mockApi(
      routes([
        template({ id: 1, name: 'reconnect', version: 3, in_use: true }),
        template({ id: 2, name: 'follow-up' }),
      ]),
    )
    await renderPage()

    const list = await screen.findByRole('list', { name: 'Templates' })
    const [used, free] = within(list).getAllByRole('button')
    expect(used).toHaveTextContent('reconnectIn use')
    expect(free).toHaveTextContent(/^follow-up$/)

    fireEvent.click(used!)
    expect(await screen.findByText('A campaign uses this version')).toBeInTheDocument()
    expect(
      screen.getByText('Saving creates version 4. The campaign keeps sending version 3 as it is.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save as version 4' })).toBeInTheDocument()
  })

  it('says nothing of versions for a template no campaign uses', async () => {
    mockApi(routes([template()]))
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))

    expect(await screen.findByRole('button', { name: 'Save' })).toBeInTheDocument()
    expect(screen.queryByText('A campaign uses this version')).not.toBeInTheDocument()
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
