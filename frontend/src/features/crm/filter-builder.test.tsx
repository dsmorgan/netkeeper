/**
 * The builder reaches every predicate, and what it builds is what the API reads.
 *
 * The reachability test walks the ops out of `openapi.json` rather than out of
 * the catalog, so a predicate the catalog forgot fails here too, not only in
 * `predicates.test.ts`.
 */
import { useState } from 'react'
import { fireEvent, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import openapiRaw from '../../../openapi.json?raw'

import { FilterBuilder } from './filter-builder'
import { mockApi, renderWithClient } from './harness'
import { PREDICATES } from './predicates'
import { emptyTree } from './tree'
import type { FilterNode, FilterTree, TagOut } from './types'
import { jsonResponse } from '@/test/fetch'

const doc = JSON.parse(openapiRaw) as {
  components: { schemas: Record<string, { discriminator?: { mapping: Record<string, string> } }> }
}
const LANGUAGE_OPS = Object.keys(
  doc.components.schemas['FilterNode-Input']?.discriminator?.mapping ?? {},
)

const TAGS: TagOut[] = [
  {
    id: 1,
    name: 'founder',
    color: null,
    kind: 'manual',
    met_signal: null,
    contact_count: 3,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 2,
    name: 'investor',
    color: null,
    kind: 'auto',
    met_signal: null,
    contact_count: 1,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
]

function countRoute(total = 7, describe_ = 'company contains "acme"') {
  return {
    'POST /api/v1/contacts/query': () => jsonResponse({ items: [], total, describe: describe_ }),
  }
}

function Harness({ initial, editingListId }: { initial: FilterTree; editingListId?: number }) {
  const [value, setValue] = useState(initial)
  return (
    <>
      <FilterBuilder value={value} onChange={setValue} tags={TAGS} editingListId={editingListId} />
      <pre data-testid="tree">{JSON.stringify(value)}</pre>
    </>
  )
}

function tree(): FilterTree {
  return JSON.parse(screen.getByTestId('tree').textContent ?? '{}') as FilterTree
}

function openPalette() {
  fireEvent.click(screen.getByRole('button', { name: /^Add a condition/ }))
}

/**
 * Clicks a predicate in the open palette by its op.
 *
 * The palette's buttons read "label, then hint" to a screen reader, which is
 * the right accessible name and a poor test selector, so the op attribute the
 * enumeration test already relies on picks the row here too.
 */
function pick(op: string) {
  const button = document.querySelector(`[data-op="${op}"]`)
  if (button === null) throw new Error(`the palette has no ${op}`)
  fireEvent.click(button)
}

describe('every predicate is reachable from the builder', () => {
  it('offers a control for each op the language defines', () => {
    mockApi(countRoute())
    const { container } = renderWithClient(<Harness initial={emptyTree()} />)
    openPalette()

    const missing = LANGUAGE_OPS.filter(
      (op) => container.querySelector(`[data-op="${op}"]`) === null,
    )
    expect(missing).toEqual([])
    expect(LANGUAGE_OPS.length).toBeGreaterThan(0)
  })

  it('makes the twenty-five usable predicates clickable', () => {
    mockApi(countRoute())
    const { container } = renderWithClient(<Harness initial={emptyTree()} />)
    openPalette()

    const usable = PREDICATES.filter((spec) => spec.unavailable === undefined)
    for (const spec of usable) {
      const element = container.querySelector(`[data-op="${spec.op}"]`)
      expect(element?.tagName, spec.op).toBe('BUTTON')
      expect(element?.getAttribute('aria-disabled')).toBeNull()
    }
    expect(usable).toHaveLength(LANGUAGE_OPS.length - 2)
  })

  it('shows the two the server refuses as unavailable, with the reason', () => {
    // `list_member` was the third until P1-27 taught the compiler to inline a
    // list; it is an ordinary clickable predicate now, with a picker of its own.
    mockApi(countRoute())
    const { container } = renderWithClient(<Harness initial={emptyTree()} />)
    openPalette()

    for (const op of ['enrolled_in', 'replied_in']) {
      const element = container.querySelector(`[data-op="${op}"]`)
      expect(element, op).not.toBeNull()
      expect(element?.getAttribute('data-unavailable')).toBe('true')
      expect(element?.getAttribute('aria-disabled')).toBe('true')
      expect(element?.tagName).not.toBe('BUTTON')
    }
    expect(container.querySelector('[data-op="list_member"]')?.tagName).toBe('BUTTON')
    expect(screen.getAllByText(/P3-04/).length).toBe(2)
  })
})

describe('building a filter', () => {
  it('produces the tree the API accepts', () => {
    mockApi(countRoute())
    renderWithClient(<Harness initial={emptyTree()} />)

    openPalette()
    pick('and')
    fireEvent.click(screen.getByRole('button', { name: /^Add a condition/ }))
    pick('contains')

    const row = document.querySelector('[data-op="contains"][data-slot="filter-node"]')
    expect(row).not.toBeNull()
    const scoped = within(row as HTMLElement)
    fireEvent.change(scoped.getByLabelText('Field'), { target: { value: 'current_company' } })
    fireEvent.change(scoped.getByLabelText('Value'), { target: { value: 'acme' } })

    fireEvent.click(screen.getByRole('button', { name: /^Add a condition/ }))
    pick('has_email')

    expect(tree()).toEqual({
      include_archived: false,
      where: {
        op: 'and',
        children: [
          { op: 'contains', field: 'current_company', value: 'acme' },
          { op: 'has_email' },
        ],
      },
    })
  })

  it('switches the op when the new field cannot take the old one', () => {
    mockApi(countRoute())
    renderWithClient(<Harness initial={emptyTree()} />)
    openPalette()
    pick('contains')

    fireEvent.change(screen.getByLabelText('Field'), { target: { value: 'degree' } })

    // `contains` is a text op; a numeric column takes the first op of its kind.
    expect(tree().where).toEqual({ op: 'eq', field: 'degree', value: 0 })
  })

  it('keeps archived contacts out unless asked', () => {
    mockApi(countRoute())
    renderWithClient(<Harness initial={emptyTree()} />)
    expect(tree().include_archived).toBe(false)
    fireEvent.click(screen.getByRole('checkbox', { name: /include archived/i }))
    expect(tree().include_archived).toBe(true)
  })

  it('names what is still missing instead of letting the server refuse it', () => {
    mockApi(countRoute())
    renderWithClient(<Harness initial={emptyTree()} />)
    openPalette()
    pick('tag_any')

    expect(screen.getByText(/needs at least one tag/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'founder' }))
    expect(tree().where).toEqual({ op: 'tag_any', names: ['founder'] })
  })
})

describe('an existing filter', () => {
  /** One node per predicate, so the round trip covers the whole language. */
  const everything: FilterTree = {
    include_archived: true,
    where: { op: 'or', children: PREDICATES.map((spec) => spec.example) },
  }

  it('renders every predicate it holds, placeholders included', () => {
    mockApi(countRoute())
    const { container } = renderWithClient(<Harness initial={everything} />)
    for (const spec of PREDICATES) {
      expect(container.querySelector(`[data-op="${spec.op}"]`), spec.op).not.toBeNull()
    }
  })

  it('round-trips unchanged when one unrelated node is removed', () => {
    mockApi(countRoute())
    renderWithClient(<Harness initial={everything} />)

    // Removing the "has a phone" node must leave every other node byte-identical.
    fireEvent.click(screen.getByRole('button', { name: 'Remove has a phone' }))

    const expected: FilterTree = {
      include_archived: true,
      where: {
        op: 'or',
        children: PREDICATES.filter((spec) => spec.op !== 'has_phone').map(
          (spec) => spec.example as FilterNode,
        ),
      },
    }
    expect(tree()).toEqual(expected)
  })

  it('explains a placeholder it was given rather than dropping it', () => {
    mockApi(countRoute())
    renderWithClient(
      <Harness
        initial={{ include_archived: false, where: { op: 'enrolled_in', campaign_id: 4 } }}
      />,
    )
    expect(screen.getByText(/campaign #4/)).toBeInTheDocument()
    // Once beside the node and once in the "not ready to run" summary.
    expect(screen.getAllByText(/P3-04/)).toHaveLength(2)
    expect(screen.getByRole('status')).toHaveTextContent(/finish the conditions/i)
  })
})

describe('choosing which list', () => {
  const LISTS = [
    { id: 1, name: 'Warm intros', kind: 'static', member_count: 12 },
    { id: 2, name: 'Validated', kind: 'smart', member_count: 40 },
  ]

  function listsRoute() {
    return { 'GET /api/v1/lists': () => jsonResponse(LISTS) }
  }

  it('starts on nobody and writes the list the picker chooses', async () => {
    // `list_id: 0` is what the palette creates and no list has that id, so a
    // row left alone selects nobody rather than quietly selecting the first
    // list somebody happens to have.
    mockApi({ ...countRoute(), ...listsRoute() })
    renderWithClient(<Harness initial={emptyTree()} />)
    openPalette()
    pick('list_member')
    expect(tree().where).toEqual({ op: 'list_member', list_id: 0 })

    const picker = await screen.findByRole('combobox', { name: 'List' })
    expect(picker).toHaveValue('0')
    // The options arrive with the lists, a request later than the picker.
    await screen.findByRole('option', { name: /Validated/ })
    fireEvent.change(picker, { target: { value: '2' } })

    expect(tree().where).toEqual({ op: 'list_member', list_id: 2 })
  })

  it('keeps a list that is no longer there, and says so', async () => {
    // Dropping the id would quietly change what a saved filter means; the
    // filter matches nobody either way, and this way the person can see why.
    mockApi({ ...countRoute(), ...listsRoute() })
    renderWithClient(
      <Harness initial={{ include_archived: false, where: { op: 'list_member', list_id: 4 } }} />,
    )

    const picker = await screen.findByRole('combobox', { name: 'List' })
    expect(picker).toHaveValue('4')
    expect(within(picker).getByRole('option', { name: /no longer there/ })).toBeInTheDocument()
    expect(screen.getByText(/no longer there\. It matches nobody/)).toBeInTheDocument()
    expect(tree().where).toEqual({ op: 'list_member', list_id: 4 })
  })

  it('says there are no lists yet rather than showing an empty picker', async () => {
    mockApi({ ...countRoute(), 'GET /api/v1/lists': () => jsonResponse([]) })
    renderWithClient(
      <Harness initial={{ include_archived: false, where: { op: 'list_member', list_id: 0 } }} />,
    )

    expect(await screen.findByText('No lists yet — make one first.')).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'List' })).not.toBeInTheDocument()
  })

  it('says the lists could not be read rather than showing an empty picker', async () => {
    mockApi({
      ...countRoute(),
      'GET /api/v1/lists': () => jsonResponse({ detail: 'the database is locked' }, 500),
    })
    renderWithClient(
      <Harness initial={{ include_archived: false, where: { op: 'list_member', list_id: 4 } }} />,
    )

    expect(
      await screen.findByText(/could not be read, so this row still names list #4/),
    ).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'List' })).not.toBeInTheDocument()
  })
})

