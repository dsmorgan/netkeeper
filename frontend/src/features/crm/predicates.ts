/**
 * Every predicate the filter language defines, as the builder offers them.
 *
 * The filter language lives in `netkeeper/crm/filters.py`. This table is the
 * builder's view of it: one entry per `op`, with the words to show, a fresh
 * node to insert, and a complete example the tests validate against the
 * published JSON Schema.
 *
 * Three predicates parse but do not compile: the backend raises
 * `UnsupportedPredicate` for them. They are listed here with `unavailable` set
 * and rendered visibly out of reach rather than left out, because a builder
 * that silently omits part of the language is a builder nobody can trust.
 *
 * Two tests keep this honest and neither can be satisfied by editing only one
 * side: `predicates.test.ts` compares the ops here with the discriminator
 * mapping in `openapi.json`, and `tests/test_filter_builder.py` compares them
 * with `filters.OPS` and `filters.PLACEHOLDERS`, so a predicate added to — or
 * graduated inside — the language fails the build until the builder catches up.
 */
import type { FilterNode, FilterOp } from './types'

/** Where a predicate sits in the picker. */
export type PredicateGroup = 'logic' | 'field' | 'presence' | 'time' | 'tags'

export interface PredicateSpec {
  readonly op: FilterOp
  /** The words in the picker and on the node's own row. */
  readonly label: string
  readonly group: PredicateGroup
  /** One line under the label, saying what the predicate means. */
  readonly hint: string
  /**
   * Why the backend will not compile this predicate yet. Set on exactly the
   * ops in `filters.PLACEHOLDERS`; absent means the predicate works.
   */
  readonly unavailable?: string
  /** A fresh node for the tree, which may still need a value typed into it. */
  readonly create: () => FilterNode
  /** A filled-in node: valid against the schema, used by the tests and the docs. */
  readonly example: FilterNode
}

export const GROUP_LABELS: Record<PredicateGroup, string> = {
  logic: 'Groups',
  field: 'Field comparisons',
  presence: 'What a contact has',
  time: 'Time',
  tags: 'Tags',
}

/** The heading the picker puts placeholders under, kept separate from the groups. */
export const UNAVAILABLE_GROUP_LABEL = 'Not available yet'

