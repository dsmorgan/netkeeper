/**
 * The builder cannot silently fall behind the filter language.
 *
 * `openapi.json` is the backend's published contract: `make gen-client`
 * regenerates it and CI fails when the committed copy drifts from the running
 * app. Its `FilterNode-Input` discriminator maps every `op` the language
 * defines onto that op's schema, so it is the authority for "what predicates
 * exist" and for "what each one may carry".
 *
 * These tests read it directly. A predicate added to `netkeeper/crm/filters.py`
 * fails the first test until the catalog carries it; a predicate whose shape
 * changed fails the last one until the builder's node matches again.
 */
import { describe, expect, it } from 'vitest'

import openapiRaw from '../../../openapi.json?raw'

import { CONTACT_COLUMNS, FIELDS, OPS_BY_KIND } from './fields'
import { ALL_OPS, PREDICATES, UNAVAILABLE_OPS } from './predicates'

interface OpenApi {
  components: { schemas: Record<string, JsonSchema> }
}

interface JsonSchema {
  $ref?: string
  type?: string
  const?: unknown
  enum?: unknown[]
  anyOf?: JsonSchema[]
  oneOf?: JsonSchema[]
  items?: JsonSchema
  properties?: Record<string, JsonSchema>
  required?: string[]
  additionalProperties?: boolean
  minLength?: number
  minItems?: number
  minimum?: number
  discriminator?: { mapping: Record<string, string> }
  'x-netkeeper-fields'?: Record<
    string,
    { kind: string; label: string; ops: string[]; values?: string[] }
  >
}

const doc = JSON.parse(openapiRaw) as OpenApi

function schema(name: string): JsonSchema {
  const found = doc.components.schemas[name]
  if (found === undefined) throw new Error(`${name} is not in the OpenAPI document`)
  return found
}

function deref(node: JsonSchema): JsonSchema {
  let current = node
  while (current.$ref !== undefined) {
    current = schema(current.$ref.replace('#/components/schemas/', ''))
  }
  return current
}

/**
 * A small JSON Schema checker, enough for the subset Pydantic emits here:
 * refs, unions, const and enum, the scalar types with their bounds, arrays,
 * and objects with `required` and `additionalProperties: false`.
 */
function check(node: JsonSchema, value: unknown, path = 'node'): string[] {
  const s = deref(node)
  const branches = s.oneOf ?? s.anyOf
  if (branches !== undefined) {
    const matched = branches.some((branch) => check(branch, value, path).length === 0)
    return matched ? [] : [`${path}: matches none of the ${branches.length} alternatives`]
  }
  if (s.const !== undefined && value !== s.const) {
    return [`${path}: expected ${String(s.const)}`]
  }
  if (s.enum !== undefined && !s.enum.includes(value)) {
    return [`${path}: ${JSON.stringify(value)} is not one of ${JSON.stringify(s.enum)}`]
  }
  switch (s.type) {
    case 'null':
      return value === null ? [] : [`${path}: expected null`]
    case 'string':
      if (typeof value !== 'string') return [`${path}: expected a string`]
      return s.minLength !== undefined && value.length < s.minLength
        ? [`${path}: shorter than ${s.minLength}`]
        : []
    case 'integer':
    case 'number':
      if (typeof value !== 'number' || (s.type === 'integer' && !Number.isInteger(value))) {
        return [`${path}: expected ${s.type}`]
      }
      return s.minimum !== undefined && value < s.minimum ? [`${path}: below ${s.minimum}`] : []
    case 'boolean':
      return typeof value === 'boolean' ? [] : [`${path}: expected a boolean`]
    case 'array': {
      if (!Array.isArray(value)) return [`${path}: expected an array`]
      const issues =
        s.minItems !== undefined && value.length < s.minItems
          ? [`${path}: fewer than ${s.minItems} items`]
          : []
      value.forEach((item, index) => {
        if (s.items !== undefined) issues.push(...check(s.items, item, `${path}[${index}]`))
      })
      return issues
    }
    case 'object': {
      if (typeof value !== 'object' || value === null || Array.isArray(value)) {
        return [`${path}: expected an object`]
      }
      const properties = s.properties ?? {}
      const issues: string[] = []
      for (const key of s.required ?? []) {
        if (!(key in value)) issues.push(`${path}.${key}: required`)
      }
      for (const [key, item] of Object.entries(value)) {
        const property = properties[key]
        if (property === undefined) {
          if (s.additionalProperties === false) issues.push(`${path}.${key}: not allowed`)
          continue
        }
        issues.push(...check(property, item, `${path}.${key}`))
      }
      return issues
    }
    default:
      return []
  }
}

