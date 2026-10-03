/**
 * Every `onChange` and `onInput` handler reads the control's value before it
 * returns (#341).
 *
 * The contact page's old Met dropdown (before #330) read `event.target.value`
 * inside the closure it handed to `write.mutate`. TanStack Query runs that
 * closure after the handler has returned, and by then React has already put a
 * controlled `<select>` back to the value it was rendered with. So the request
 * carried the old value: you picked Met, and the PATCH said `unknown`. Nothing
 * failed, nothing saved.
 *
 * The rule this pins: copy the value into a local in the handler body, then
 * use the local. A value read in a nested function (a `mutate` closure, a
 * `setTimeout`, a `.then`, a state updater) fails this test, even where React
 * happens to run that function in time today.
 */
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const sources = import.meta.glob<string>(['../**/*.tsx', '!../**/*.test.tsx'], {
  query: '?raw',
  import: 'default',
  eager: true,
})

const HANDLERS = new Set(['onChange', 'onInput'])

/** `file:line` for each event parameter read inside a function nested in its handler. */
function lateEventReads(fileName: string, text: string): string[] {
  const source = ts.createSourceFile(
    fileName,
    text,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  )
  const found: string[] = []

  const visitHandler = (handler: ts.ArrowFunction | ts.FunctionExpression) => {
    const param = handler.parameters[0]?.name
    if (param === undefined || !ts.isIdentifier(param)) return
    const name = param.text
    const walk = (node: ts.Node, nested: boolean) => {
      if (nested && ts.isIdentifier(node) && node.text === name) {
        const { line } = source.getLineAndCharacterOfPosition(node.getStart(source))
        found.push(`${fileName}:${line + 1}`)
      }
      const inner = nested || ts.isArrowFunction(node) || ts.isFunctionExpression(node)
      ts.forEachChild(node, (child) => walk(child, inner))
    }
    ts.forEachChild(handler.body, (child) => walk(child, false))
  }

  const visit = (node: ts.Node) => {
    if (
      ts.isJsxAttribute(node) &&
      ts.isIdentifier(node.name) &&
      HANDLERS.has(node.name.text) &&
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

  it('scans the app’s source', () => {
    // A glob that matched nothing would pass the next test vacuously.
    expect(Object.keys(sources).length).toBeGreaterThan(50)
  })

  it('finds no handler in the app that reads its event late', () => {
    const late = Object.entries(sources).flatMap(([file, text]) => lateEventReads(file, text))
    expect(late).toEqual([])
  })
})
