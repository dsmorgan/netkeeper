import { describe, expect, it } from 'vitest'

import {
  MAX_PAGE,
  MAX_PAGE_SIZE,
  buildFilter,
  filterBarCanShow,
  lastPage,
  pageOffset,
  validateContactsSearch,
} from './search'
import type { FilterTree } from './types'

describe('page bounds', () => {
  it('pins the highest page a URL may ask for', () => {
    expect(MAX_PAGE).toBe(1_000_000)
  })

  it('keeps the largest offset a URL can produce inside the API ceiling', () => {
    // `MAX_QUERY_OFFSET` in netkeeper/web/schemas.py, pinned there too.
    const largest = pageOffset({ page: MAX_PAGE, size: MAX_PAGE_SIZE })
    expect(Number.isSafeInteger(largest)).toBe(true)
    expect(largest).toBeLessThanOrEqual(1_000_000_000)
  })

  it('clamps a page past MAX_SAFE_INTEGER instead of sending an inexact offset', () => {
    const search = validateContactsSearch({ page: '9007199254740991', size: '200' })
    expect(search.page).toBe(MAX_PAGE)
    expect(Number.isSafeInteger(pageOffset(search))).toBe(true)
  })

  it.each([
    [0, 50, 1],
    [1, 50, 1],
    [50, 50, 1],
    [51, 50, 2],
    [120, 50, 3],
  ])('%i rows at %i a page end on page %i', (total, size, expected) => {
    expect(lastPage(total, size)).toBe(expected)
  })
})

describe('filterBarCanShow', () => {
  it('holds for everything the bar itself writes', () => {
    const tree = buildFilter({
      q: 'ferry',
      company: 'Harbor',
      met: 'met',
      dnc: true,
      tags: ['sailing', 'boats'],
      archived: true,
    })
    expect(filterBarCanShow(tree)).toBe(true)
    expect(filterBarCanShow(buildFilter({}))).toBe(true)
  })

  it('holds for no filter at all, and for a missing `where`', () => {
    expect(filterBarCanShow(null)).toBe(true)
    expect(filterBarCanShow({ include_archived: false })).toBe(true)
  })

  it('ignores the order of an `and`, and of the keys in a node', () => {
    const tree = buildFilter({ company: 'Harbor', met: 'met' })
    const where = tree.where as { op: 'and'; children: unknown[] }
    const reordered = {
      where: {
        op: 'and',
        children: [{ value: 'met', field: 'met', op: 'eq' }, ...where.children.slice(0, 1)],
      },
      include_archived: false,
    } as FilterTree
    expect(filterBarCanShow(reordered)).toBe(true)
  })

  it('fails for a predicate the bar has no control for', () => {
    const tree: FilterTree = {
      include_archived: false,
      where: { op: 'lt', field: 'connected_on', value: '2020-01-01' },
    }
    expect(filterBarCanShow(tree)).toBe(false)
  })

  it('fails when a readable predicate sits beside one it cannot read', () => {
    const tree: FilterTree = {
      include_archived: false,
      where: {
        op: 'and',
        children: [
          { op: 'eq', field: 'met', value: 'met' },
          { op: 'lt', field: 'connected_on', value: '2020-01-01' },
        ],
      },
    }
    expect(filterBarCanShow(tree)).toBe(false)
  })

  it('fails for a text search over fewer fields than the bar searches', () => {
    const tree: FilterTree = {
      include_archived: false,
      where: {
        op: 'or',
        children: [
          { op: 'contains', field: 'first_name', value: 'ada' },
          { op: 'contains', field: 'last_name', value: 'ada' },
        ],
      },
    }
    expect(filterBarCanShow(tree)).toBe(false)
  })
})