export const PREDICATES: readonly PredicateSpec[] = [
  {
    op: 'and',
    label: 'All of',
    group: 'logic',
    hint: 'Every condition inside must hold.',
    create: () => ({ op: 'and', children: [] }),
    example: { op: 'and', children: [{ op: 'has_email' }] },
  },
  {
    op: 'or',
    label: 'Any of',
    group: 'logic',
    hint: 'At least one condition inside must hold.',
    create: () => ({ op: 'or', children: [] }),
    example: { op: 'or', children: [{ op: 'has_email' }] },
  },
  {
    op: 'not',
    label: 'Not',
    group: 'logic',
    hint: 'The exact opposite of what is inside; an empty column never makes it unknown.',
    create: () => ({ op: 'not', child: { op: 'and', children: [] } }),
    example: { op: 'not', child: { op: 'has_email' } },
  },
  {
    op: 'eq',
    label: 'is',
    group: 'field',
    hint: 'Equal to a value. Text compares without regard to case.',
    create: () => ({ op: 'eq', field: 'current_title', value: '' }),
    example: { op: 'eq', field: 'met', value: 'met' },
  },
  {
    op: 'neq',
    label: 'is not',
    group: 'field',
    hint: 'The exact complement of "is", empty columns included.',
    create: () => ({ op: 'neq', field: 'current_title', value: '' }),
    example: { op: 'neq', field: 'met', value: 'skip' },
  },
  {
    op: 'contains',
    label: 'contains',
    group: 'field',
    hint: 'Substring, without regard to case. % and _ match themselves.',
    create: () => ({ op: 'contains', field: 'current_title', value: '' }),
    example: { op: 'contains', field: 'current_title', value: 'engineer' },
  },
  {
    op: 'starts_with',
    label: 'starts with',
    group: 'field',
    hint: 'Prefix, without regard to case.',
    create: () => ({ op: 'starts_with', field: 'current_company', value: '' }),
    example: { op: 'starts_with', field: 'current_company', value: 'acme' },
  },
  {
    op: 'is_empty',
    label: 'is empty',
    group: 'field',
    hint: 'Not set, or the empty string for a text column.',
    create: () => ({ op: 'is_empty', field: 'headline' }),
    example: { op: 'is_empty', field: 'headline' },
  },
  {
    op: 'gt',
    label: 'is after / more than',
    group: 'field',
    hint: 'Strictly greater than, on a number or a date.',
    create: () => ({ op: 'gt', field: 'connected_on', value: '' }),
    example: { op: 'gt', field: 'degree', value: 1 },
  },
  {
    op: 'gte',
    label: 'is since / at least',
    group: 'field',
    hint: 'Greater than or equal to.',
    create: () => ({ op: 'gte', field: 'connected_on', value: '' }),
    example: { op: 'gte', field: 'connected_on', value: '2026-01-01' },
  },
  {
    op: 'lt',
    label: 'is before / less than',
    group: 'field',
    hint: 'Strictly less than.',
    create: () => ({ op: 'lt', field: 'connected_on', value: '' }),
    example: { op: 'lt', field: 'degree', value: 3 },
  },
  {
    op: 'lte',
    label: 'is up to / at most',
    group: 'field',
    hint: 'Less than or equal to.',
    create: () => ({ op: 'lte', field: 'connected_on', value: '' }),
    example: { op: 'lte', field: 'connected_on', value: '2026-12-31' },
  },
  {
    op: 'between',
    label: 'is between',
    group: 'field',
    hint: 'Both ends included. The high end may not sit below the low one.',
    create: () => ({ op: 'between', field: 'connected_on', low: '', high: '' }),
    example: { op: 'between', field: 'degree', low: 1, high: 2 },
  },
  {
    op: 'has_email',
    label: 'has an email',
    group: 'presence',
    hint: 'At least one email row, optionally of one status.',
    create: () => ({ op: 'has_email' }),
    example: { op: 'has_email', status: 'ok' },
  },
  {
    op: 'has_phone',
    label: 'has a phone',
    group: 'presence',
    hint: 'At least one phone row.',
    create: () => ({ op: 'has_phone' }),
    example: { op: 'has_phone' },
  },
  {
    op: 'has_li_url',
    label: 'has a LinkedIn URL',
    group: 'presence',
    hint: 'The profile URL is set and not empty.',
    create: () => ({ op: 'has_li_url' }),
    example: { op: 'has_li_url' },
  },
  {
    op: 'has_position',
    label: 'has a position',
    group: 'presence',
    hint: 'At least one row in the position history.',
    create: () => ({ op: 'has_position' }),
    example: { op: 'has_position' },
  },
  {
    op: 'email_contains',
    label: 'an email contains',
    group: 'presence',
    hint: 'Any of the contact’s emails contains the text, without regard to case.',
    create: () => ({ op: 'email_contains', value: '' }),
    example: { op: 'email_contains', value: '@example.com' },
  },
  {
    op: 'last_contacted',
    label: 'last contacted',
    group: 'time',
    hint: 'Within a window, longer ago than one, or never. Never-contacted people are not "long ago".',
    create: () => ({ op: 'last_contacted', within_days: 90, never: false }),
    example: { op: 'last_contacted', within_days: 90, never: false },
  },
  {
    op: 'connected_within_days',
    label: 'connected within',
    group: 'time',
    hint: 'Counted back from today in your timezone.',
    create: () => ({ op: 'connected_within_days', days: 365 }),
    example: { op: 'connected_within_days', days: 365 },
  },
  {
    op: 'changed_jobs_within_days',
    label: 'changed jobs within',
    group: 'time',
    hint: 'A job-change snapshot observed in the window.',
    create: () => ({ op: 'changed_jobs_within_days', days: 90 }),
    example: { op: 'changed_jobs_within_days', days: 90 },
  },
  {
    op: 'tag_any',
    label: 'has any of the tags',
    group: 'tags',
    hint: 'Matched without regard to case, whatever put the tag there.',
    create: () => ({ op: 'tag_any', names: [] }),
    example: { op: 'tag_any', names: ['founder'] },
  },
  {
    op: 'tag_all',
    label: 'has all of the tags',
    group: 'tags',
    hint: 'Carries every tag named.',
    create: () => ({ op: 'tag_all', names: [] }),
    example: { op: 'tag_all', names: ['founder', 'investor'] },
  },
  {
    op: 'tag_none',
    label: 'has none of the tags',
    group: 'tags',
    hint: 'Carries none of the tags named.',
    create: () => ({ op: 'tag_none', names: [] }),
    example: { op: 'tag_none', names: ['recruiter'] },
  },
  {
    op: 'list_member',
    label: 'is in the list',
    group: 'logic',
    hint: 'Members of a static or smart list.',
    unavailable:
      'The server compiles this one now — issue #73 landed with P1-27, so a saved filter that uses it works. What is missing is here: the builder has no list picker yet, so there is no way to choose which list from this screen.',
    create: () => ({ op: 'list_member', list_id: 0 }),
    example: { op: 'list_member', list_id: 1 },
  },
  {
    op: 'enrolled_in',
    label: 'is enrolled in the campaign',
    group: 'logic',
    hint: 'Anyone a campaign has enrolled.',
    unavailable:
      'Campaigns arrive with P3-04. The predicate parses so the language is complete, but the server refuses to compile it.',
    create: () => ({ op: 'enrolled_in', campaign_id: 0 }),
    example: { op: 'enrolled_in', campaign_id: 1 },
  },
  {
    op: 'replied_in',
    label: 'replied in the campaign',
    group: 'logic',
    hint: 'Anyone who answered a campaign.',
    unavailable:
      'Campaigns arrive with P3-04. The predicate parses so the language is complete, but the server refuses to compile it.',
    create: () => ({ op: 'replied_in', campaign_id: 0 }),
    example: { op: 'replied_in', campaign_id: 1 },
  },
]

const BY_OP = new Map<string, PredicateSpec>(PREDICATES.map((spec) => [spec.op, spec]))

/** The catalog entry for `op`, or undefined for an op this build does not know. */
export function predicate(op: string): PredicateSpec | undefined {
  return BY_OP.get(op)
}

/** The entry for `op`, or a throw: callers hold an op that came from a typed node. */
export function predicateOrThrow(op: FilterOp): PredicateSpec {
  const spec = BY_OP.get(op)
  if (spec === undefined) {
    throw new Error(`${op} is missing from the predicate catalog`)
  }
  return spec
}

/** Every op, in catalog order. */
export const ALL_OPS: readonly FilterOp[] = PREDICATES.map((spec) => spec.op)

/** The ops the backend parses but refuses to compile. */
export const UNAVAILABLE_OPS: readonly FilterOp[] = PREDICATES.filter(
  (spec) => spec.unavailable !== undefined,
).map((spec) => spec.op)

/** Whether `node` is a group with a list of children. */
export function isBranch(node: FilterNode): node is Extract<FilterNode, { children: unknown }> {
  return node.op === 'and' || node.op === 'or'
}

/** Whether `node` wraps exactly one child. */
export function isNegation(node: FilterNode): node is Extract<FilterNode, { op: 'not' }> {
  return node.op === 'not'
}
