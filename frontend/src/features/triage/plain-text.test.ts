import { describe, expect, it } from 'vitest'

import { toPlainText, truncate } from './plain-text'

describe('toPlainText', () => {
  it('leaves plain text alone', () => {
    expect(toPlainText('Good to meet you at the meetup.')).toBe('Good to meet you at the meetup.')
  })

  it('returns null for nothing worth showing', () => {
    expect(toPlainText(null)).toBeNull()
    expect(toPlainText('')).toBeNull()
    expect(toPlainText('   ')).toBeNull()
  })

  it('reads the text out of an InMail body', () => {
    const body = '<p>Hi there.</p><p>Are you free <b>Thursday</b>?</p>'
    expect(toPlainText(body)).toBe('Hi there.Are you free Thursday?')
  })

  it('survives a body the archive cut mid-tag (issue #75)', () => {
    const truncated = 'Great talking today. Read more <a href="https://example.com/a-very-lo'
    expect(toPlainText(truncated)).toBe('Great talking today. Read more')
  })

  it('resolves entities', () => {
    expect(toPlainText('Ops &amp; Strategy &lt;draft&gt;')).toBe('Ops & Strategy <draft>')
  })

  it('keeps no markup from a payload that tries to be one', () => {
    const payload = '<img src=x onerror=alert(1)>Hello<script>alert(2)</script>'
    const text = toPlainText(payload)
    expect(text).not.toContain('<')
    expect(text).not.toContain('onerror')
    expect(text).toContain('Hello')
  })

  it('falls back to the raw string when there is nothing but a broken tag', () => {
    expect(toPlainText('<div class="unclosed')).toBe('<div class="unclosed')
  })
})

describe('truncate', () => {
  it('leaves a short string alone', () => {
    expect(truncate('short', 20)).toBe('short')
  })

  it('cuts on a word boundary', () => {
    expect(truncate('one two three four five six', 12)).toBe('one two…')
  })
})