describe('choosing a list inside a smart list’s own filter', () => {
  // List 5 is being edited. List 3 names 5, and list 4 names 3 (inside a `not`),
  // so choosing either would make list 5 include itself; 1 and 2 are fine.
  const where = (listId: number) => ({ op: 'list_member', list_id: listId })
  const LISTS = [
    { id: 1, name: 'Warm intros', kind: 'static', filter: null, member_count: 12 },
    { id: 2, name: 'Validated', kind: 'smart', filter: { where: null }, member_count: 40 },
    { id: 3, name: 'Names five', kind: 'smart', filter: { where: where(5) }, member_count: 1 },
    {
      id: 4,
      name: 'Names three',
      kind: 'smart',
      filter: { where: { op: 'and', children: [{ op: 'not', child: where(3) }] } },
      member_count: 1,
    },
    { id: 5, name: 'Being edited', kind: 'smart', filter: { where: null }, member_count: 0 },
  ]

  async function options() {
    await screen.findByRole('option', { name: /Warm intros/ })
    const picker = screen.getByRole('combobox', { name: 'List' })
    return within(picker)
      .getAllByRole('option')
      .map((option) => option.textContent)
  }

  it('leaves out the list itself and every list that already leads back to it', async () => {
    mockApi({ ...countRoute(), 'GET /api/v1/lists': () => jsonResponse(LISTS) })
    renderWithClient(
      <Harness
        initial={{ include_archived: false, where: where(0) } as FilterTree}
        editingListId={5}
      />,
    )
    expect(await options()).toEqual(['Choose a list…', 'Warm intros (static)', 'Validated (smart)'])
  })

  it('offers every list when the filter is not a list’s own', async () => {
    mockApi({ ...countRoute(), 'GET /api/v1/lists': () => jsonResponse(LISTS) })
    renderWithClient(
      <Harness initial={{ include_archived: false, where: where(0) } as FilterTree} />,
    )
    expect(await options()).toHaveLength(LISTS.length + 1)
  })

  it('keeps a row that already names a left-out list, so the filter is shown as saved', async () => {
    mockApi({ ...countRoute(), 'GET /api/v1/lists': () => jsonResponse(LISTS) })
    renderWithClient(
      <Harness
        initial={{ include_archived: false, where: where(3) } as FilterTree}
        editingListId={5}
      />,
    )
    expect(await options()).toContain('Names five (smart)')
    expect(screen.getByRole('combobox', { name: 'List' })).toHaveValue('3')
  })

  it('says why there is nothing to choose when the only list is this one', async () => {
    mockApi({ ...countRoute(), 'GET /api/v1/lists': () => jsonResponse([LISTS[4]]) })
    renderWithClient(
      <Harness
        initial={{ include_archived: false, where: where(0) } as FilterTree}
        editingListId={5}
      />,
    )
    expect(await screen.findByText(/a list cannot include itself/)).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'List' })).not.toBeInTheDocument()
  })
})