const filterNode = schema('FilterNode-Input')
const languageOps = Object.keys(filterNode.discriminator?.mapping ?? {}).sort()

describe('the predicate catalog and the filter language', () => {
  it('offers every predicate the language defines', () => {
    expect([...ALL_OPS].sort()).toEqual(languageOps)
  })

  it('still describes the twenty-seven predicates this builder was written against', () => {
    // A failure here means the language grew or shrank. That is allowed — add
    // the predicate to `predicates.ts` and change this number deliberately, so
    // nobody widens the language without deciding how the builder shows it.
    expect(languageOps).toHaveLength(27)
  })

  it('lists no predicate the language does not have', () => {
    for (const spec of PREDICATES) {
      expect(languageOps).toContain(spec.op)
    }
  })

  it('marks exactly the predicates the compiler refuses as unavailable', () => {
    // `filters.PLACEHOLDERS`; `tests/test_filter_builder.py` checks this side
    // against the backend's own dict, which is where a graduation shows up.
    expect([...UNAVAILABLE_OPS].sort()).toEqual(['enrolled_in', 'list_member', 'replied_in'])
  })

  it('gives every unavailable predicate a reason worth reading', () => {
    for (const spec of PREDICATES.filter((candidate) => candidate.unavailable !== undefined)) {
      expect(spec.unavailable?.length ?? 0).toBeGreaterThan(30)
    }
  })

  it('builds an example of every predicate that the API would accept', () => {
    for (const spec of PREDICATES) {
      expect({ op: spec.op, issues: check(filterNode, spec.example) }).toEqual({
        op: spec.op,
        issues: [],
      })
    }
  })

  it('builds fresh nodes that carry only keys the predicate allows', () => {
    // A fresh node may still be missing a value the person has to type, so it
    // is not validated whole; what it must never do is invent a key.
    for (const spec of PREDICATES) {
      const target = filterNode.discriminator?.mapping[spec.op]
      expect(target).toBeDefined()
      const allowed = Object.keys(
        schema(String(target).replace('#/components/schemas/', '')).properties ?? {},
      )
      expect({ op: spec.op, keys: Object.keys(spec.create()).sort() }).toEqual({
        op: spec.op,
        keys: Object.keys(spec.create())
          .filter((key) => allowed.includes(key))
          .sort(),
      })
    }
  })
})

describe('the field catalog', () => {
  const published = schema('FilterTree-Input')['x-netkeeper-fields']

  it('matches the fields the backend publishes for the builder', () => {
    expect(published).toBeDefined()
    const ours = Object.fromEntries(
      FIELDS.map((spec) => [
        spec.name,
        {
          kind: spec.kind,
          label: spec.label,
          ops: [...OPS_BY_KIND[spec.kind]],
          ...(spec.values === undefined ? {} : { values: [...spec.values] }),
        },
      ]),
    )
    expect(ours).toEqual(published)
  })

  it('offers every column a saved view can show', () => {
    const columns = deref(schema('ContactQuery').properties?.columns ?? {})
    const enumerated = deref(columns.anyOf?.[0] ?? {}).items?.enum ?? []
    expect([...CONTACT_COLUMNS]).toEqual(enumerated)
  })
})
