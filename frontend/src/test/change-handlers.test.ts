/**
 * Every event handler reads the event's `target` before it returns (#341).
 *
 * The contact page's old Met dropdown (before #330) read `event.target.value`
 * inside the closure it handed to `write.mutate`. TanStack Query runs that
 * closure after the handler has returned, and by then React has already put a
 * controlled `<select>` back to the value it was rendered with. So the request
 * carried the old value: you picked Met, and the PATCH said `unknown`. Nothing
 * failed, nothing saved.
 *
 * The rule this pins: copy the value into a local in the handler body, then
 * use the local. Reading `event.target` or `event.currentTarget` (directly,
 * destructured, or through a local holding the element) in a nested function
 * (a `mutate` closure, a `setTimeout`, a `.then`, a state updater) fails this
 * test, even where React happens to run that function in time today. A plain
 * value a custom component passes its callback is not an event and is not
 * checked.
 */
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const sources = import.meta.glob<string>(['../**/*.tsx', '!../**/*.test.tsx'], {
  query: '?raw',
  import: 'default',
  eager: true,
})

/** Every event-handler prop: `onChange`, `onInput`, `onClick`, `onValueChange`, and so on. */
const HANDLER = /^on[A-Z]/

const EVENT_FIELDS = new Set(['target', 'currentTarget'])

type FunctionLike =
  | ts.ArrowFunction
  | ts.FunctionExpression
  | ts.FunctionDeclaration
  | ts.MethodDeclaration
  | ts.AccessorDeclaration
  | ts.ConstructorDeclaration

function isFunctionLike(node: ts.Node): node is FunctionLike {
  return (
    ts.isArrowFunction(node) ||
    ts.isFunctionExpression(node) ||
    ts.isFunctionDeclaration(node) ||
    ts.isMethodDeclaration(node) ||
    ts.isGetAccessorDeclaration(node) ||
    ts.isSetAccessorDeclaration(node) ||
    ts.isConstructorDeclaration(node)
  )
}

/** `x as T`, `(x)`, `x!` and `x satisfies T` are all still `x`. */
function unwrap(node: ts.Expression): ts.Expression {
  let current = node
  while (
    ts.isParenthesizedExpression(current) ||
    ts.isAsExpression(current) ||
    ts.isNonNullExpression(current) ||
    ts.isSatisfiesExpression(current)
  ) {
    current = current.expression
  }
  return current
}

/** The names a parameter or variable binding introduces. */
function boundNames(name: ts.BindingName): string[] {
  if (ts.isIdentifier(name)) return [name.text]
  return name.elements.flatMap((element) =>
    ts.isOmittedExpression(element) ? [] : boundNames(element.name),
  )
}

/** The names a destructuring takes from `target` or `currentTarget`, as in `({ target })`. */
function eventFieldNames(pattern: ts.ObjectBindingPattern): string[] {
  return pattern.elements.flatMap((element) => {
    const key = element.propertyName ?? element.name
    return ts.isIdentifier(key) && EVENT_FIELDS.has(key.text) ? boundNames(element.name) : []
  })
}

/** Whether `node` reads a variable, rather than naming a property, attribute, or declaration. */
function isRead(node: ts.Identifier): boolean {
  const parent = node.parent
  if (ts.isPropertyAccessExpression(parent) && parent.name === node) return false
  if (ts.isPropertyAssignment(parent) && parent.name === node) return false
  if (ts.isJsxAttribute(parent) && parent.name === node) return false
  if (ts.isBindingElement(parent) && (parent.propertyName === node || parent.name === node)) {
    return false
  }
  if (
    (ts.isVariableDeclaration(parent) ||
      ts.isParameter(parent) ||
      ts.isFunctionDeclaration(parent) ||
      ts.isMethodDeclaration(parent) ||
      ts.isPropertyDeclaration(parent)) &&
    parent.name === node
  ) {
    return false
  }
  return true
}

/**
 * `file:line` for each read of the event's `target` or `currentTarget` inside a
 * function nested in its handler. The event is the handler's first parameter;
 * a destructured `{ target }` and a local such as `const el = event.target`
 * count as the same read.
 */