describe('the live count', () => {
  it('shows what the filter selects and follows an edit', async () => {
    let total = 12
    mockApi({
      'POST /api/v1/contacts/query': () =>
        jsonResponse({ items: [], total, describe: 'has email' }),
    })
    renderWithClient(<Harness initial={{ include_archived: false, where: { op: 'has_email' } }} />)

    expect(await screen.findByText('12')).toBeInTheDocument()

    total = 3
    fireEvent.click(screen.getByRole('checkbox', { name: /include archived/i }))
    expect(await screen.findByText('3')).toBeInTheDocument()
  })

  it('says why it cannot count an unfinished filter', () => {
    mockApi(countRoute())
    renderWithClient(
      <Harness initial={{ include_archived: false, where: { op: 'tag_any', names: [] } }} />,
    )
    expect(screen.getByRole('status')).toHaveTextContent(/finish the conditions/i)
  })

  it('shows the server’s refusal when the filter will not compile', async () => {
    mockApi({
      'POST /api/v1/contacts/query': () =>
        jsonResponse({ detail: 'where: list_member is not available yet; P1-08 delivers it' }, 422),
    })
    renderWithClient(<Harness initial={{ include_archived: false, where: { op: 'has_email' } }} />)
    expect(await screen.findByText(/not available yet/)).toBeInTheDocument()
  })
})
