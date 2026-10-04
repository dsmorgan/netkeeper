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
import { describe, expect, it, vi } from 'vitest'

import { mockApi, renderWithClient, requestsTo, type RouteHandler } from '@/features/crm/harness'
import { jsonResponse } from '@/test/fetch'

import type { LintIssue, MergeField, TemplateOut } from './api'
import { LINT_DEBOUNCE_MS } from './draft'
import { PROMPT_RULES } from './lint'
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

/** Invented values, as the backend's placeholders are, and one contact's own for `contact_id`. */
const mergeFieldsRoute: RouteHandler = ({ url }) => {
  const contactId = url.searchParams.get('contact_id')
  const fields: MergeField[] = [
    {
      name: 'first_name',
      group: 'contact',
      description: "The contact's first name.",
      insert: 'first_name',
      example: contactId === null ? 'Alex' : 'Robin',
      example_source: contactId === null ? 'placeholder' : 'contact',
    },
    {
      name: 'company',
      group: 'contact',
      description: "The contact's current company.",
      insert: 'company',
      example: contactId === null ? 'Example Co' : null,
      example_source: contactId === null ? 'placeholder' : 'contact',
    },
    {
      name: 'previous_send_date',
      group: 'campaign',
      description: 'When the previous step went out.',
      insert: 'previous_send_date | ago',
      example: '3 weeks ago',
      example_source: 'placeholder',
    },
  ]
  return jsonResponse({ contact_id: contactId === null ? null : Number(contactId), fields })
}

function routes(rows: TemplateOut[], extra: Record<string, RouteHandler> = {}) {
  const byId: Record<string, RouteHandler> = {}
  for (const row of rows) byId[`GET /api/v1/templates/${row.id}`] = () => jsonResponse(row)
  return {
    'GET /api/v1/templates': () => jsonResponse(rows.filter((row) => row.current)),
    'GET /api/v1/templates/merge-fields': mergeFieldsRoute,
    'POST /api/v1/templates/lint': lintRoute,
    ...byId,
    ...extra,
  }
}

