/**
 * Merge in the UI (#363): the contact page's Merge with…, the preview, the
 * confirmation, and the possible-duplicate hint on the review band.
 *
 * Every name, address, and number here is invented.
 */

import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { components } from '@/api/schema'
import { describeMoves, labelPair } from '@/features/contacts/merge-text'

import {
  contactDetail,
  contactPage,
  contactRow,
  mockApi,
  type SeenRequest,
} from './contacts-fixtures'
import { jsonResponse } from './fetch'
import { renderApp } from './render'

type Schemas = components['schemas']
type Detail = Schemas['ContactDetail']

const NO_MOVES: Schemas['MergeMovesOut'] = {
  emails: { moved: 0, dropped: 0 },
  phones: { moved: 0, dropped: 0 },
  links: { moved: 0, dropped: 0 },
  positions: { moved: 0, dropped: 0 },
  snapshots: 0,
  interactions: 0,
  tags_added: [],
  tags_removed: [],
  lists_added: [],
  enrollments_moved: 0,
  enrollments_combined: 0,
  messages_moved: 0,
  messages_discarded: 0,
  history_rows: 0,
}

const ADA = contactDetail({ id: 1 })
const BO = contactDetail({
  id: 2,
  first_name: 'Bo',
  last_name: 'Marsh',
  preferred_name: 'Bo',
  li_urn: null,
  li_public_id: 'bo-marsh-new',
  headline: 'Card headline',
  emails: [
    {
      id: 20,
      email: 'bo@example.test',
      kind: 'other',
      is_primary: true,
      status: 'ok',
      source: 'manual',
      observed_at: '2026-01-02T09:00:00Z',
    },
  ],
})

const MOVES: Schemas['MergeMovesOut'] = {
  ...NO_MOVES,
  emails: { moved: 1, dropped: 0 },
  enrollments_moved: 1,
  enrollments_combined: 1,
  messages_moved: 3,
  messages_discarded: 1,
}

/** The preview the backend would give, with an "after" that differs from both sides. */
function previewOf(survivor: Detail, loser: Detail): Schemas['MergePreviewOut'] {
  return {
    survivor,
    loser,
    result: {
      ...survivor,
      headline: null,
      emails: [...survivor.emails, ...loser.emails],
      met: 'met',
    },
    moves: MOVES,
    undoable: false,
  }
}

const CONTACTS: Record<number, Detail> = { 1: ADA, 2: BO }

function serve(
  options: {
    contacts?: Record<number, Detail>
    duplicates?: Schemas['PossibleDuplicateOut'][]
  } = {},
): SeenRequest[] {
  const contacts = options.contacts ?? CONTACTS
  return mockApi((request, body) => {
    const { pathname } = new URL(request.url)
    const one = /^\/api\/v1\/contacts\/(\d+)$/.exec(pathname)
    if (one && request.method === 'GET') {
      const found = contacts[Number(one[1])]
      return found ? jsonResponse(found) : jsonResponse({ detail: 'no such contact' }, 404)
    }
    if (/^\/api\/v1\/contacts\/\d+\/tags$/.test(pathname)) return jsonResponse([])
    if (/^\/api\/v1\/contacts\/\d+\/duplicates$/.test(pathname)) {
      return jsonResponse(options.duplicates ?? [])
    }
    if (pathname === '/api/v1/contacts/query') {
      return jsonResponse(
        contactPage([
          contactRow(1, { first_name: 'Ada', last_name: 'Ventura', preferred_name: 'Ada' }),
          contactRow(2, {
            first_name: 'Bo',
            last_name: 'Marsh',
            preferred_name: 'Bo',
            primary_email: 'bo@example.test',
          }),
        ]),
      )
    }
    const preview = /^\/api\/v1\/contacts\/(\d+)\/merge\/preview$/.exec(pathname)
    if (preview) {
      const loserId = (body as { loser_id: number }).loser_id
      const survivor = contacts[Number(preview[1])] as Detail
      return jsonResponse(previewOf(survivor, contacts[loserId] as Detail))
    }
    const merge = /^\/api\/v1\/contacts\/(\d+)\/merge$/.exec(pathname)
    if (merge && request.method === 'POST') {
      const survivor = contacts[Number(merge[1])] as Detail
      return jsonResponse(survivor)
    }
    return undefined
  })
}

