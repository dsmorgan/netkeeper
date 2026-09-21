/**
 * Switching a comparison's field may never leave a value the server would refuse.
 *
 * A filter row keeps its value when you change the column, which is right when
 * the new column is of the same kind and wrong otherwise: text, an enum value,
 * a date and a datetime are all JavaScript strings, so "same type" is not the
 * same question as "same kind". Carrying a company name into the `met` enum
 * produced a tree the compiler refuses while the builder reported no problem,
 * and the row lied about it too — a `<select>` whose value is not one of its
 * options displays the first one.
 *
 * This walks every ordered pair of filterable columns, which is every kind
 * transition there is, and after each switch asserts three things: the tree
 * validates, the field picker shows the field the tree holds, and an enum's
 * value picker shows the value the tree holds.
 */
import { useState } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'

import { jsonResponse } from '@/test/fetch'

import { FilterBuilder } from './filter-builder'
import { FIELDS } from './fields'
import type { FieldSpec } from './fields'
import { mockApi } from './harness'
import { validateTree } from './tree'
import type { FilterTree } from './types'

/** A value the API would accept for a column of this kind. */
function validValue(spec: FieldSpec): string | number | boolean {
  switch (spec.kind) {
    case 'string':
      return 'northwind'
    case 'enum':
      return spec.values?.[0] ?? ''
    case 'int':
      return 2
    case 'bool':
      return true
    case 'date':
      return '2026-03-04'
    case 'datetime':
      return '2026-03-04T12:00:00.000Z'
  }
}

function Harness({ initial }: { initial: FilterTree }) {
  const [value, setValue] = useState(initial)
  // The count is off: this test is about the tree, and a query per switch
  // would be four hundred requests to say nothing.
  return (
    <>
      <FilterBuilder value={value} onChange={setValue} tags={[]} showCount={false} />
      <pre data-testid="tree">{JSON.stringify(value)}</pre>
    </>
  )
}

function tree(): FilterTree {
  return JSON.parse(screen.getByTestId('tree').textContent ?? '{}') as FilterTree
}

function renderHarness(initial: FilterTree) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <Harness initial={initial} />
    </QueryClientProvider>,
  )
}

describe('changing the column of a comparison', () => {
  it('never leaves a value of the wrong kind behind', () => {
    mockApi({})
    const broken: string[] = []

    for (const from of FIELDS) {
      const { unmount } = renderHarness({
        include_archived: false,
        where: { op: 'eq', field: from.name, value: validValue(from) } as FilterTree['where'],
      })

      // Every switch starts from the same valid value, so the pair under test
      // is `from → to` and not whatever the previous iteration left behind.
      for (const to of FIELDS) {
        fireEvent.change(screen.getByLabelText('Field'), { target: { value: from.name } })
        fireEvent.change(screen.getByLabelText('Field'), { target: { value: to.name } })
        const where = tree().where

        // An empty box the person still has to fill is fine and expected. A
        // value the server would refuse is the bug this guards.
        const refused = validateTree(tree()).filter((issue) => issue.kind === 'invalid')
        if (refused.length > 0) {
          broken.push(`${from.name} → ${to.name}: ${refused.map((i) => i.message).join('; ')}`)
          continue
        }

        // The row must read back what the tree holds, not what a select fell
        // back to displaying.
        expect(screen.getByLabelText('Field')).toHaveValue(to.name)
        if (to.kind === 'enum' && where !== null && where !== undefined && 'value' in where) {
          expect(screen.getByLabelText('Value')).toHaveValue(String(where.value))
        }
      }
      unmount()
    }

    expect(broken).toEqual([])
  })

  it('keeps the value when the new column is of the same kind', () => {
    mockApi({})
    renderHarness({
      include_archived: false,
      where: { op: 'contains', field: 'current_title', value: 'engineer' },
    })
    fireEvent.change(screen.getByLabelText('Field'), { target: { value: 'current_company' } })
    expect(tree().where).toEqual({ op: 'contains', field: 'current_company', value: 'engineer' })
  })

  it('drops it when the new column is a different kind, even though both are strings', () => {
    mockApi({})
    renderHarness({
      include_archived: false,
      where: { op: 'eq', field: 'current_company', value: 'acme' },
    })
    fireEvent.change(screen.getByLabelText('Field'), { target: { value: 'met' } })

    expect(tree().where).toEqual({ op: 'eq', field: 'met', value: 'unknown' })
    expect(screen.getByLabelText('Value')).toHaveValue('unknown')
    expect(validateTree(tree())).toEqual([])
  })

  it('drops it between two enums, which share a kind but no values', () => {
    mockApi({})
    renderHarness({
      include_archived: false,
      where: { op: 'eq', field: 'met', value: 'skip' },
    })
    fireEvent.change(screen.getByLabelText('Field'), { target: { value: 'source' } })

    // `skip` is not one of source's values, so carrying it would have produced
    // a tree the server refuses and a row displaying `sync` over it.
    expect(tree().where).toEqual({ op: 'eq', field: 'source', value: 'sync' })
    expect(screen.getByLabelText('Value')).toHaveValue('sync')
    expect(validateTree(tree())).toEqual([])
  })
})

describe('a value the builder did not put there', () => {
  it('is reported rather than counted as ready', () => {
    mockApi({
      'POST /api/v1/contacts/query': () =>
        jsonResponse({ items: [], total: 1, describe: 'anything' }),
    })
    // A tree that could arrive from anywhere: an enum column holding text.
    renderHarness({
      include_archived: false,
      where: { op: 'eq', field: 'met', value: 'acme' } as FilterTree['where'],
    })
    expect(screen.getByText(/met takes one of: unknown, met, not_met, skip/)).toBeInTheDocument()
  })

  it('is reported for a number column holding text', () => {
    mockApi({})
    renderHarness({
      include_archived: false,
      where: { op: 'gt', field: 'degree', value: 'acme' } as FilterTree['where'],
    })
    expect(screen.getByText(/degree takes a whole number/)).toBeInTheDocument()
  })

  it('is reported for a date column holding a timestamp', () => {
    mockApi({})
    renderHarness({
      include_archived: false,
      where: {
        op: 'gte',
        field: 'connected_on',
        value: '2026-03-04T12:00:00Z',
      } as FilterTree['where'],
    })
    expect(screen.getByText(/connected on takes a date/)).toBeInTheDocument()
  })
})