describe('lint', () => {
  it('counts only errors in the red badge and warnings in an outlined one', async () => {
    const long: LintIssue = {
      rule: 'linkedin_long',
      severity: 'warning',
      part: 'body',
      message: 'the body is 1,500 characters; over 1,000, the prefill takes minutes to type it',
      field: null,
      line: 1,
    }
    const subject: LintIssue = {
      rule: 'linkedin_subject',
      severity: 'error',
      part: 'subject',
      message: 'LinkedIn messages have no subject, so clear it',
      field: null,
      line: 1,
    }
    mockApi(
      routes([
        template({ id: 1, name: 'long one', channel: 'linkedin', subject: null, lint: [long] }),
        template({ id: 2, name: 'both', channel: 'linkedin', lint: [subject, long] }),
      ]),
    )
    await renderPage()
    const list = await screen.findByRole('list', { name: 'Templates' }, WAIT)
    const items = within(list).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    const [first, second] = items as [HTMLElement, HTMLElement]
    expect(within(first).queryByText(/lint error/)).toBeNull()
    expect(within(first).getByText('1 warning')).toBeInTheDocument()
    expect(within(second).getByText('1 lint error')).toBeInTheDocument()
    expect(within(second).getByText('1 warning')).toBeInTheDocument()
  })

  it('shows a lint error, with its line and a plain sentence, before anything is saved', async () => {
    const seen = mockApi(routes([]))
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'New template' }))

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'loopy' } })
    fireEvent.change(screen.getByLabelText('Body'), {
      target: { value: 'Hi {{ first_name }}\n{% for x in y %}{% endfor %}' },
    })

    const issues = await screen.findByRole('list', { name: 'Body lint' }, WAIT)
    expect(within(issues).getByText("Loops aren't supported in templates")).toBeInTheDocument()
    expect(within(issues).getByText('Line 2')).toBeInTheDocument()
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
    await screen.findByRole('list', { name: 'Body lint' }, WAIT)

    fireEvent.change(screen.getByLabelText('Body'), { target: { value: 'Hi {{ first_name }}' } })
    expect(await screen.findByText('No lint issues.', undefined, WAIT)).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Body lint' })).not.toBeInTheDocument()
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
    const issues = await screen.findByRole('list', { name: 'Body lint' })
    await waitFor(() => expect(screen.queryByText('Checking…')).not.toBeInTheDocument())
    const section = screen.getByRole('region', { name: 'Lint' })
    expect(section).not.toHaveAttribute('aria-busy')
    expect(issues.closest('[data-stale]')).toBeNull()

    fireEvent.change(screen.getByLabelText('Body'), { target: { value: 'Hi {{ first_name }}' } })
    // The loop is gone from the text, but its answer is still on screen: dimmed, and busy.
    expect(screen.getByText('Checking…')).toBeInTheDocument()
    expect(section).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByRole('list', { name: 'Body lint' }).closest('[data-stale]')).not.toBeNull()

    // Past the debounce, with the request out: still the old answer, so still stale.
    await lint.asked('Hi {{ first_name }}')
    expect(screen.getByText('Checking…')).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Body lint' }).closest('[data-stale]')).not.toBeNull()

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
    expect(screen.queryByRole('list', { name: 'Body lint' })).not.toBeInTheDocument()
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

  it('keeps an edit typed while an in-place save is out, and leaves it unsaved', async () => {
    let answer: ((row: TemplateOut) => void) | undefined
    const seen = mockApi(
      routes([template()], {
        'PATCH /api/v1/templates/1': () =>
          new Promise((resolve) => {
            answer = (row) => resolve(jsonResponse(row))
          }),
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    const body = await screen.findByLabelText('Body')
    fireEvent.change(body, { target: { value: 'first' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(answer).toBeDefined(), WAIT)

    // The save is out; the editor stays editable, and you keep typing.
    expect(body).not.toHaveAttribute('readonly')
    fireEvent.change(body, { target: { value: 'first plus more' } })
    act(() => answer?.(template({ body: 'first' })))

    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled(), WAIT)
    expect(body).toHaveValue('first plus more')
    expect(requestsTo(seen, 'PATCH', '/api/v1/templates/1')[0]?.body).toMatchObject({
      body: 'first',
    })
    // What was typed after the save is still unsaved, so it is still guarded.
    fireEvent.click(screen.getByRole('button', { name: 'New template' }))
    expect(
      await screen.findByRole('alertdialog', { name: 'Discard unsaved changes?' }),
    ).toBeInTheDocument()
  })

  it('holds the fields still while a create is out, since the saved row opens fresh', async () => {
    let answer: (() => void) | undefined
    mockApi(
      routes([], {
        'POST /api/v1/templates': () =>
          new Promise((resolve) => {
            answer = () => resolve(jsonResponse(template({ id: 5, name: 'fresh' }), 201))
          }),
        'GET /api/v1/templates/5': () => jsonResponse(template({ id: 5, name: 'fresh' })),
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'New template' }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'fresh' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create template' }))
    await waitFor(() => expect(answer).toBeDefined(), WAIT)

    for (const label of ['Name', 'Subject', 'Body']) {
      expect(screen.getByLabelText(label)).toHaveAttribute('readonly')
    }
    expect(screen.getByLabelText('Channel')).toBeDisabled()

    act(() => answer?.())
    expect(await screen.findByRole('heading', { name: 'Editing version 1' }, WAIT)).toBeVisible()
    expect(screen.getByLabelText('Body')).not.toHaveAttribute('readonly')
  })

  it('holds the fields still while a save that makes a new version is out', async () => {
    let answer: (() => void) | undefined
    const next = template({ id: 9, version: 2, previous_id: 1, body: 'Yo' })
    mockApi(
      routes([template({ in_use: true }), next], {
        'GET /api/v1/templates': () => jsonResponse([template({ in_use: true })]),
        'PATCH /api/v1/templates/1': () =>
          new Promise((resolve) => {
            answer = () => resolve(jsonResponse(next))
          }),
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /^reconnect/ }))
    fireEvent.change(await screen.findByLabelText('Body'), { target: { value: 'Yo' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save as version 2' }))
    await waitFor(() => expect(answer).toBeDefined(), WAIT)

    expect(screen.getByLabelText('Body')).toHaveAttribute('readonly')
    act(() => answer?.())
    expect(await screen.findByText('Saved as version 2', undefined, WAIT)).toBeInTheDocument()
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

describe('merge-field helper', () => {
  async function openTemplate(row: TemplateOut = template()) {
    mockApi(routes([row]))
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: row.name }))
    await screen.findByRole('button', { name: 'Insert {{ company }}' }, WAIT)
    return {
      subject: screen.getByLabelText('Subject') as HTMLInputElement,
      body: screen.getByLabelText('Body') as HTMLTextAreaElement,
    }
  }

  it('inserts at the cursor in the body and leaves the cursor after the insert', async () => {
    const { subject, body } = await openTemplate()
    act(() => {
      body.focus()
      body.setSelectionRange(3, 3) // "Hi |{{ first_name }} at ..."
    })
    fireEvent.click(screen.getByRole('button', { name: 'Insert {{ company }}' }))

    const inserted = 'Hi {{ company }}{{ first_name }} at {{ company }}.'
    expect(body.value).toBe(inserted)
    expect(document.activeElement).toBe(body)
    expect(body.selectionStart).toBe(3 + '{{ company }}'.length)
    expect(body.selectionEnd).toBe(body.selectionStart)
    expect(subject.value).toBe('Hi {{ first_name }}')
  })

  it('inserts at the cursor in the subject when it was focused last, replacing a selection', async () => {
    const { subject, body } = await openTemplate()
    act(() => body.focus())
    act(() => {
      subject.focus()
      subject.setSelectionRange(3, 19) // selects "{{ first_name }}"
    })
    fireEvent.click(screen.getByRole('button', { name: 'Insert {{ previous_send_date | ago }}' }))

    expect(subject.value).toBe('Hi {{ previous_send_date | ago }}')
    expect(document.activeElement).toBe(subject)
    expect(subject.selectionStart).toBe(subject.value.length)
    expect(body.value).toBe('Hi {{ first_name }} at {{ company }}.')

    // The cursor stays in the subject, so a second insert follows the first.
    fireEvent.click(screen.getByRole('button', { name: 'Insert {{ first_name }}' }))
    expect(subject.value).toBe('Hi {{ previous_send_date | ago }}{{ first_name }}')
  })

  it('inserts through the browser’s own editing, so the insert can be undone', async () => {
    const { body } = await openTemplate()
    // What a browser's insertText does: edit at the selection and fire an input event.
    const execCommand = vi.fn((command: string, _ui?: boolean, text?: string) => {
      const field = document.activeElement as HTMLTextAreaElement
      if (command !== 'insertText' || text === undefined) return false
      const { selectionStart: start, selectionEnd: end, value } = field
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set
      setter?.call(field, value.slice(0, start) + text + value.slice(end))
      field.setSelectionRange(start + text.length, start + text.length)
      field.dispatchEvent(new Event('input', { bubbles: true }))
      return true
    })
    const original = document.execCommand
    document.execCommand = execCommand as unknown as typeof document.execCommand
    try {
      act(() => {
        body.focus()
        body.setSelectionRange(3, 3)
      })
      fireEvent.click(screen.getByRole('button', { name: 'Insert {{ company }}' }))
      expect(execCommand).toHaveBeenCalledWith('insertText', false, '{{ company }}')
      // Once: the value the input event carried, not inserted a second time by hand.
      expect(body.value).toBe('Hi {{ company }}{{ first_name }} at {{ company }}.')
      expect(document.activeElement).toBe(body)
      expect(body.selectionStart).toBe(3 + '{{ company }}'.length)
    } finally {
      document.execCommand = original
    }
  })

  it('sets the value itself when the browser does not do insertText', async () => {
    const { body } = await openTemplate()
    const execCommand = vi.fn(() => false)
    const original = document.execCommand
    document.execCommand = execCommand as unknown as typeof document.execCommand
    try {
      act(() => {
        body.focus()
        body.setSelectionRange(3, 3)
      })
      fireEvent.click(screen.getByRole('button', { name: 'Insert {{ company }}' }))
      expect(execCommand).toHaveBeenCalledWith('insertText', false, '{{ company }}')
      expect(body.value).toBe('Hi {{ company }}{{ first_name }} at {{ company }}.')
      expect(document.activeElement).toBe(body)
      expect(body.selectionStart).toBe(3 + '{{ company }}'.length)
    } finally {
      document.execCommand = original
    }
  })

  it('appends to the body when no field has been focused yet', async () => {
    const { body } = await openTemplate()
    fireEvent.click(screen.getByRole('button', { name: 'Insert {{ first_name }}' }))
    expect(body.value).toBe('Hi {{ first_name }} at {{ company }}.{{ first_name }}')
  })

  it('describes each field, with invented examples until a contact is picked', async () => {
    const seen = mockApi(
      routes([template()], {
        'GET /api/v1/contacts': () =>
          jsonResponse({
            items: [{ id: 7, first_name: 'Robin', last_name: 'Example', current_company: null }],
            total: 1,
            describe: '',
          }),
        'GET /api/v1/templates/1/preview': () =>
          jsonResponse({ subject: 'Hi Robin', body: 'Hi Robin at .', issues: [] }),
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    const helper = await screen.findByRole('region', { name: 'Merge fields' }, WAIT)
    const insert = await within(helper).findByRole('button', { name: 'Insert {{ first_name }}' })
    expect(insert).toHaveAccessibleDescription(/The contact's first name\.\s*Example: Alex/)
    expect(within(helper).getByText(/Examples are made up/)).toBeInTheDocument()
    expect(requestsTo(seen, 'GET', '/api/v1/templates/merge-fields')[0]?.search).toBe('')

    fireEvent.change(await screen.findByLabelText('Contact'), { target: { value: 'rob' } })
    fireEvent.click(await screen.findByRole('button', { name: /Robin Example/ }, WAIT))

    await within(helper).findByText(/Examples are Robin Example's values\./, undefined, WAIT)
    await waitFor(() =>
      expect(
        within(helper).getByRole('button', { name: 'Insert {{ first_name }}' }),
      ).toHaveAccessibleDescription(/This contact: Robin/),
    )
    expect(
      within(helper).getByRole('button', { name: 'Insert {{ company }}' }),
    ).toHaveAccessibleDescription(/No value for this contact/)
    expect(requestsTo(seen, 'GET', '/api/v1/templates/merge-fields').at(-1)?.search).toBe(
      '?contact_id=7',
    )
  })
})

describe('inline lint', () => {
  const ISSUES: LintIssue[] = [
    {
      rule: 'undefined_variable',
      severity: 'error',
      part: 'subject',
      message: '`frist_name` is not a merge field',
      field: 'frist_name',
      line: 1,
    },
    {
      rule: 'bad_link',
      severity: 'warning',
      part: 'body',
      message: '`http:/broken` is not a link that parses',
      field: 'http:/broken',
      line: 3,
    },
    {
      rule: 'undefined_variable',
      severity: 'error',
      part: 'body',
      message: '`compnay` is not a merge field',
      field: 'compnay',
      line: 2,
    },
  ]
  const BODY = 'Hi {{ first_name }}\n{{ compnay }}\nsee http:/broken'

  it('shows each finding under its field, at its line, with why it matters', async () => {
    mockApi(
      routes([template({ subject: 'Hi {{ frist_name }}', body: BODY })], {
        'POST /api/v1/templates/lint': () => jsonResponse(ISSUES),
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))

    const bodyLint = await screen.findByRole('list', { name: 'Body lint' }, WAIT)
    const [first, second] = within(bodyLint).getAllByRole('listitem')
    if (first === undefined || second === undefined) throw new Error('expected two findings')
    const items = [first, second]
    // In reading order, by line.
    expect(items.map((item) => within(item).getByText(/^Line \d$/).textContent)).toEqual([
      'Line 2',
      'Line 3',
    ])
    expect(within(first).getByText('{{ compnay }}')).toBeInTheDocument()
    expect(within(first).getByText('`compnay` is not a merge field')).toBeInTheDocument()
    expect(within(first).getByText(/Why it matters: This isn't a merge field/)).toBeInTheDocument()
    expect(within(second).getByText('Warning')).toBeInTheDocument()
    expect(within(second).getByText(/won't open for the person/)).toBeInTheDocument()

    // Tied to the fields, so a screen reader reads them with each one.
    const body = screen.getByLabelText('Body')
    expect(body).toHaveAttribute('aria-invalid', 'true')
    expect(body.getAttribute('aria-describedby')).toBe(bodyLint.id)
    const subjectLint = screen.getByRole('list', { name: 'Subject lint' })
    expect(within(subjectLint).getByText('`frist_name` is not a merge field')).toBeInTheDocument()
    expect(within(subjectLint).queryByText(/^Line/)).not.toBeInTheDocument()
    const subject = screen.getByLabelText('Subject')
    expect(subject.getAttribute('aria-describedby')?.split(' ')).toContain(subjectLint.id)
    expect(subject).toHaveAccessibleDescription(/Required for email.*frist_name/)

    // The lines themselves are marked behind the text: the error line and the warning line.
    const marks = screen.getByTestId('line-marks')
    expect(marks).toHaveAttribute('aria-hidden', 'true')
    expect(marks.querySelector('[data-line="1"]')).not.toHaveAttribute('data-severity')
    expect(marks.querySelector('[data-line="2"]')).toHaveAttribute('data-severity', 'error')
    expect(marks.querySelector('[data-line="3"]')).toHaveAttribute('data-severity', 'warning')

    // A long quoted line must not widen the editor column (#358 review): every grid item
    // holding a field and its findings may shrink below its content's width.
    for (const list of [bodyLint, subjectLint]) {
      const field = list.closest('.grid')
      expect(field).toHaveClass('min-w-0', 'grid-cols-[minmax(0,1fr)]')
      expect(list.parentElement).toHaveClass('min-w-0')
    }
    expect(within(first).getByText('{{ compnay }}')).toHaveClass('truncate')
    expect(screen.getByText(/3 lint findings, shown under/)).toHaveAttribute('role', 'status')

    // Errors still block activation, and the editor says so.
    expect(screen.getByText(/a campaign can't use this template/)).toBeInTheDocument()
  })

  it('moves the cursor to a finding’s line', async () => {
    mockApi(
      routes([template({ body: BODY })], {
        'POST /api/v1/templates/lint': () => jsonResponse(ISSUES.slice(1)),
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Go to line 2' }, WAIT))
    const body = screen.getByLabelText('Body') as HTMLTextAreaElement
    expect(document.activeElement).toBe(body)
    expect(body.value.slice(body.selectionStart, body.selectionEnd)).toBe('{{ compnay }}')
  })

  it('does not say a warning blocks activation', async () => {
    mockApi(
      routes([template({ body: BODY })], {
        'POST /api/v1/templates/lint': () => jsonResponse(ISSUES.slice(1, 2)),
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'reconnect' }))
    const bodyLint = await screen.findByRole('list', { name: 'Body lint' }, WAIT)
    expect(within(bodyLint).getByText('Warning')).toBeInTheDocument()
    expect(screen.queryByText(/a campaign can't use this template/)).not.toBeInTheDocument()
    expect(screen.getByLabelText('Body')).not.toHaveAttribute('aria-invalid')
  })
})

describe('draft with your AI assistant', () => {
  const ROBIN = { id: 7, first_name: 'Robin', last_name: 'Example', current_company: null }

  function withClipboard(writeText?: (text: string) => Promise<void>) {
    Object.defineProperty(navigator, 'clipboard', {
      value: writeText === undefined ? undefined : { writeText },
      configurable: true,
    })
  }

  async function openHelper(
    row: TemplateOut = template(),
    extra: Record<string, RouteHandler> = {},
  ) {
    const seen = mockApi(
      routes([row], {
        'GET /api/v1/contacts': () => jsonResponse({ items: [ROBIN], total: 1, describe: '' }),
        'GET /api/v1/templates/1/preview': () =>
          jsonResponse({ subject: 'Hi Robin', body: 'Hi Robin at .', issues: [] }),
        ...extra,
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByRole('button', { name: row.name }))
    const helper = await screen.findByRole('region', { name: 'Draft with your AI assistant' }, WAIT)
    const toggle = within(helper).getByRole('button', { name: 'Show' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    expect(within(helper).getByRole('button', { name: 'Hide' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
    return { seen, helper }
  }

  it('copies a prompt with the allowed fields and the rules, and no contact data', async () => {
    const writeText = vi.fn<(text: string) => Promise<void>>().mockResolvedValue(undefined)
    withClipboard(writeText)
    const { seen, helper } = await openHelper()

    // Pick a contact in the preview: the merge-field examples become Robin's values.
    fireEvent.change(await screen.findByLabelText('Contact'), { target: { value: 'rob' } })
    fireEvent.click(await screen.findByRole('button', { name: /Robin Example/ }, WAIT))
    await screen.findByText(/Examples are Robin Example's values\./, undefined, WAIT)

    fireEvent.change(within(helper).getByLabelText('What the campaign is for'), {
      target: { value: 'Ask for advice on a move into design' },
    })
    fireEvent.change(within(helper).getByLabelText("Who it's for"), {
      target: { value: 'Old teammates' },
    })
    fireEvent.change(within(helper).getByLabelText('Tone'), { target: { value: 'professional' } })
    fireEvent.change(within(helper).getByLabelText('Steps'), { target: { value: '2' } })
    fireEvent.change(within(helper).getByLabelText('Anything to mention'), {
      target: { value: 'The hackathon' },
    })
    const copy = await within(helper).findByRole('button', { name: 'Copy prompt' })
    await waitFor(() => expect(copy).toBeEnabled(), WAIT)
    fireEvent.click(copy)

    await within(helper).findByText('Copied. Paste it into your AI chat assistant.')
    expect(writeText).toHaveBeenCalledTimes(1)
    const prompt = writeText.mock.calls[0]?.[0] ?? ''
    expect(prompt).toContain('{{ first_name }}')
    expect(prompt).toContain('{{ company }}')
    expect(prompt).toContain('{{ previous_send_date | ago }}')
    expect(prompt).toContain(PROMPT_RULES.no_contact_field ?? 'missing')
    expect(prompt).not.toContain('Current template to improve')
    expect(prompt).toContain('Ask for advice on a move into design')
    expect(prompt).toContain('Tone: professional')
    expect(prompt).toContain('End of step 2')
    expect(prompt).not.toMatch(/Robin|Example Co|Alex/)
    // The helper's own field list was asked for without a contact.
    expect(
      requestsTo(seen, 'GET', '/api/v1/templates/merge-fields').some((r) => r.search === ''),
    ).toBe(true)
  })

  it('shows the prompt selected to copy by hand when there is no clipboard', async () => {
    withClipboard(undefined)
    const { helper } = await openHelper()
    const copy = await within(helper).findByRole('button', { name: 'Copy prompt' })
    await waitFor(() => expect(copy).toBeEnabled(), WAIT)
    fireEvent.click(copy)

    const manual = (await within(helper).findByLabelText('Prompt')) as HTMLTextAreaElement
    expect(manual).toHaveAttribute('readonly')
    expect(manual.value).toContain('{{ first_name }}')
    await waitFor(() => expect(document.activeElement).toBe(manual))
    expect(manual.selectionStart).toBe(0)
    expect(manual.selectionEnd).toBe(manual.value.length)
    expect(within(helper).getByText(/didn't allow copying/)).toBeInTheDocument()
  })

  it('shows the prompt to copy by hand when the clipboard refuses', async () => {
    withClipboard(vi.fn<(text: string) => Promise<void>>().mockRejectedValue(new Error('denied')))
    const { helper } = await openHelper()
    const copy = await within(helper).findByRole('button', { name: 'Copy prompt' })
    await waitFor(() => expect(copy).toBeEnabled(), WAIT)
    fireEvent.click(copy)
    expect(await within(helper).findByLabelText('Prompt')).toBeInTheDocument()
  })

  it('fills the subject and body from a pasted reply and lints it', async () => {
    withClipboard(undefined)
    const { seen, helper } = await openHelper()
    const body = 'Hi {{ first_name }},\n{% for x in y %}{% endfor %}'
    fireEvent.change(within(helper).getByLabelText("Assistant's reply"), {
      target: {
        value: `Here you go!\n\nStep 1\nSubject: Long time, {{ first_name }}\nBody:\n${body}\nEnd of step 1\n\nHope it helps.`,
      },
    })
    fireEvent.click(within(helper).getByRole('button', { name: 'Paste result' }))

    expect(screen.getByLabelText('Subject')).toHaveValue('Long time, {{ first_name }}')
    expect(screen.getByLabelText('Body')).toHaveValue(body)
    expect(
      within(helper).getByText(
        'Filled the subject and body. 2 lines outside the labeled format were left out.',
      ),
    ).toBeInTheDocument()
    const issues = await screen.findByRole('list', { name: 'Body lint' }, WAIT)
    expect(within(issues).getByText("Loops aren't supported in templates")).toBeInTheDocument()
    expect(requestsTo(seen, 'POST', '/api/v1/templates/lint').at(-1)?.body).toEqual({
      channel: 'email',
      subject: 'Long time, {{ first_name }}',
      body,
    })
  })

  it('lints a paste at once, without waiting for the typing debounce', async () => {
    withClipboard(undefined)
    const { seen, helper } = await openHelper()
    await screen.findByText('No lint issues.', undefined, WAIT)
    fireEvent.change(within(helper).getByLabelText("Assistant's reply"), {
      target: { value: 'Subject: S\nBody:\nPasted {{ first_name }}' },
    })
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      fireEvent.click(within(helper).getByRole('button', { name: 'Paste result' }))
      // Short of the debounce: a typed edit would still be waiting it out.
      await act(() => vi.advanceTimersByTimeAsync(LINT_DEBOUNCE_MS - 1))
      expect(
        requestsTo(seen, 'POST', '/api/v1/templates/lint').some(
          (r) => (r.body as { body: string }).body === 'Pasted {{ first_name }}',
        ),
      ).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('pastes a reply without the labels into the body as it is', async () => {
    withClipboard(undefined)
    const { helper } = await openHelper()
    fireEvent.change(within(helper).getByLabelText("Assistant's reply"), {
      target: { value: '  Hi {{ first_name }}, it has been ages!  ' },
    })
    fireEvent.click(within(helper).getByRole('button', { name: 'Paste result' }))

    expect(screen.getByLabelText('Body')).toHaveValue('Hi {{ first_name }}, it has been ages!')
    expect(screen.getByLabelText('Subject')).toHaveValue('Hi {{ first_name }}')
    expect(within(helper).getByText('Pasted the reply into the body as it is.')).toBeInTheDocument()
    expect(within(helper).getByText(/didn't have Subject: and Body: labels/)).toBeInTheDocument()
  })

  it('fills step 1 of several and offers the others, step 1 included once you switch', async () => {
    const writeText = vi.fn<(text: string) => Promise<void>>().mockResolvedValue(undefined)
    withClipboard(writeText)
    const { helper } = await openHelper()
    fireEvent.change(within(helper).getByLabelText("Assistant's reply"), {
      target: {
        value:
          'Step 1\nSubject: One\nBody:\nFirst {{ first_name }}\n\nStep 2\nSubject: Two\nBody:\nSecond {{ first_name }}',
      },
    })
    fireEvent.click(within(helper).getByRole('button', { name: 'Paste result' }))
    expect(screen.getByLabelText('Body')).toHaveValue('First {{ first_name }}')
    expect(
      within(helper).getByText('Filled the subject and body from step 1 of 2.'),
    ).toBeInTheDocument()

    const others = within(helper).getByRole('region', { name: 'Other steps' })
    expect(within(others).queryByRole('button', { name: 'Use step 1 here' })).toBeNull()
    fireEvent.click(within(others).getByRole('button', { name: 'Copy step 2' }))
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        'Step 2\nSubject: Two\nBody:\nSecond {{ first_name }}\nEnd of step 2',
      ),
    )
    fireEvent.click(within(others).getByRole('button', { name: 'Use step 2 here' }))
    expect(screen.getByLabelText('Subject')).toHaveValue('Two')
    expect(screen.getByLabelText('Body')).toHaveValue('Second {{ first_name }}')
    expect(
      within(helper).getByText('Filled the subject and body from step 2 of 2.'),
    ).toBeInTheDocument()
    const after = within(helper).getByRole('region', { name: 'Other steps' })
    expect(within(after).queryByRole('button', { name: 'Use step 2 here' })).toBeNull()
    fireEvent.click(within(after).getByRole('button', { name: 'Use step 1 here' }))
    expect(screen.getByLabelText('Body')).toHaveValue('First {{ first_name }}')
  })

  it('offers to undo a paste that replaced text, until the next edit', async () => {
    withClipboard(undefined)
    const { helper } = await openHelper()
    const reply = within(helper).getByLabelText("Assistant's reply")
    fireEvent.change(reply, { target: { value: 'Subject: New\nBody:\nNew {{ first_name }}' } })
    fireEvent.click(within(helper).getByRole('button', { name: 'Paste result' }))
    expect(screen.getByLabelText('Body')).toHaveValue('New {{ first_name }}')

    fireEvent.click(within(helper).getByRole('button', { name: 'Undo paste' }))
    expect(screen.getByLabelText('Subject')).toHaveValue('Hi {{ first_name }}')
    expect(screen.getByLabelText('Body')).toHaveValue('Hi {{ first_name }} at {{ company }}.')
    expect(within(helper).queryByRole('button', { name: 'Undo paste' })).toBeNull()

    // Paste again, then type: the edit ends the undo.
    fireEvent.click(within(helper).getByRole('button', { name: 'Paste result' }))
    expect(within(helper).getByRole('button', { name: 'Undo paste' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Body'), { target: { value: 'Typed {{ first_name }}' } })
    expect(within(helper).queryByRole('button', { name: 'Undo paste' })).toBeNull()
  })

  it('offers to undo Use step N here too', async () => {
    withClipboard(undefined)
    const { helper } = await openHelper(template({ subject: '', body: '' }))
    fireEvent.change(within(helper).getByLabelText("Assistant's reply"), {
      target: {
        value:
          'Step 1\nSubject: One\nBody:\nA {{ first_name }}\n\nStep 2\nSubject: Two\nBody:\nB {{ first_name }}',
      },
    })
    fireEvent.click(within(helper).getByRole('button', { name: 'Paste result' }))
    // Nothing was there to lose, so nothing to undo.
    expect(within(helper).queryByRole('button', { name: 'Undo paste' })).toBeNull()
    fireEvent.click(within(helper).getByRole('button', { name: 'Use step 2 here' }))
    fireEvent.click(within(helper).getByRole('button', { name: 'Undo paste' }))
    expect(screen.getByLabelText('Subject')).toHaveValue('One')
    expect(screen.getByLabelText('Body')).toHaveValue('A {{ first_name }}')
  })

  it('leaves the subject alone for an email step that has none', async () => {
    withClipboard(undefined)
    const { helper } = await openHelper()
    fireEvent.change(within(helper).getByLabelText("Assistant's reply"), {
      target: { value: 'Body:\nOnly a body, {{ first_name }}' },
    })
    fireEvent.click(within(helper).getByRole('button', { name: 'Paste result' }))
    expect(screen.getByLabelText('Subject')).toHaveValue('Hi {{ first_name }}')
    expect(screen.getByLabelText('Body')).toHaveValue('Only a body, {{ first_name }}')
    expect(within(helper).getByText('Filled the body.')).toBeInTheDocument()
  })

  it('includes the current text only when you tick the box', async () => {
    const writeText = vi.fn<(text: string) => Promise<void>>().mockResolvedValue(undefined)
    withClipboard(writeText)
    const { helper } = await openHelper()
    const copy = await within(helper).findByRole('button', { name: 'Copy prompt' })
    await waitFor(() => expect(copy).toBeEnabled(), WAIT)
    const include = within(helper).getByRole('checkbox', { name: 'Include the current text' })
    expect(include).not.toBeChecked()
    expect(include).toHaveAccessibleDescription(/only merge-field placeholders unless you typed/)

    fireEvent.click(copy)
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    expect(writeText.mock.calls[0]?.[0]).not.toContain('Current template to improve')

    fireEvent.click(include)
    fireEvent.click(copy)
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2))
    expect(writeText.mock.calls[1]?.[0]).toContain(
      'Current template to improve:\n\nSubject: Hi {{ first_name }}\nBody:\nHi {{ first_name }} at {{ company }}.',
    )
  })

  it('never saves the template on Enter in the helper', async () => {
    withClipboard(undefined)
    const { seen, helper } = await openHelper()
    const editorForm = screen.getByLabelText('Body').closest('form')
    expect(editorForm).not.toBeNull()
    for (const name of ['What the campaign is for', "Who it's for", 'Tone', 'Steps']) {
      const field = within(helper).getByLabelText(name) as HTMLInputElement | HTMLSelectElement
      expect(field.form).not.toBe(editorForm)
      fireEvent.keyDown(field, { key: 'Enter', code: 'Enter' })
      if (field.form !== null) fireEvent.submit(field.form)
    }
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)))
    expect(requestsTo(seen, 'PATCH', '/api/v1/templates/1')).toEqual([])
    expect(requestsTo(seen, 'POST', '/api/v1/templates')).toEqual([])
  })

  it('never saves the template on Enter on the include checkbox', async () => {
    withClipboard(undefined)
    const { seen, helper } = await openHelper()
    // An edit enables Save, the form's default submitter, which a stray Enter would click.
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'reconnect 2' } })
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
    const include = within(helper).getByRole('checkbox', { name: 'Include the current text' })
    // Base UI's checkbox handles Enter itself: it clicks the default submitter of its hidden
    // input's form, in a microtask. jsdom has no implicit submission, so this is the whole path.
    fireEvent.keyDown(include, { key: 'Enter', code: 'Enter' })
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)))
    expect(requestsTo(seen, 'PATCH', '/api/v1/templates/1')).toEqual([])
    expect(requestsTo(seen, 'POST', '/api/v1/templates')).toEqual([])
    expect(include).not.toBeChecked()
  })

  it('sits below the channel and above the subject', async () => {
    withClipboard(undefined)
    const { helper } = await openHelper()
    const follows = (a: Node, b: Node) =>
      (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0
    expect(follows(screen.getByLabelText('Channel'), helper)).toBe(true)
    expect(follows(helper, screen.getByLabelText('Subject'))).toBe(true)
  })

  it('leaves the subject alone for a LinkedIn template, and says why', async () => {
    withClipboard(undefined)
    const { helper } = await openHelper(template({ channel: 'linkedin', subject: null }))
    fireEvent.change(within(helper).getByLabelText("Assistant's reply"), {
      target: { value: 'Subject: Nope\nBody:\nHi {{ first_name }}' },
    })
    fireEvent.click(within(helper).getByRole('button', { name: 'Paste result' }))
    expect(screen.getByLabelText('Subject')).toHaveValue('')
    expect(screen.getByLabelText('Body')).toHaveValue('Hi {{ first_name }}')
    expect(within(helper).getByText(/the reply's subject was left out/)).toBeInTheDocument()
  })

  it('links the guide', async () => {
    withClipboard(undefined)
    const { helper } = await openHelper()
    expect(within(helper).getByRole('link', { name: 'Read the guide' })).toHaveAttribute(
      'href',
      'https://github.com/dsmorgan/netkeeper/blob/main/docs/ai-drafting.md',
    )
  })
})
