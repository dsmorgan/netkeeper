import { describe, expect, it } from 'vitest'

import type { LintIssue } from './api'
import { showIssue } from './lint'

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