function sent(seen: readonly SeenRequest[], path: string): SeenRequest[] {
  return seen.filter((entry) => entry.method === 'POST' && entry.path === path)
}

async function openPanelAndPickBo() {
  fireEvent.click(await screen.findByRole('button', { name: /Merge with…/ }))
  const panel = await screen.findByRole('region', { name: 'Merge contacts' })
  fireEvent.change(within(panel).getByLabelText('Find the other contact'), {
    target: { value: 'bo' },
  })
  const matches = await within(panel).findByRole('list', { name: 'Matches' })
  // The page's own contact is never offered as the other one.
  expect(within(matches).queryByRole('button', { name: /Ada Ventura/ })).toBeNull()
  fireEvent.click(within(matches).getByRole('button', { name: /Bo Marsh/ }))
  return panel
}

describe('merge from the contact page', () => {
  it('searches by name or email, previews side by side, and swaps the survivor', async () => {
    const seen = serve()
    await renderApp('/contacts/1')
    const panel = await openPanelAndPickBo()

    const [search] = seen.filter((entry) => entry.path === '/api/v1/contacts/query')
    const where = JSON.stringify((search?.body as { filter: unknown }).filter)
    expect(where).toContain('"preferred_name"')
    expect(where).toContain('"email_contains"')

    const table = await within(panel).findByRole('table')
    expect(within(table).getByRole('columnheader', { name: 'Stays: Ada Ventura' })).toBeVisible()
    expect(within(table).getByRole('columnheader', { name: 'Merged away: Bo Marsh' })).toBeVisible()
    const headline = within(table).getByRole('row', { name: /Headline/ })
    expect(
      within(headline)
        .getAllByRole('cell')
        .map((cell) => cell.textContent),
    ).toEqual(['Principal Engineer at Tessellate Labs', 'Card headline', '—'])
    expect(within(panel).getByRole('list', { name: 'What moves' })).toHaveTextContent(
      '1 campaign enrollment combines',
    )
    expect(sent(seen, '/api/v1/contacts/1/merge/preview')[0]?.body).toEqual({ loser_id: 2 })

    fireEvent.click(within(panel).getByRole('button', { name: 'Keep Bo Marsh instead' }))
    await waitFor(() => expect(sent(seen, '/api/v1/contacts/2/merge/preview')).toHaveLength(1))
    expect(sent(seen, '/api/v1/contacts/2/merge/preview')[0]?.body).toEqual({ loser_id: 1 })
    expect(
      await within(panel).findByRole('columnheader', { name: 'Stays: Bo Marsh' }),
    ).toBeVisible()
    expect(sent(seen, '/api/v1/contacts/1/merge')).toHaveLength(0)
  })

  it('merges only after the confirmation, once, and says it cannot be undone', async () => {
    const seen = serve()
    const { router } = await renderApp('/contacts/1')
    const panel = await openPanelAndPickBo()
    fireEvent.click(within(panel).getByRole('button', { name: 'Keep Bo Marsh instead' }))
    await within(panel).findByRole('columnheader', { name: 'Stays: Bo Marsh' })

    fireEvent.click(within(panel).getByRole('button', { name: 'Merge…' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Merge Ada Ventura into Bo Marsh?')
    expect(dialog).toHaveTextContent('1 email address moves')
    expect(dialog).toHaveTextContent('1 campaign enrollment moves')
    expect(dialog).toHaveTextContent('3 campaign messages move')
    expect(dialog).toHaveTextContent(/A merge can.t be undone/)
    expect(sent(seen, '/api/v1/contacts/2/merge')).toHaveLength(0)

    const confirm = within(dialog).getByRole('button', { name: 'Merge' })
    await act(async () => {
      fireEvent.click(confirm)
      fireEvent.click(confirm)
      await Promise.resolve()
    })
    await waitFor(() => expect(router.state.location.pathname).toBe('/contacts/2'))
    expect(sent(seen, '/api/v1/contacts/2/merge')).toHaveLength(1)
    // The previews were dropped, not refetched into a 409 against a merged-away contact.
    await screen.findByRole('heading', { name: 'Bo Marsh' })
    expect(seen.filter((entry) => entry.path.endsWith('/merge/preview'))).toHaveLength(2)
    expect(sent(seen, '/api/v1/contacts/2/merge')[0]?.body).toEqual({ loser_id: 1 })
  })

  it('cancels without merging, and keeps the controls on the keyboard', async () => {
    const seen = serve()
    await renderApp('/contacts/1')

    const open = await screen.findByRole('button', { name: /Merge with…/ })
    expect(open.tagName).toBe('BUTTON')
    open.focus()
    expect(document.activeElement).toBe(open)
    fireEvent.click(open)
    const panel = await screen.findByRole('region', { name: 'Merge contacts' })
    // The search takes focus as the panel opens, so typing starts right away.
    const search = within(panel).getByLabelText('Find the other contact')
    expect(document.activeElement).toBe(search)
    fireEvent.change(search, { target: { value: 'bo' } })
    const match = await within(panel).findByRole('button', { name: /Bo Marsh/ })
    expect(match.tagName).toBe('BUTTON')
    fireEvent.click(match)

    const merge = await within(panel).findByRole('button', { name: 'Merge…' })
    for (const control of [
      merge,
      within(panel).getByRole('button', { name: 'Keep Bo Marsh instead' }),
      within(panel).getByRole('button', { name: 'Choose another contact' }),
      within(panel).getByRole('button', { name: 'Close' }),
    ]) {
      expect(control.tagName).toBe('BUTTON')
      expect(control).not.toHaveAttribute('tabindex', '-1')
    }
    fireEvent.click(merge)
    const dialog = await screen.findByRole('alertdialog')
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true))

    fireEvent.keyDown(document.activeElement ?? dialog, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(sent(seen, '/api/v1/contacts/1/merge')).toHaveLength(0)
  })

  it('shows why the two cannot be merged', async () => {
    mockApi((request) => {
      const { pathname } = new URL(request.url)
      if (pathname === '/api/v1/contacts/1' && request.method === 'GET') return jsonResponse(ADA)
      if (pathname === '/api/v1/contacts/1/tags') return jsonResponse([])
      if (pathname === '/api/v1/contacts/query') {
        return jsonResponse(
          contactPage([
            contactRow(2, { first_name: 'Bo', last_name: 'Marsh', preferred_name: 'Bo' }),
          ]),
        )
      }
      if (pathname === '/api/v1/contacts/1/merge/preview') {
        return jsonResponse({ detail: 'contact 2 is already merged into 3, not 1' }, 409)
      }
      return undefined
    })
    await renderApp('/contacts/1')
    const panel = await openPanelAndPickBo()
    expect(await within(panel).findByRole('alert')).toHaveTextContent(
      /already merged into 3, not 1/,
    )
    expect(within(panel).queryByRole('button', { name: 'Merge…' })).toBeNull()
  })
})

describe('two records with one name', () => {
  const email = (id: number, address: string) => ({
    id,
    email: address,
    kind: 'other' as const,
    is_primary: true,
    status: 'ok' as const,
    source: 'manual' as const,
    observed_at: '2026-01-02T09:00:00Z',
  })

  /** Two Ada Quills, 1 and 3, served with a picker that finds 3 and a preview of the pair. */
  function serveTwins(first: Partial<Detail>, second: Partial<Detail>) {
    const base = { first_name: 'Ada', last_name: 'Quill', preferred_name: 'Ada' }
    const contacts: Record<number, Detail> = {
      1: contactDetail({ id: 1, ...base, ...first }),
      3: contactDetail({ id: 3, ...base, li_urn: null, ...second }),
    }
    mockApi((request, body) => {
      const { pathname } = new URL(request.url)
      const one = /^\/api\/v1\/contacts\/(\d+)$/.exec(pathname)
      if (one && request.method === 'GET') return jsonResponse(contacts[Number(one[1])])
      if (/\/tags$/.test(pathname)) return jsonResponse([])
      if (pathname === '/api/v1/contacts/query') {
        return jsonResponse(contactPage([contactRow(3, { ...base, primary_email: null })]))
      }
      const preview = /^\/api\/v1\/contacts\/(\d+)\/merge\/preview$/.exec(pathname)
      if (preview) {
        const loserId = (body as { loser_id: number }).loser_id
        return jsonResponse(
          previewOf(contacts[Number(preview[1])] as Detail, contacts[loserId] as Detail),
        )
      }
      return undefined
    })
  }

  /** Opens the merge from contact 1, picks contact 3, and reads every place the pair is named. */
  async function namesInTheMerge(): Promise<string[]> {
    await renderApp('/contacts/1')
    fireEvent.click(await screen.findByRole('button', { name: /Merge with…/ }))
    const panel = await screen.findByRole('region', { name: 'Merge contacts' })
    fireEvent.change(within(panel).getByLabelText('Find the other contact'), {
      target: { value: 'ada' },
    })
    fireEvent.click(await within(panel).findByRole('button', { name: /Ada Quill/ }))
    const stays = await within(panel).findByRole('columnheader', { name: /^Stays: / })
    // Read the panel before the dialog opens: a modal hides everything behind it.
    const names = [
      within(panel).getByRole('heading', { level: 3 }).textContent ?? '',
      stays.textContent ?? '',
      within(panel).getByRole('columnheader', { name: /^Merged away: / }).textContent ?? '',
      panel.querySelector('p')?.textContent ?? '',
      within(panel).getByRole('button', { name: /^Keep / }).textContent ?? '',
    ]
    fireEvent.click(within(panel).getByRole('button', { name: 'Merge…' }))
    const dialog = await screen.findByRole('alertdialog')
    return [...names, dialog.textContent ?? '']
  }

  it.each([
    {
      case: 'different emails',
      first: { emails: [email(10, 'ada@quill.test')] },
      second: { emails: [email(30, 'ada.q@quill.test')] },
      labels: ['Ada Quill (ada@quill.test)', 'Ada Quill (ada.q@quill.test)'],
    },
    {
      case: 'the same email, different companies',
      first: { emails: [email(10, 'ada@quill.test')], current_company: 'Quill Press' },
      second: { emails: [email(30, 'ADA@quill.test')], current_company: 'Tessellate Labs' },
      labels: ['Ada Quill (Quill Press)', 'Ada Quill (Tessellate Labs)'],
    },
    {
      case: 'the same email and company, different LinkedIn ids',
      first: { emails: [email(10, 'ada@quill.test')], li_public_id: 'ada-quill' },
      second: { emails: [email(30, 'ada@quill.test')], li_public_id: 'ada-quill-2' },
      labels: ['Ada Quill (ada-quill)', 'Ada Quill (ada-quill-2)'],
    },
  ])('labels the pair by the first detail that differs: $case', async (example) => {
    serveTwins(example.first, example.second)
    const [keep, fold] = example.labels as [string, string]
    const [heading, stays, away, sentence, swap, dialog] = await namesInTheMerge()

    expect(heading).toBe(`Merge ${keep} with ${fold}`)
    expect(stays).toBe(`Stays: ${keep}`)
    expect(away).toBe(`Merged away: ${fold}`)
    expect(sentence).toContain(`${keep} stays. ${fold} is merged into them`)
    expect(swap).toBe(`Keep ${fold} instead`)
    expect(dialog).toContain(`Merge ${fold} into ${keep}?`)
    expect(dialog).toContain(`${keep} keeps its record. Everything below moves to it from ${fold}`)
  })
})

describe('labelPair', () => {
  const ada = { first_name: 'Ada', last_name: 'Quill', preferred_name: 'Ada' }

  it('needs no detail when the names differ', () => {
    expect(labelPair({ id: 1, ...ada }, { id: 2, first_name: 'Bo', last_name: 'Marsh' })).toEqual([
      'Ada Quill',
      'Bo Marsh',
    ])
  })

  it('falls back to the date added, then the id', () => {
    const same = {
      primary_email: 'ada@quill.test',
      current_company: 'Quill Press',
      li_public_id: null,
    }
    expect(
      labelPair(
        { id: 1, ...ada, ...same, created_at: '2025-03-01T09:00:00Z' },
        { id: 2, ...ada, ...same, created_at: '2026-01-02T09:00:00Z' },
      ),
    ).toEqual(['Ada Quill (added 2025-03-01)', 'Ada Quill (added 2026-01-02)'])
    expect(
      labelPair(
        { id: 1, ...ada, ...same, created_at: '2026-01-02T09:00:00Z' },
        { id: 2, ...ada, ...same, created_at: '2026-01-02T10:00:00Z' },
      ),
    ).toEqual(['Ada Quill (contact 1)', 'Ada Quill (contact 2)'])
  })

  it('skips a detail one side has not loaded, and says so when one side has none', () => {
    expect(
      labelPair(
        { id: 1, ...ada, primary_email: undefined, current_company: 'Quill Press' },
        { id: 2, ...ada, primary_email: 'ada@quill.test', current_company: null },
      ),
    ).toEqual(['Ada Quill (Quill Press)', 'Ada Quill (no company)'])
  })
})

describe('the possible-duplicate hint', () => {
  const waiting = { ...BO, needs_review_at: '2026-09-24T12:00:00Z' }
  const ada: Schemas['PossibleDuplicateOut'] = {
    contact_id: 1,
    first_name: 'Ada',
    last_name: 'Ventura',
    preferred_name: 'Ada',
    current_title: 'Principal Engineer',
    current_company: 'Tessellate Labs',
    li_public_id: 'ada-ventura-fake',
    needs_review: false,
    matched_by: ['name'],
    linkedin_ids_differ: true,
  }

  it('sits in the review band and opens the merge with the confirmed contact kept', async () => {
    const seen = serve({ contacts: { 1: ADA, 2: waiting }, duplicates: [ada] })
    await renderApp('/contacts/2')

    const band = await screen.findByRole('region', { name: 'Needs review' })
    const hint = await within(band).findByRole('list', { name: 'Possible duplicates' })
    expect(hint).toHaveTextContent('Possible duplicate of Ada Ventura')
    expect(hint).toHaveTextContent('the same name; LinkedIn ids differ')
    expect(within(hint).getByRole('link', { name: 'Ada Ventura' })).toHaveAttribute(
      'href',
      '/contacts/1',
    )
    // Read-only: showing the hint wrote nothing.
    expect(seen.filter((entry) => entry.method !== 'GET')).toHaveLength(0)

    fireEvent.click(within(hint).getByRole('button', { name: 'Merge with Ada Ventura' }))
    const panel = await screen.findByRole('region', { name: 'Merge contacts' })
    expect(
      await within(panel).findByRole('columnheader', {
        name: 'Stays: Ada Ventura',
      }),
    ).toBeVisible()
    expect(sent(seen, '/api/v1/contacts/1/merge/preview')[0]?.body).toEqual({ loser_id: 2 })
    expect(sent(seen, '/api/v1/contacts/1/merge')).toHaveLength(0)
  })

  it('says nothing when there is no likely duplicate', async () => {
    serve({ contacts: { 1: ADA, 2: waiting }, duplicates: [] })
    await renderApp('/contacts/2')
    await screen.findByRole('region', { name: 'Needs review' })
    await waitFor(() =>
      expect(screen.queryByRole('list', { name: 'Possible duplicates' })).toBeNull(),
    )
  })
})

describe('describeMoves', () => {
  it('says one line per kind that moves, with the verb agreeing', () => {
    expect(
      describeMoves({
        ...NO_MOVES,
        emails: { moved: 2, dropped: 1 },
        phones: { moved: 1, dropped: 0 },
        tags_added: ['Climbing'],
        lists_added: ['First 100'],
        history_rows: 1,
      }),
    ).toEqual([
      '2 email addresses move; 1 already on the survivor',
      '1 phone number moves',
      'Tags added: Climbing',
      'Lists joined: First 100',
      '1 old-campaign record moves',
    ])
  })

  it('says so when nothing but the fields move', () => {
    expect(describeMoves(NO_MOVES)).toEqual([
      'Nothing but the fields above: the other contact has no other records.',
    ])
  })
})
