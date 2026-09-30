/**
 * "Add contact" on the Contacts page (#303): the form's own checks, a success
 * that opens the new contact, the backend's per-field refusals, and a duplicate
 * that offers the contact already there. Every person here is invented.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { jsonResponse } from './fetch'
import { renderApp } from './render'
import {
  contactDetail,
  contactPage,
  contactRow,
  mockApi,
  type SeenRequest,
} from './contacts-fixtures'

const TAG = {
  id: 3,
  name: 'friends',
  color: null,
  contact_count: 0,
  created_at: '2026-01-02T09:00:00Z',
  updated_at: '2026-01-02T09:00:00Z',
  kind: 'manual',
  met_signal: null,
}

function list(id: number, name: string, kind: 'static' | 'smart') {
  return {
    id,
    name,
    kind,
    builtin: false,
    broken: null,
    filter: null,
    member_count: 0,
    created_at: '2026-01-02T09:00:00Z',
    updated_at: '2026-01-02T09:00:00Z',
  }
}

/** The table, its tags and lists, and `POST /contacts` answered by `create`. */
function serve(create: (body: Record<string, unknown>) => Response): SeenRequest[] {
  return mockApi((request, body) => {
    const { pathname } = new URL(request.url)
    if (pathname === '/api/v1/contacts/query') return jsonResponse(contactPage([contactRow(1)]))
    if (pathname === '/api/v1/tags') return jsonResponse([TAG])
    if (pathname === '/api/v1/lists') {
      return jsonResponse([list(5, 'Keep warm', 'static'), list(6, 'Engineers', 'smart')])
    }
    if (pathname === '/api/v1/contacts' && request.method === 'POST') {
      return create(body as Record<string, unknown>)
    }
    if (pathname === '/api/v1/contacts/42') return jsonResponse(contactDetail({ id: 42 }))
    return undefined
  })
}

function posts(seen: readonly SeenRequest[]) {
  return seen.filter((entry) => entry.path === '/api/v1/contacts' && entry.method === 'POST')
}

async function openForm() {
  fireEvent.click(await screen.findByRole('button', { name: 'Add contact' }))
  return screen.findByRole('form', { name: 'Add a contact' })
}

function type(form: HTMLElement, label: string, value: string) {
  fireEvent.change(within(form).getByLabelText(label), { target: { value } })
}

function submit(form: HTMLElement) {
  fireEvent.click(within(form).getByRole('button', { name: 'Add contact' }))
}