function lateEventReads(fileName: string, text: string): string[] {
  const source = ts.createSourceFile(
    fileName,
    text,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  )
  const found: string[] = []
  const report = (node: ts.Node) => {
    const { line } = source.getLineAndCharacterOfPosition(node.getStart(source))
    found.push(`${fileName}:${line + 1}`)
  }

  const visitHandler = (handler: ts.ArrowFunction | ts.FunctionExpression) => {
    const param = handler.parameters[0]?.name
    if (param === undefined) return
    // Names that hold the event itself, and names that hold its target.
    const events = new Set<string>()
    const aliases = new Set<string>()
    if (ts.isIdentifier(param)) events.add(param.text)
    else if (ts.isObjectBindingPattern(param)) eventFieldNames(param).forEach((n) => aliases.add(n))
    if (events.size === 0 && aliases.size === 0) return

    // Locals the handler body itself takes from the event.
    const collect = (node: ts.Node) => {
      if (isFunctionLike(node)) return
      if (ts.isVariableDeclaration(node) && node.initializer !== undefined) {
        const init = unwrap(node.initializer)
        if (
          ts.isIdentifier(node.name) &&
          ts.isPropertyAccessExpression(init) &&
          EVENT_FIELDS.has(init.name.text) &&
          ts.isIdentifier(unwrap(init.expression)) &&
          events.has((unwrap(init.expression) as ts.Identifier).text)
        ) {
          aliases.add(node.name.text)
        }
        if (
          ts.isObjectBindingPattern(node.name) &&
          ts.isIdentifier(init) &&
          events.has(init.text)
        ) {
          eventFieldNames(node.name).forEach((n) => aliases.add(n))
        }
      }
      ts.forEachChild(node, collect)
    }
    ts.forEachChild(handler.body, collect)

    const walk = (
      node: ts.Node,
      nested: boolean,
      watchedEvents: ReadonlySet<string>,
      watchedAliases: ReadonlySet<string>,
    ) => {
      let events = watchedEvents
      let aliases = watchedAliases
      let inner = nested
      if (isFunctionLike(node)) {
        inner = true
        // A parameter that reuses a watched name is a different variable.
        const rebound = new Set(node.parameters.flatMap((p) => boundNames(p.name)))
        if (rebound.size > 0) {
          events = new Set([...events].filter((n) => !rebound.has(n)))
          aliases = new Set([...aliases].filter((n) => !rebound.has(n)))
        }
      }
      if (inner && ts.isIdentifier(node) && isRead(node)) {
        const parent = node.parent
        if (
          events.has(node.text) &&
          ts.isPropertyAccessExpression(parent) &&
          parent.expression === node &&
          EVENT_FIELDS.has(parent.name.text)
        ) {
          report(node)
        } else if (aliases.has(node.text)) {
          report(node)
        }
      }
      if (events.size === 0 && aliases.size === 0) return
      ts.forEachChild(node, (child) => walk(child, inner, events, aliases))
    }
    ts.forEachChild(handler.body, (child) => walk(child, false, events, aliases))
  }

  const visit = (node: ts.Node) => {
    if (
      ts.isJsxAttribute(node) &&
      ts.isIdentifier(node.name) &&
      HANDLER.test(node.name.text) &&
      node.initializer !== undefined &&
      ts.isJsxExpression(node.initializer)
    ) {
      const expression = node.initializer.expression
      if (
        expression !== undefined &&
        (ts.isArrowFunction(expression) || ts.isFunctionExpression(expression))
      ) {
        visitHandler(expression)
      }
    }
    ts.forEachChild(node, visit)
  }
  visit(source)
  return found
}

describe('change handlers read the value before they return (#341)', () => {
  it('finds the old Met dropdown’s fault', () => {
    // The MetEditor as it was before #330, trimmed to its handler.
    const old = `
      export function MetEditor({ contact }) {
        const write = useContactWrite(contact.id)
        return (
          <Select
            aria-label="Met"
            value={contact.met}
            onChange={(event) =>
              write.mutate(() =>
                patchContact(contact.id, { met: event.target.value }),
              )
            }
          />
        )
      }`
    expect(lateEventReads('old-met-editor.tsx', old)).toEqual(['old-met-editor.tsx:10'])
  })

  it('accepts a value copied out first', () => {
    const fixed = `
      <Select
        value={contact.met}
        onChange={(event) => {
          const met = event.target.value
          write.mutate(() => patchContact(contact.id, { met }))
        }}
      />`
    expect(lateEventReads('fixed.tsx', fixed)).toEqual([])
  })

  it.each([
    [
      'a destructured target',
      `<Select onChange={({ target }) => write.mutate(() => save(target.value))} />`,
    ],
    [
      'a destructured currentTarget, renamed',
      `<Select onChange={({ currentTarget: el }) => write.mutate(() => save(el.value))} />`,
    ],
    [
      'a local taken from event.target',
      `<Select onChange={(event) => { const el = event.target; write.mutate(() => save(el.value)) }} />`,
    ],
    [
      'a local destructured from the event',
      `<Select onChange={(event) => { const { target } = event; setTimeout(() => save(target.value)) }} />`,
    ],
    [
      'a nested function declaration',
      `<Select onChange={(event) => { function later() { save(event.target.value) } queueMicrotask(later) }} />`,
    ],
    [
      'a nested method',
      `<Select onChange={(event) => { const job = { run() { save(event.currentTarget.value) } }; schedule(job) }} />`,
    ],
    [
      'any event-handler prop',
      `<Button onClick={(event) => write.mutate(() => save(event.currentTarget.dataset.id))} />`,
    ],
  ])('flags %s', (_, code) => {
    expect(lateEventReads('late.tsx', code)).toEqual(['late.tsx:1'])
  })

  it.each([
    [
      'a property that shares the event’s name',
      `<Select onChange={(e) => { const value = e.target.value; rows.map((row) => row.e); write.mutate(() => save({ e: value })) }} />`,
    ],
    [
      'a property named value next to a value parameter',
      `<Picker onChange={(value) => setPicked(opts.filter((o) => o.value === value))} />`,
    ],
    [
      'a nested parameter that rebinds the event’s name',
      `<Select onChange={(event) => { const v = event.target.value; bus.on((event) => log(event.target, v)) }} />`,
    ],
    [
      'a nested parameter that rebinds an alias',
      `<Select onChange={({ target }) => { const v = target.value; nodes.forEach((target) => target.focus()) }} />`,
    ],
    [
      'a value callback that is not an event',
      `<Picker onValueChange={(value) => write.mutate(() => save(value))} />`,
    ],
    [
      'reads in the handler body itself',
      `<Select onChange={(event) => { const el = event.target; save(el.value, event.currentTarget.id) }} />`,
    ],
  ])('accepts %s', (_, code) => {
    expect(lateEventReads('ok.tsx', code)).toEqual([])
  })

  it('scans the app’s source', () => {
    // A glob that matched nothing would pass the next test vacuously.
    expect(Object.keys(sources).length).toBeGreaterThan(50)
  })

  it('finds no handler in the app that reads its event late', () => {
    const late = Object.entries(sources).flatMap(([file, text]) => lateEventReads(file, text))
    expect(late).toEqual([])
  })
})
