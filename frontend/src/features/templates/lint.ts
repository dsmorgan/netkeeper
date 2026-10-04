/**
 * Lint findings as the editor shows them: a line number where lint gave one,
 * a plain sentence for each construct the template allowlist refuses, and a
 * one-line reason each rule matters (#344).
 *
 * The backend's refusal reads "line 3: `for` is not available in a message
 * template", which is exact but assumes you know what a `for` node is. The
 * editor leads with what it means ("Loops aren't supported in templates") and
 * keeps the server's wording underneath, so nothing it said is lost.
 */
import type { LintIssue } from './api'

export type LintRule = LintIssue['rule']

export interface ShownIssue {
  issue: LintIssue
  /** One-based, when lint named a line. */
  line: number | null
  /** The sentence to lead with. */
  headline: string
  /** The server's own wording, minus the line prefix, when it differs from the headline. */
  detail: string | null
  /** Why the rule matters, in one plain sentence. */
  why: string
}

/**
 * Why each lint rule matters, in one plain sentence (#344). Typed against the
 * backend's rule list, so a new rule does not build until it has a reason here.
 */
export const WHY_IT_MATTERS: Readonly<Record<LintRule, string>> = {
  syntax: "The template can't be read, so it can't be sent to anyone until it's fixed.",
  unsupported:
    'Templates allow only merge fields, filters, and simple conditions, so a message can never run code.',
  unsafe_attribute:
    'Names starting with _ reach into the program behind the template, so they are refused for safety.',
  attribute_access:
    "Merge fields are plain values with nothing inside them, so this can't produce text.",
  undefined_variable:
    "This isn't a merge field (often a typo), so the message would go out with a blank where you expect a value.",
  no_contact_field:
    'Every contact would get the identical message, and identical bulk mail is a spam signal.',
  missing_subject: "An email can't go out without a subject line.",
  bad_link: "A link that doesn't parse won't open for the person you send it to.",
  missing_value:
    'This contact has no value for the field, so the message has a blank where it goes.',
  removed_field:
    'The me.* fields were removed, so this renders as a blank. Write your own details into the template; a test send shows the contact fields with yours, from Settings.',
}

/**
 * Each lint rule as an instruction to whoever writes the text, for the prompt
 * the "Draft with your AI assistant" helper builds (#368). Keyed by rule id next
 * to {@link WHY_IT_MATTERS}, so a new rule does not build until it has one too.
 * Null for a rule that is not about the text you write (`missing_value` is about
 * one contact's data in the preview).
 */
export const PROMPT_RULES: Readonly<Record<LintRule, string | null>> = {
  syntax: 'Write valid placeholders: close every {{ with }}, and every {% if %} with {% endif %}.',
  unsupported:
    'Use only placeholders, filters, and simple {% if %} conditions: no loops, {% set %}, macros, or other template code.',
  unsafe_attribute: 'Never write a name that starts with an underscore.',
  attribute_access:
    'Write each placeholder exactly as listed above, with nothing added after a dot.',
  undefined_variable: 'Use only the merge fields listed above, spelled exactly as shown.',
  no_contact_field:
    'Name at least one placeholder about the recipient, such as {{ first_name }}, in every message body.',
  missing_subject: 'Give every email a subject line.',
  bad_link: 'Write every link in full, starting with https://.',
  missing_value: null,
  removed_field:
    'Use no placeholder for a detail about me. Leave out my own name and signature: I add them myself.',
}

const LINE_PREFIX = /^line (\d+): /

/** Every line break Jinja counts lines by: CRLF, a bare CR, and LF. */
export const LINE_BREAK = /\r\n|\r|\n/

/** `text`'s lines, as lint numbers them (one-based: line 1 is the first). */
export function splitLines(text: string): string[] {
  return text.split(LINE_BREAK)
}

/** The offsets of one-based `line` in `text`: where it starts and where it ends. */
export function lineRange(text: string, line: number): [number, number] {
  const breaks = new RegExp(LINE_BREAK.source, 'g')
  let start = 0
  for (let at = 1; at < line; at++) {
    const found = breaks.exec(text)
    if (found === null) break
    start = found.index + found[0].length
  }
  const rest = new RegExp(LINE_BREAK.source).exec(text.slice(start))
  return [start, rest === null ? text.length : start + rest.index]
}

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
  // The line lint reports; a finding stored before lint reported lines may still carry
  // one in its message.
  const line = issue.line ?? (match === null ? null : Number(match[1]))
  const text = match === null ? issue.message : issue.message.slice(match[0].length)
  const why = WHY_IT_MATTERS[issue.rule]
  const plain =
    issue.rule === 'unsupported' && issue.field != null ? REFUSED[issue.field] : undefined
  if (plain === undefined) {
    return { issue, line, headline: capitalize(text), detail: null, why }
  }
  return { issue, line, headline: plain, detail: text, why }
}

/**
 * The issues about one part, in the order you read it: by line, the ones about the
 * whole part last. Stable, so issues on one line keep lint's order.
 */
export function issuesFor(issues: readonly LintIssue[], part: LintIssue['part']): ShownIssue[] {
  return issues
    .filter((issue) => issue.part === part)
    .map(showIssue)
    .map((shown, index) => ({ shown, index }))
    .sort(
      (a, b) =>
        (a.shown.line ?? Number.MAX_SAFE_INTEGER) - (b.shown.line ?? Number.MAX_SAFE_INTEGER) ||
        a.index - b.index,
    )
    .map(({ shown }) => shown)
}

/** The worst severity on each line, for marking the lines in the editor. */
export function severityByLine(shown: readonly ShownIssue[]): Map<number, LintIssue['severity']> {
  const lines = new Map<number, LintIssue['severity']>()
  for (const { issue, line } of shown) {
    if (line === null) continue
    if (lines.get(line) !== 'error') lines.set(line, issue.severity)
  }
  return lines
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1)
}

export function hasErrors(issues: readonly LintIssue[]): boolean {
  return issues.some((issue) => issue.severity === 'error')
}
