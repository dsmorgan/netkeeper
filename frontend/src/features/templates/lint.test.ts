import { describe, expect, it } from 'vitest'

import type { LintIssue } from './api'
import { WHY_IT_MATTERS, issuesFor, severityByLine, showIssue } from './lint'

function issue(overrides: Partial<LintIssue>): LintIssue {
  return {
    rule: 'unsupported',
    severity: 'error',
    part: 'body',
    message: '',
    field: null,
    ...overrides,
  }
}

describe('showIssue', () => {
  it('leads a refused loop with a plain sentence and keeps the server wording', () => {
    const shown = showIssue(
      issue({ message: 'line 3: `for` is not available in a message template', field: 'for' }),
    )
    expect(shown).toMatchObject({
      line: 3,
      headline: "Loops aren't supported in templates",
      detail: '`for` is not available in a message template',
    })
  })

  it('keeps the server sentence for a refusal it has no plain wording for', () => {
    const shown = showIssue(
      issue({
        message: 'line 1: the `tojson` filter is not available in a message template',
        field: 'tojson',
      }),
    )
    expect(shown).toMatchObject({
      line: 1,
      headline: 'The `tojson` filter is not available in a message template',
      detail: null,
    })
  })

  it('reads a syntax error line, and has no line when lint gave none', () => {
    expect(showIssue(issue({ rule: 'syntax', message: "line 4: unexpected '}'" }))).toMatchObject({
      line: 4,
      headline: "Unexpected '}'",
    })
    expect(
      showIssue(
        issue({ rule: 'missing_subject', part: 'subject', message: 'an email needs a subject' }),
      ),
    ).toMatchObject({ line: null, headline: 'An email needs a subject', detail: null })
  })

  it('does not treat a field named like a refusal as one unless the rule is unsupported', () => {
    const shown = showIssue(
      issue({ rule: 'undefined_variable', message: '`for` is not a merge field', field: 'for' }),
    )
    expect(shown.headline).toBe('`for` is not a merge field')
  })
})

describe('lines and reasons (#344)', () => {
  it('takes the line lint reports over one in the message', () => {
    expect(showIssue(issue({ message: '`x` is not a merge field', line: 5 })).line).toBe(5)
    expect(showIssue(issue({ message: 'line 2: oops', line: 7 })).line).toBe(7)
  })

  it('gives every rule a one-line reason', () => {
    for (const [rule, why] of Object.entries(WHY_IT_MATTERS)) {
      expect(why, rule).toMatch(/^[A-Z].*\.$/)
      expect(why, rule).not.toContain('\n')
    }
    expect(showIssue(issue({ rule: 'no_contact_field' })).why).toContain('spam signal')
  })

  it('orders a part’s issues by line, the whole-part ones last', () => {
    const issues = [
      issue({ rule: 'no_contact_field', message: 'a' }),
      issue({ message: 'b', line: 4 }),
      issue({ message: 'c', part: 'subject', line: 1 }),
      issue({ message: 'd', line: 2 }),
      issue({ message: 'e', line: 2, severity: 'warning' }),
    ]
    expect(issuesFor(issues, 'body').map((shown) => shown.issue.message)).toEqual([
      'd',
      'e',
      'b',
      'a',
    ])
    expect(issuesFor(issues, 'subject').map((shown) => shown.issue.message)).toEqual(['c'])
  })

  it('marks a line with its worst finding', () => {
    const shown = issuesFor(
      [
        issue({ severity: 'warning', line: 2 }),
        issue({ severity: 'error', line: 2 }),
        issue({ severity: 'warning', line: 3 }),
        issue({ rule: 'no_contact_field' }),
      ],
      'body',
    )
    expect([...severityByLine(shown)]).toEqual([
      [2, 'error'],
      [3, 'warning'],
    ])
  })
})
