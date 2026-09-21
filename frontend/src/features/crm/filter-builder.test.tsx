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

function Harness({ initial }: { initial: FilterTree }) {
  const [value, setValue] = useState(initial)
  return (
    <>
      <FilterBuilder value={value} onChange={setValue} tags={TAGS} />
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

  it('makes the twenty-four usable predicates clickable', () => {
    mockApi(countRoute())
    const { container } = renderWithClient(<Harness initial={emptyTree()} />)
    openPalette()

    const usable = PREDICATES.filter((spec) => spec.unavailable === undefined)
    for (const spec of usable) {
      const element = container.querySelector(`[data-op="${spec.op}"]`)
      expect(element?.tagName, spec.op).toBe('BUTTON')
      expect(element?.getAttribute('aria-disabled')).toBeNull()
    }
    expect(usable).toHaveLength(LANGUAGE_OPS.length - 3)
  })

  it('shows the three the server refuses as unavailable, with the reason', () => {
    mockApi(countRoute())
    const { container } = renderWithClient(<Harness initial={emptyTree()} />)
    openPalette()

    for (const op of ['list_member', 'enrolled_in', 'replied_in']) {
      const element = container.querySelector(`[data-op="${op}"]`)
      expect(element, op).not.toBeNull()
      expect(element?.getAttribute('data-unavailable')).toBe('true')
      expect(element?.getAttribute('aria-disabled')).toBe('true')
      expect(element?.tagName).not.toBe('BUTTON')
    }
    expect(screen.getByText(/issue #73/)).toBeInTheDocument()
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
      <Harness initial={{ include_archived: false, where: { op: 'list_member', list_id: 4 } }} />,
    )
    expect(screen.getByText(/list #4/)).toBeInTheDocument()
    // Once beside the node and once in the "not ready to run" summary.
    expect(screen.getAllByText(/issue #73/)).toHaveLength(2)
    expect(screen.getByRole('status')).toHaveTextContent(/finish the conditions/i)
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
