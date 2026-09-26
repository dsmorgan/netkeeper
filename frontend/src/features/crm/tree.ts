/**
 * Walking and editing a filter tree without mutating it.
 *
 * A path is the list of child indexes from the root: `[]` is the root
 * predicate, `[1, 0]` is the first child of the root's second child. A `not`
 * node has one child at index 0. Every edit returns a new tree, so React sees
 * a changed reference and a smart list's live membership is never read from a
 * stale one.
 */
import { fieldSpec } from './fields'
import { predicateOrThrow } from './predicates'
import type { FilterNode, FilterTree } from './types'

export type FilterPath = readonly number[]

function childrenOf(node: FilterNode): readonly FilterNode[] {
  if (node.op === 'and' || node.op === 'or') return node.children
  if (node.op === 'not') return [node.child]
  return []
}

function withChildren(node: FilterNode, children: readonly FilterNode[]): FilterNode {
  if (node.op === 'and' || node.op === 'or') return { ...node, children: [...children] }
  if (node.op === 'not') {
    const first = children[0]
    if (first === undefined) throw new Error('a "not" keeps exactly one child')
    return { ...node, child: first }
  }
  throw new Error(`${node.op} has no children`)
}

/** The node at `path`, or undefined when the path runs off the tree. */
export function nodeAt(root: FilterNode, path: FilterPath): FilterNode | undefined {
  let current: FilterNode | undefined = root
  for (const index of path) {
    if (current === undefined) return undefined
    current = childrenOf(current)[index]
  }
  return current
}

/** `root` with the node at `path` replaced by `replace(node)`. */
export function replaceAt(
  root: FilterNode,
  path: FilterPath,
  replace: (node: FilterNode) => FilterNode,
): FilterNode {
  if (path.length === 0) return replace(root)
  const [head, ...rest] = path
  const children = childrenOf(root)
  const target = head === undefined ? undefined : children[head]
  if (head === undefined || target === undefined) return root
  const next = children.map((child, index) =>
    index === head ? replaceAt(target, rest, replace) : child,
  )
  return withChildren(root, next)
}

/** `root` with the node at `path` removed, or undefined when the root itself goes. */
export function removeAt(root: FilterNode, path: FilterPath): FilterNode | undefined {
  if (path.length === 0) return undefined
  const parentPath = path.slice(0, -1)
  const index = path[path.length - 1]
  if (index === undefined) return root
  const parent = nodeAt(root, parentPath)
  if (parent === undefined) return root
  if (parent.op === 'not') {
    // A "not" cannot lose its only child; emptying it removes the "not" too.
    return removeAt(root, parentPath)
  }
  const kept = childrenOf(parent).filter((_, position) => position !== index)
  return replaceAt(root, parentPath, (node) => withChildren(node, kept))
}

/** `root` with `child` appended to the group at `path`. */
export function appendAt(root: FilterNode, path: FilterPath, child: FilterNode): FilterNode {
  return replaceAt(root, path, (node) => withChildren(node, [...childrenOf(node), child]))
}

/** A stable key for React, derived from the path. */
export function pathKey(path: FilterPath): string {
  return path.length === 0 ? 'root' : path.join('.')
}

/**
 * Which of two different things is wrong with a node.
 *
 * `incomplete` is a box still to be filled in — normal, expected, and the only
 * thing a freshly added condition should ever report. `invalid` is a value the
 * server would refuse, which the builder should never be able to produce and
 * which is worth telling apart in a test. Both stop a save; `unavailable` is
 * the third predicate kind, which no amount of typing fixes.
 */
export type FilterIssueKind = 'incomplete' | 'invalid' | 'unavailable'

