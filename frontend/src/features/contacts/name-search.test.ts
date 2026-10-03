import { describe, expect, it } from 'vitest'

import { nameSearchFilter } from './name-search'

const FIELDS = ['first_name', 'last_name', 'preferred_name', 'current_company']
const orOver = (word: string) => ({
  op: 'or',
  children: FIELDS.map((field) => ({ op: 'contains', field, value: word })),
})

describe('nameSearchFilter', () => {
  it('returns null for an empty or blank search', () => {
    expect(nameSearchFilter('', FIELDS)).toBeNull()
    expect(nameSearchFilter('   ', FIELDS)).toBeNull()
  })

  it('gives one word a bare or over every field', () => {
    expect(nameSearchFilter('doe', FIELDS)).toEqual(orOver('doe'))
  })

  it('ANDs one or per word', () => {
    expect(nameSearchFilter(' doe  acme ', FIELDS)).toEqual({
      op: 'and',
      children: [orOver('doe'), orOver('acme')],
    })
  })

  it('defaults to the three name fields', () => {
    expect(nameSearchFilter('a')).toEqual({
      op: 'or',
      children: ['first_name', 'last_name', 'preferred_name'].map((field) => ({
        op: 'contains',
        field,
        value: 'a',
      })),
    })
  })
})
