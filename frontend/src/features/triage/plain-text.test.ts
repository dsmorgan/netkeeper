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

  it('says nothing rather than printing an image-only body as markup (issue #92)', () => {
    // A complete void element parses fine — into an element with no text — so
    // the truncated-tag fallback used to catch it and print the tag verbatim.
    // An InMail whose body is one inline image is common enough to matter.
    expect(toPlainText('<img src=x onerror="alert(1)">')).toBeNull()
    expect(toPlainText('<p></p>')).toBeNull()
    expect(toPlainText('<img src=x><br>')).toBeNull()
  })

  it('still reads the text out of a body that has an image in it too', () => {
    expect(toPlainText('<img src=x>Lunch on Thursday?')).toBe('Lunch on Thursday?')
  })

  it('keeps the paragraphs of text a person typed, when asked to', () => {
    const notes = 'Met at the meetup.\n\nWants an intro to  the ops team.'
    expect(toPlainText(notes, { keepLineBreaks: true })).toBe(
      'Met at the meetup.\n\nWants an intro to the ops team.',
    )
    // Without the option it is one block, which is what a message body wants.
    expect(toPlainText(notes)).toBe('Met at the meetup. Wants an intro to the ops team.')
  })

  it('strips markup out of a note without losing its line breaks', () => {
    expect(
      toPlainText('<b>Intro</b> wanted\nfor <i>the ops team</i>', { keepLineBreaks: true }),
    ).toBe('Intro wanted\nfor the ops team')
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
