/**
 * Walking and editing a filter tree without mutating it.
 *
 * A path is the list of child indexes from the root: `[]` is the root
 * predicate, `[1, 0]` is the first child of the root's second child. A `not`
 * node has one child at index 0. Every edit returns a new tree, so React sees
 * a changed reference and a smart list's live membership is never read from a
 * stale one.
 */
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

export interface FilterIssue {
  readonly path: FilterPath
  readonly message: string
}

/**
 * What still has to be filled in before the API would accept the tree.
 *
 * These are the constraints the Pydantic models carry (a non-empty group, a
 * value of at least one character, exactly one `last_contacted` option, an
 * ordered `between`) plus the three predicates the compiler refuses. Showing
 * them here means the save button can say why it is off instead of the server
 * answering 422 after the click.
 */
export function validate(node: FilterNode, path: FilterPath = []): FilterIssue[] {
  const issues: FilterIssue[] = []
  const spec = predicateOrThrow(node.op)
  if (spec.unavailable !== undefined) {
    issues.push({ path, message: `${spec.label}: ${spec.unavailable}` })
  }
  switch (node.op) {
    case 'and':
    case 'or':
      if (node.children.length === 0) {
        issues.push({ path, message: `“${spec.label}” needs at least one condition.` })
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
        issues.push({ path, message: `“${spec.label}” needs something to look for.` })
      }
      break
    case 'tag_any':
    case 'tag_all':
    case 'tag_none':
      if (node.names.length === 0) {
        issues.push({ path, message: `“${spec.label}” needs at least one tag.` })
      }
      break
    case 'eq':
    case 'neq':
      if (typeof node.value === 'string' && node.value === '') {
        issues.push({ path, message: `“${spec.label}” needs a value.` })
      }
      break
    case 'gt':
    case 'gte':
    case 'lt':
    case 'lte':
      if (node.value === '') {
        issues.push({ path, message: `“${spec.label}” needs a value.` })
      }
      break
    case 'between':
      if (node.low === '' || node.high === '') {
        issues.push({ path, message: '“is between” needs both ends.' })
      } else if (comparable(node.low) > comparable(node.high)) {
        issues.push({ path, message: 'The high end of “is between” is below the low one.' })
      }
      break
    case 'last_contacted': {
      const chosen =
        (node.within_days !== null && node.within_days !== undefined ? 1 : 0) +
        (node.older_than_days !== null && node.older_than_days !== undefined ? 1 : 0) +
        (node.never ? 1 : 0)
      if (chosen !== 1) {
        issues.push({ path, message: 'Choose exactly one “last contacted” option.' })
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
