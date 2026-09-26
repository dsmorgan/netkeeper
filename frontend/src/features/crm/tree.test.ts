import { describe, expect, it } from 'vitest'

import { listsLeadingTo, listsNamedIn } from './tree'
import type { FilterNode, FilterTree } from './types'

const member = (listId: number): FilterNode => ({ op: 'list_member', list_id: listId })

function smart(id: number, where: FilterNode | null): { id: number; filter: FilterTree } {
  return { id, filter: { include_archived: false, where } }
}

describe('listsNamedIn', () => {
  it('finds list_member predicates under and, or, and not', () => {
    const node: FilterNode = {
      op: 'or',
      children: [
        member(1),
        { op: 'and', children: [{ op: 'has_email' }, { op: 'not', child: member(2) }] },
      ],
    }
    expect(listsNamedIn(node)).toEqual([1, 2])
  })
})

describe('listsLeadingTo', () => {
  it('is the target and everything that reaches it, and nothing that only it reaches', () => {
    // 3 -> 2 -> 1, 4 -> 3; 1 -> 5 is the target naming someone, which is fine.
    const lists = [
      smart(1, member(5)),
      smart(2, member(1)),
      smart(3, member(2)),
      smart(4, { op: 'and', children: [member(3), { op: 'has_email' }] }),
      smart(5, null),
      { id: 6, filter: null },
    ]
    expect([...listsLeadingTo(1, lists)].sort()).toEqual([1, 2, 3, 4])
    expect([...listsLeadingTo(5, lists)].sort()).toEqual([1, 2, 3, 4, 5])
    expect([...listsLeadingTo(6, lists)]).toEqual([6])
  })

  it('stops on a cycle already in the data', () => {
    const lists = [smart(1, member(2)), smart(2, member(1))]
    expect([...listsLeadingTo(1, lists)].sort()).toEqual([1, 2])
  })
})
