import { describe, expect, it } from 'vitest'

import { detailMessage } from './errors'

describe('detailMessage', () => {
  it("reads a route's own refusal", () => {
    expect(detailMessage({ detail: 'no import run 999999 for this user' })).toBe(
      'no import run 999999 for this user',
    )
  })

  it('reads every message in a validation-error list, in order', () => {
    const body = {
      detail: [
        {
          type: 'too_long',
          loc: ['body', 'selection', 'ids'],
          msg: 'List should have at most 1000 items after validation, not 1200',
        },
        { type: 'missing', loc: ['body', 'action'], msg: 'Field required' },
      ],
    }
    expect(detailMessage(body)).toBe(
      'List should have at most 1000 items after validation, not 1200; Field required',
    )
  })

  it("drops pydantic's 'Value error, ' prefix", () => {
    const body = {
      detail: [
        {
          type: 'value_error',
          loc: ['body', 'pattern'],
          msg: 'Value error, pattern may run slowly: an unbounded repeat inside another one',
        },
      ],
    }
    expect(detailMessage(body)).toBe(
      'pattern may run slowly: an unbounded repeat inside another one',
    )
  })

  it.each([undefined, null, 'oops', {}, { detail: '' }, { detail: [] }, { detail: [{}] }])(
    'returns null when %j carries no message',
    (body) => {
      expect(detailMessage(body)).toBeNull()
    },
  )
})
