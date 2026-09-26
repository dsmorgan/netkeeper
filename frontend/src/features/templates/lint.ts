/**
 * Lint findings as the editor shows them: a line number where lint gave one,
 * and a plain sentence for each construct the template allowlist refuses.
 *
 * The backend's refusal reads "line 3: `for` is not available in a message
 * template", which is exact but assumes you know what a `for` node is. The
 * editor leads with what it means ("Loops aren't supported in templates") and
 * keeps the server's wording underneath, so nothing it said is lost.
 */
import type { LintIssue } from './api'

export interface ShownIssue {
  issue: LintIssue
  /** One-based, when lint named a line. */
  line: number | null
  /** The sentence to lead with. */
  headline: string
  /** The server's own wording, minus the line prefix, when it differs from the headline. */
  detail: string | null
}

const LINE_PREFIX = /^line (\d+): /

/**
 * What each refused construct is, keyed by the name lint puts in `field` for
 * an `unsupported` issue (`_NODE_NAMES` in `netkeeper/campaigns/render.py`).
 * A name not here, like a filter off the allowlist, keeps the server's sentence,
 * which already says "the `tojson` filter is not available".
 */
const REFUSED: Readonly<Record<string, string>> = {
  for: "Loops aren't supported in templates",
  with: "`with` blocks aren't supported in templates",
  set: "Setting variables isn't supported in templates",
  macro: "Macros aren't supported in templates",
  call: "Calling functions isn't supported in templates",
  filter: "`filter` blocks aren't supported in templates",
  autoescape: "`autoescape` blocks aren't supported in templates",
  block: "Template blocks and inheritance aren't supported in templates",
  extends: "Template blocks and inheritance aren't supported in templates",
  include: "Including other templates isn't supported",
  import: "Importing other templates isn't supported",
  list: "Lists aren't supported in templates",
  tuple: "Lists aren't supported in templates",
  dict: "Dictionaries aren't supported in templates",
  subscript: "Indexing with [ ] isn't supported in templates",
  slice: "Slicing with [ : ] isn't supported in templates",
  '-': 'Only + and * on whole numbers are supported; - is not',
  '/': 'Only + and * on whole numbers are supported; / is not',
  '//': 'Only + and * on whole numbers are supported; // is not',
  '%': 'Only + and * on whole numbers are supported; % is not',
  '**': 'Only + and * on whole numbers are supported; ** is not',
  keyword: 'Keyword arguments are only supported inside a filter',
  self: "`self` isn't available in templates",
  nesting: 'This template nests too deeply',
}

export function showIssue(issue: LintIssue): ShownIssue {
  const match = LINE_PREFIX.exec(issue.message)
  const line = match === null ? null : Number(match[1])
  const text = match === null ? issue.message : issue.message.slice(match[0].length)
  const plain =
    issue.rule === 'unsupported' && issue.field != null ? REFUSED[issue.field] : undefined
  if (plain === undefined) {
    return { issue, line, headline: capitalize(text), detail: null }
  }
  return { issue, line, headline: plain, detail: text }
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1)
}

export function hasErrors(issues: readonly LintIssue[]): boolean {
  return issues.some((issue) => issue.severity === 'error')
}