export interface FilterIssue {
  readonly path: FilterPath
  readonly message: string
  readonly kind: FilterIssueKind
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/
const ISO_OFFSET = /([zZ]|[+-]\d{2}:?\d{2})$/

interface ValueProblem {
  message: string
  kind: FilterIssueKind
}

/**
 * Why `value` is not something `field` can be compared against, or null.
 *
 * This mirrors `filters.typed_value`, which is what the server runs, and it
 * judges the value that is in the tree rather than trusting whatever put it
 * there. That distinction matters: a value carried across a field change used
 * to land on a column of another kind — text in a number column, a company
 * name in the `met` enum, one enum's value in another enum — and an emptiness
 * check saw nothing wrong with it, so Save and Download stayed lit and the
 * server answered 422. An enum was the worst of it, because a `<select>` whose
 * value is not among its options displays the first one, so the row read back
 * as something the tree did not hold.
 */
function valueIssue(
  field: string,
  value: string | number | boolean,
  label: string,
): ValueProblem | null {
  const bad = (message: string): ValueProblem => ({ message, kind: 'invalid' })
  const empty = (message: string): ValueProblem => ({ message, kind: 'incomplete' })
  const spec = fieldSpec(field)
  if (spec === undefined) return bad(`${field} is not a column this build can filter on.`)
  switch (spec.kind) {
    case 'string':
      if (typeof value !== 'string') return bad(`${spec.label} takes text.`)
      return value === '' ? empty(`“${label}” needs a value.`) : null
    case 'enum':
      return typeof value === 'string' && (spec.values ?? []).includes(value)
        ? null
        : bad(`${spec.label} takes one of: ${(spec.values ?? []).join(', ')}.`)
    case 'int':
      return typeof value === 'number' && Number.isInteger(value)
        ? null
        : bad(`${spec.label} takes a whole number.`)
    case 'bool':
      return typeof value === 'boolean' ? null : bad(`${spec.label} takes yes or no.`)
    case 'date':
      if (value === '') return empty(`“${label}” needs a date.`)
      if (typeof value !== 'string' || !ISO_DATE.test(value)) {
        return bad(`${spec.label} takes a date.`)
      }
      return Number.isNaN(Date.parse(value)) ? bad(`${value} is not a real date.`) : null
    case 'datetime':
      if (value === '') return empty(`“${label}” needs a date and time.`)
      if (typeof value !== 'string' || !ISO_OFFSET.test(value) || Number.isNaN(Date.parse(value))) {
        return bad(`${spec.label} takes a date and time with a timezone.`)
      }
      return null
  }
}

/**
 * Whether `field` would accept `value` — the one predicate that decides both
 * what the builder reports and what survives a change of column.
 *
 * Deciding the carry with this rather than with a type or even a kind
 * comparison is what keeps the two answers from disagreeing: two enums are the
 * same kind and still do not share a value, and a date and a datetime are both
 * strings. If the value would not be accepted, it does not travel.
 */
export function valueFits(field: string, value: string | number | boolean): boolean {
  const problem = valueIssue(field, value, '')
  return problem === null || problem.kind === 'incomplete'
}

/**
 * What still has to be filled in, or put right, before the API would accept the tree.
 *
 * These are the constraints the Pydantic models carry — a non-empty group, a
 * value of at least one character, a value of the field's own kind, exactly one
 * `last_contacted` option, an ordered `between` — plus the three predicates the
 * compiler refuses. Showing them here means the save button can say why it is
 * off instead of the server answering 422 after the click, which for an export
 * arrives as a blank tab the browser has already navigated to.
 */
export function validate(node: FilterNode, path: FilterPath = []): FilterIssue[] {
  const issues: FilterIssue[] = []
  const spec = predicateOrThrow(node.op)
  if (spec.unavailable !== undefined) {
    issues.push({ path, message: `${spec.label}: ${spec.unavailable}`, kind: 'unavailable' })
  }
  switch (node.op) {
    case 'and':
    case 'or':
      if (node.children.length === 0) {
        issues.push({
          path,
          message: `“${spec.label}” needs at least one condition.`,
          kind: 'incomplete',
        })
      }
      node.children.forEach((child, index) => issues.push(...validate(child, [...path, index])))
      break
    case 'not':
      issues.push(...validate(node.child, [...path, 0]))
      break
    case 'contains':
    case 'starts_with':
    case 'email_contains':
      if (node.value.trim() === '') {
        issues.push({
          path,
          message: `“${spec.label}” needs something to look for.`,
          kind: 'incomplete',
        })
      }
      break
    case 'tag_any':
    case 'tag_all':
    case 'tag_none':
      if (node.names.length === 0) {
        issues.push({
          path,
          message: `“${spec.label}” needs at least one tag.`,
          kind: 'incomplete',
        })
      }
      break
    case 'eq':
    case 'neq':
    case 'gt':
    case 'gte':
    case 'lt':
    case 'lte': {
      const problem = valueIssue(node.field, node.value, spec.label)
      if (problem !== null) issues.push({ path, ...problem })
      break
    }
    case 'between': {
      const problem =
        valueIssue(node.field, node.low, spec.label) ??
        valueIssue(node.field, node.high, spec.label)
      if (problem !== null) {
        issues.push({ path, ...problem })
      } else if (comparable(node.low) > comparable(node.high)) {
        issues.push({
          path,
          message: 'The high end of “is between” is below the low one.',
          kind: 'invalid',
        })
      }
      break
    }
    case 'last_contacted': {
      const chosen =
        (node.within_days !== null && node.within_days !== undefined ? 1 : 0) +
        (node.older_than_days !== null && node.older_than_days !== undefined ? 1 : 0) +
        (node.never ? 1 : 0)
      if (chosen !== 1) {
        issues.push({
          path,
          message: 'Choose exactly one “last contacted” option.',
          kind: 'invalid',
        })
      }
      break
    }
    default:
      break
  }
  return issues
}

function comparable(value: string | number | boolean): string | number {
  return typeof value === 'boolean' ? Number(value) : value
}

/** The whole tree's issues, root option included. */
export function validateTree(tree: FilterTree): FilterIssue[] {
  return tree.where === null || tree.where === undefined ? [] : validate(tree.where)
}

/** An empty filter: every live contact. */
export function emptyTree(): FilterTree {
  return { where: null, include_archived: false }
}

/** The ids every `list_member` predicate under `node` names. */
export function listsNamedIn(node: FilterNode): number[] {
  if (node.op === 'list_member') return [node.list_id]
  return childrenOf(node).flatMap(listsNamedIn)
}

/**
 * The lists a filter for list `target` may not name, because each one already
 * leads back to `target`: `target` itself, every list whose saved filter names
 * it, every list whose filter names one of those, and so on. Choosing any of
 * them would close a cycle, which the server refuses at save (P1-27).
 *
 * Read from the lists' saved filters, so it is as current as `GET /lists`; the
 * server's check stays the authority.
 */
export function listsLeadingTo(
  target: number,
  lists: readonly { id: number; filter: FilterTree | null }[],
): Set<number> {
  const namedBy = new Map<number, number[]>()
  for (const list of lists) {
    const where = list.filter?.where
    if (where === null || where === undefined) continue
    for (const named of listsNamedIn(where)) {
      namedBy.set(named, [...(namedBy.get(named) ?? []), list.id])
    }
  }
  const reached = new Set<number>([target])
  const pending = [target]
  for (let next = pending.pop(); next !== undefined; next = pending.pop()) {
    for (const naming of namedBy.get(next) ?? []) {
      if (!reached.has(naming)) {
        reached.add(naming)
        pending.push(naming)
      }
    }
  }
  return reached
}
