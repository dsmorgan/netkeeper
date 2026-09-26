import { describe, expect, it } from 'vitest'

import { safeHref } from './safe-href'

describe('safeHref', () => {
  it('keeps an absolute http or https url', () => {
    expect(safeHref('https://priya-fake.example.test/')).toBe('https://priya-fake.example.test/')
    expect(safeHref('http://blog.example.test/a?b=1')).toBe('http://blog.example.test/a?b=1')
    expect(safeHref('  HTTPS://Example.test/x')).toBe('https://example.test/x')
  })

  it.each([
    'javascript:alert(1)',
    'JaVaScRiPt:alert(1)',
    '  javascript:alert(1)',
    'java\tscript:alert(1)',
    'java\nscript:alert(1)',
    '\u0001javascript:alert(1)',
    'data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==',
    'vbscript:msgbox(1)',
    'VBScript:msgbox(1)',
    'mailto:priya.fake@example.test',
    '//evil.example.test/x',
    'javascript%3Aalert(1)',
    '&#106;avascript:alert(1)',
    'example.test',
    'https://',
    '',
  ])('refuses %j', (url) => {
    expect(safeHref(url)).toBeNull()
  })
})