describe('add contact', () => {
  it('needs a name, and a well-formed address and profile URL, before it sends anything', async () => {
    const seen = serve(() => jsonResponse({}, 500))
    await renderApp('/contacts')
    const form = await openForm()

    submit(form)
    expect(await within(form).findByText('Enter a first name or a last name.')).toBeVisible()
    expect(within(form).getByLabelText('First name')).toHaveAttribute('aria-invalid', 'true')

    type(form, 'Last name', 'Halloway')
    type(form, 'Email', 'wren at example')
    type(form, 'LinkedIn URL', 'https://example.test/wren')
    submit(form)
    expect(
      await within(form).findByText('Enter one email address, such as name@example.com.'),
    ).toBeVisible()
    expect(
      within(form).getByText(
        'Enter a LinkedIn profile URL, such as https://www.linkedin.com/in/name.',
      ),
    ).toBeVisible()
    expect(within(form).queryByText('Enter a first name or a last name.')).toBeNull()
    expect(posts(seen)).toEqual([])
  })

  it('sends the contact with its tag and list, and opens it', async () => {
    const seen = serve(() => jsonResponse(contactDetail({ id: 42 }), 201))
    const { router } = await renderApp('/contacts')
    const form = await openForm()

    // Only a static list takes members, so a smart list is not offered.
    const lists = await within(form).findByLabelText('Add to list')
    expect(within(lists).queryByRole('option', { name: 'Engineers' })).toBeNull()

    type(form, 'First name', '  Wren ')
    type(form, 'Last name', 'Halloway')
    type(form, 'Email', 'wren@example.test')
    type(form, 'Company', 'Brindle Works')
    type(form, 'LinkedIn URL', 'linkedin.com/in/wren-fake')
    fireEvent.click(await within(form).findByRole('checkbox', { name: 'friends' }))
    fireEvent.change(lists, { target: { value: '5' } })
    submit(form)

    await waitFor(() => expect(router.state.location.pathname).toBe('/contacts/42'))
    expect(posts(seen).map((entry) => entry.body)).toEqual([
      {
        first_name: 'Wren',
        last_name: 'Halloway',
        email: 'wren@example.test',
        current_company: 'Brindle Works',
        current_title: null,
        li_url: 'linkedin.com/in/wren-fake',
        tag_ids: [3],
        list_id: 5,
        allow_name_match: false,
      },
    ])
    await waitFor(() => expect(screen.queryByRole('form', { name: 'Add a contact' })).toBeNull())
  })

  it('offers the contact already there when the address matches', async () => {
    serve(() =>
      jsonResponse(
        {
          detail: 'duplicate',
          contact_id: 7,
          contact_ids: [7],
          matched_by: 'email',
          archived: true,
        },
        409,
      ),
    )
    const { router } = await renderApp('/contacts')
    const form = await openForm()
    type(form, 'First name', 'Ada')
    type(form, 'Email', 'ada@example.test')
    submit(form)

    const alert = await within(form).findByRole('alert')
    expect(alert).toHaveTextContent(
      'Already a contact: contact 7 has that email address and is archived.',
    )
    // A match by address is certain: nothing to add anyway.
    expect(within(alert).queryByRole('button', { name: 'Add anyway' })).toBeNull()
    fireEvent.click(within(alert).getByRole('link', { name: 'Open contact 7' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/contacts/7'))
  })

  it('adds anyway after a match on name and company alone', async () => {
    const seen = serve((body) =>
      body.allow_name_match === true
        ? jsonResponse(contactDetail({ id: 42 }), 201)
        : jsonResponse(
            {
              detail: 'duplicate',
              contact_id: 9,
              contact_ids: [9],
              matched_by: 'name',
              archived: false,
            },
            409,
          ),
    )
    const { router } = await renderApp('/contacts')
    const form = await openForm()
    type(form, 'First name', 'Ada')
    type(form, 'Last name', 'Quill')
    type(form, 'Company', 'Blueleaf')
    submit(form)

    const alert = await within(form).findByRole('alert')
    expect(alert).toHaveTextContent('This may already be a contact: contact 9 has the same name')
    fireEvent.click(within(alert).getByRole('button', { name: 'Add anyway' }))

    await waitFor(() => expect(router.state.location.pathname).toBe('/contacts/42'))
    expect(
      posts(seen).map((entry) => (entry.body as { allow_name_match: boolean }).allow_name_match),
    ).toEqual([false, true])
  })

  it('puts the backend’s refusal beside the field it names', async () => {
    serve(() =>
      jsonResponse(
        {
          detail: [
            {
              loc: ['body', 'email'],
              msg: "'wren@example.test, x@example.test' is not an email address",
              type: 'value_error',
            },
          ],
        },
        422,
      ),
    )
    await renderApp('/contacts')
    const form = await openForm()
    type(form, 'First name', 'Wren')
    // Loose enough for the form's own check; the backend's is stricter.
    type(form, 'Email', 'wren@example.test')
    submit(form)

    expect(
      await within(form).findByText("'wren@example.test, x@example.test' is not an email address"),
    ).toBeVisible()
    expect(within(form).getByLabelText('Email')).toHaveAttribute('aria-invalid', 'true')
    expect(within(form).queryByRole('alert')).toBeNull()

    // Editing the field clears what the backend said about it.
    type(form, 'Email', 'wren@example.org')
    expect(within(form).getByLabelText('Email')).toHaveAttribute('aria-invalid', 'false')
  })

  it('links a tag or list refusal to its control, and a new choice clears it', async () => {
    serve(() =>
      jsonResponse(
        {
          detail: [
            { loc: ['body', 'tag_ids'], msg: 'no tag 3', type: 'value_error' },
            { loc: ['body', 'list_id'], msg: 'no list 5', type: 'value_error' },
          ],
        },
        422,
      ),
    )
    await renderApp('/contacts')
    const form = await openForm()
    type(form, 'First name', 'Wren')
    const tag = await within(form).findByRole('checkbox', { name: 'friends' })
    fireEvent.click(tag)
    const list = await within(form).findByLabelText('Add to list')
    fireEvent.change(list, { target: { value: '5' } })
    submit(form)

    const tagError = await within(form).findByText('no tag 3')
    const listError = within(form).getByText('no list 5')
    const tags = within(form).getByRole('group', { name: 'Tags' })
    expect(tags).toHaveAttribute('aria-describedby', tagError.id)
    expect(list).toHaveAttribute('aria-describedby', listError.id)
    expect(list).toHaveAttribute('aria-invalid', 'true')
    // Both named fields sit beside their controls; nothing is said twice in an alert.
    expect(within(form).queryByRole('alert')).toBeNull()

    fireEvent.change(list, { target: { value: '' } })
    expect(within(form).queryByText('no tag 3')).toBeNull()
    expect(within(form).queryByText('no list 5')).toBeNull()
    expect(list).not.toHaveAttribute('aria-describedby')
    expect(within(form).queryByRole('alert')).toBeNull()
  })

  it('drops the duplicate banner when a tag changes', async () => {
    serve(() =>
      jsonResponse(
        {
          detail: 'duplicate',
          contact_id: 7,
          contact_ids: [7],
          matched_by: 'name',
          archived: false,
        },
        409,
      ),
    )
    await renderApp('/contacts')
    const form = await openForm()
    type(form, 'First name', 'Ada')
    submit(form)
    expect(await within(form).findByRole('alert')).toHaveTextContent('contact 7')

    fireEvent.click(await within(form).findByRole('checkbox', { name: 'friends' }))
    expect(within(form).queryByRole('alert')).toBeNull()
  })

  it('takes a LinkedIn URL with a port, as the backend does', async () => {
    const seen = serve(() => jsonResponse(contactDetail({ id: 42 }), 201))
    const { router } = await renderApp('/contacts')
    const form = await openForm()
    type(form, 'First name', 'Wren')
    type(form, 'LinkedIn URL', 'https://www.linkedin.com:443/in/wren-fake/')
    submit(form)
    await waitFor(() => expect(router.state.location.pathname).toBe('/contacts/42'))
    expect(posts(seen)).toHaveLength(1)
  })

  it('says a failure that names no field on its own', async () => {
    serve(() => jsonResponse({ detail: 'database is locked' }, 503))
    await renderApp('/contacts')
    const form = await openForm()
    type(form, 'Last name', 'Halloway')
    submit(form)
    expect(await within(form).findByRole('alert')).toHaveTextContent(
      'add contact: database is locked',
    )
  })
})
