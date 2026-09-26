/**
 * One node of a filter tree, and through recursion the whole tree.
 *
 * A comparison row reads field, then op, then value, because that is the order
 * the sentence runs in. Changing the field keeps the op when the new column
 * accepts it and moves to the column's first op when it does not, so the row is
 * always a predicate the language allows rather than one the server would
 * refuse.
 */
import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { ChevronRight, X } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { cn } from '@/lib/utils'

import { listsQuery } from './api'
import { Callout } from './controls'
import { FIELDS, OPS_BY_KIND, fieldSpec } from './fields'
import type { FieldKind } from './fields'
import { predicateOrThrow } from './predicates'
import type { FilterPath } from './tree'
import { listsLeadingTo, pathKey, valueFits } from './tree'
import type { FilterField, FilterNode, TagOut } from './types'

/** The ops that compare a column, all of which carry a `field`. */
const COMPARISON_OPS = [
  'eq',
  'neq',
  'contains',
  'starts_with',
  'is_empty',
  'gt',
  'gte',
  'lt',
  'lte',
  'between',
] as const

type ComparisonOp = (typeof COMPARISON_OPS)[number]
type ComparisonNode = Extract<FilterNode, { op: ComparisonOp }>

function isComparison(node: FilterNode): node is ComparisonNode {
  return (COMPARISON_OPS as readonly string[]).includes(node.op)
}

type Scalar = string | number | boolean

function blankValue(kind: FieldKind, values: readonly string[] | undefined): Scalar {
  switch (kind) {
    case 'int':
      return 0
    case 'bool':
      return true
    case 'enum':
      return values?.[0] ?? ''
    default:
      return ''
  }
}

/**
 * A comparison node for `op` on `field`, keeping `previous` only if `field` would accept it.
 *
 * `valueFits` is the same predicate the validator runs, so the value that
 * survives a change of column is exactly the value the builder would then call
 * valid — the two cannot disagree. Neither a `typeof` test nor a comparison of
 * kinds would do: text, an enum value, a date and a datetime are all
 * JavaScript strings, and two enums are the same kind while sharing no value,
 * so `met`'s `unknown` would land in `source`. The server refuses all of those,
 * and a `<select>` given a value that is not one of its options shows the first
 * one instead, so the row read back as something the tree did not hold.
 *
 * The generated types give each op its own narrower field union — `contains`
 * takes only text columns, `is_empty` only the ones that can be empty — and
 * that pairing is exactly what `OPS_BY_KIND` enforces at every call site here.
 * TypeScript cannot follow the invariant through a computed op, so the result
 * is asserted once, here, rather than at each of the callers.
 */
function buildComparison(op: ComparisonOp, field: FilterField, previous?: Scalar): ComparisonNode {
  const spec = fieldSpec(field)
  const blank = blankValue(spec?.kind ?? 'string', spec?.values)
  const keep = previous !== undefined && valueFits(field, previous) ? previous : blank
  switch (op) {
    case 'is_empty':
      return { op, field } as ComparisonNode
    case 'between':
      return { op, field, low: keep, high: keep } as ComparisonNode
    case 'contains':
    case 'starts_with':
      return { op, field, value: typeof keep === 'string' ? keep : '' } as ComparisonNode
    default:
      return { op, field, value: keep } as ComparisonNode
  }
}

function currentValue(node: ComparisonNode): Scalar | undefined {
  if (node.op === 'is_empty') return undefined
  if (node.op === 'between') return node.low
  return node.value
}

// --- datetime plumbing -------------------------------------------------------

/**
 * `<input type="datetime-local">` speaks wall-clock time with no offset, and
 * the filter language rejects a naive datetime. These two carry the value
 * across that boundary through the browser's own timezone.
 */
function isoToLocalInput(value: Scalar): string {
  if (typeof value !== 'string' || value === '') return ''
  const at = new Date(value)
  if (Number.isNaN(at.getTime())) return ''
  const pad = (part: number) => String(part).padStart(2, '0')
  return (
    `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}` +
    `T${pad(at.getHours())}:${pad(at.getMinutes())}`
  )
}

function localInputToIso(local: string): string {
  if (local === '') return ''
  const at = new Date(local)
  return Number.isNaN(at.getTime()) ? '' : at.toISOString()
}

// --- scalar editor -----------------------------------------------------------

interface ScalarEditorProps {
  kind: FieldKind
  values?: readonly string[]
  value: Scalar
  label: string
  onChange: (next: Scalar) => void
}

function ScalarEditor({ kind, values, value, label, onChange }: ScalarEditorProps) {
  switch (kind) {
    case 'enum':
      return (
        <Select
          aria-label={label}
          value={String(value)}
          onChange={(event) => onChange(event.target.value)}
        >
          {(values ?? []).map((option) => (
            <option key={option} value={option}>
              {option.replace(/_/g, ' ')}
            </option>
          ))}
        </Select>
      )
    case 'bool':
      return (
        <Select
          aria-label={label}
          value={value === true ? 'true' : 'false'}
          onChange={(event) => onChange(event.target.value === 'true')}
        >
          <option value="true">yes</option>
          <option value="false">no</option>
        </Select>
      )
    case 'int':
      return (
        <Input
          aria-label={label}
          type="number"
          className="w-24"
          value={typeof value === 'number' ? String(value) : ''}
          onChange={(event) => onChange(event.target.value === '' ? 0 : Number(event.target.value))}
        />
      )
    case 'date':
      return (
        <Input
          aria-label={label}
          type="date"
          className="w-44"
          value={typeof value === 'string' ? value : ''}
          onChange={(event) => onChange(event.target.value)}
        />
      )
    case 'datetime':
      return (
        <Input
          aria-label={label}
          type="datetime-local"
          className="w-56"
          value={isoToLocalInput(value)}
          onChange={(event) => onChange(localInputToIso(event.target.value))}
        />
      )
    default:
      return (
        <Input
          aria-label={label}
          className="w-56"
          value={typeof value === 'string' ? value : ''}
          onChange={(event) => onChange(event.target.value)}
        />
      )
  }
}

// --- list picker -------------------------------------------------------------

/**
 * Which list `list_member` names.
 *
 * `list_id: 0` is what the palette creates, and no list has that id, so a row
 * left alone selects nobody rather than silently selecting the first list —
 * the count under the builder says zero and the reason is on screen. A list
 * the picker does not know (deleted since the filter was saved, most often)
 * keeps its id and says so, because dropping it would quietly change what a
 * saved filter means.
 *
 * Inside a smart list's own filter (`editingListId`), the list being edited and
 * every list that already leads back to it are left out (#140): choosing one
 * would define the list in terms of itself, which the server refuses only once
 * Save is pressed. A row that already names one keeps it, for the same reason a
 * missing list is kept.
 */
function ListPicker({
  listId,
  editingListId,
  onChange,
}: {
  listId: number
  editingListId?: number
  onChange: (listId: number) => void
}) {
  const lists = useQuery(listsQuery)
  const known = lists.data ?? []
  const missing = listId !== 0 && !known.some((list) => list.id === listId)
  const barred =
    editingListId === undefined ? new Set<number>() : listsLeadingTo(editingListId, known)
  const choices = known.filter((list) => !barred.has(list.id) || list.id === listId)

  if (lists.isError) {
    return (
      <span className="text-sm text-muted-foreground">
        The lists could not be read, so this row still names list #{listId}.
      </span>
    )
  }
  if (!lists.isPending && known.length === 0) {
    return <span className="text-sm text-muted-foreground">No lists yet — make one first.</span>
  }
  if (!lists.isPending && choices.length === 0 && !missing) {
    return (
      <span className="text-sm text-muted-foreground">
        No list to choose — a list cannot include itself, or a list that includes it. Make another
        list first.
      </span>
    )
  }
  return (
    <>
      <Select
        aria-label="List"
        value={String(listId)}
        onChange={(event) => onChange(Number(event.target.value))}
      >
        <option value="0">Choose a list…</option>
        {missing && <option value={String(listId)}>list #{listId} (no longer there)</option>}
        {choices.map((list) => (
          <option key={list.id} value={String(list.id)}>
            {list.name} ({list.kind})
          </option>
        ))}
      </Select>
      {missing && (
        <span className="text-sm text-muted-foreground">
          This filter names a list that is no longer there. It matches nobody until you choose
          another.
        </span>
      )}
    </>
  )
}

// --- tag chips ---------------------------------------------------------------

function TagPicker({
  names,
  tags,
  onChange,
}: {
  names: readonly string[]
  tags: readonly TagOut[]
  onChange: (next: string[]) => void
}) {
  const known = new Set(tags.map((tag) => tag.name.toLowerCase()))
  const orphans = names.filter((name) => !known.has(name.toLowerCase()))
  const toggle = (name: string) => {
    const lower = name.toLowerCase()
    const chosen = names.some((current) => current.toLowerCase() === lower)
    onChange(chosen ? names.filter((current) => current.toLowerCase() !== lower) : [...names, name])
  }

  if (tags.length === 0 && orphans.length === 0) {
    return <span className="text-sm text-muted-foreground">No tags yet — make one first.</span>
  }
  return (
    <div className="flex flex-wrap gap-1">
      {tags.map((tag) => {
        const chosen = names.some((current) => current.toLowerCase() === tag.name.toLowerCase())
        return (
          <button
            key={tag.id}
            type="button"
            aria-pressed={chosen}
            onClick={() => toggle(tag.name)}
            className={cn(
              'rounded-4xl border px-2 py-0.5 text-xs transition-colors',
              chosen
                ? 'border-primary bg-primary text-primary-foreground'
                : 'border-border text-muted-foreground hover:bg-muted',
            )}
          >
            {tag.name}
          </button>
        )
      })}
      {orphans.map((name) => (
        <button
          key={`orphan-${name}`}
          type="button"
          aria-pressed
          onClick={() => toggle(name)}
          className="rounded-4xl border border-dashed border-amber-500/60 px-2 py-0.5 text-xs"
          title="No tag by this name — it matches nobody until you make one."
        >
          {name} (unknown)
        </button>
      ))}
    </div>
  )
}

// --- the node ----------------------------------------------------------------

export interface NodeEditorProps {
  node: FilterNode
  path: FilterPath
  tags: readonly TagOut[]
  /** See `FilterBuilderProps.editingListId`. */
  editingListId?: number
  onChange: (path: FilterPath, next: FilterNode) => void
  onRemove: (path: FilterPath) => void
  /** Ask the builder to open the palette for the group at `path`. */
  onRequestAdd: (path: FilterPath) => void
  /** The key of the group whose palette is open, if any. */
  openPaletteKey: string | null
  /** Rendered by the builder so the palette lives in one place. */
  renderPalette: (path: FilterPath) => ReactNode
}

export function NodeEditor(props: NodeEditorProps) {
  const { node, path } = props
  const spec = predicateOrThrow(node.op)

  if (node.op === 'and' || node.op === 'or') {
    return (
      <div
        data-slot="filter-group"
        data-op={node.op}
        data-path={pathKey(path)}
        className="space-y-2 rounded-lg border bg-muted/20 p-2"
      >
        <div className="flex items-center gap-2">
          <Select
            aria-label="Group"
            value={node.op}
            onChange={(event) =>
              props.onChange(path, { ...node, op: event.target.value === 'or' ? 'or' : 'and' })
            }
          >
            <option value="and">All of</option>
            <option value="or">Any of</option>
          </Select>
          <span className="text-xs text-muted-foreground">{spec.hint}</span>
          <RemoveButton label={spec.label} onClick={() => props.onRemove(path)} />
        </div>
        <div className="space-y-2 border-l pl-3">
          {node.children.map((child, index) => (
            <NodeEditor
              key={pathKey([...path, index])}
              {...props}
              node={child}
              path={[...path, index]}
            />
          ))}
          {node.children.length === 0 && (
            <p className="text-sm text-muted-foreground">No conditions yet.</p>
          )}
          {props.openPaletteKey === pathKey(path) ? (
            props.renderPalette(path)
          ) : (
            <button
              type="button"
              onClick={() => props.onRequestAdd(path)}
              className="inline-flex items-center gap-1 text-sm text-primary underline-offset-4 hover:underline"
            >
              <ChevronRight className="size-3" aria-hidden />
              Add a condition
            </button>
          )}
        </div>
      </div>
    )
  }

  if (node.op === 'not') {
    return (
      <div
        data-slot="filter-group"
        data-op="not"
        data-path={pathKey(path)}
        className="space-y-2 rounded-lg border border-dashed p-2"
      >
        <div className="flex items-center gap-2">
          <Badge variant="secondary">Not</Badge>
          <span className="text-xs text-muted-foreground">{spec.hint}</span>
          <RemoveButton label={spec.label} onClick={() => props.onRemove(path)} />
        </div>
        <div className="border-l pl-3">
          <NodeEditor
            key={pathKey([...path, 0])}
            {...props}
            node={node.child}
            path={[...path, 0]}
          />
        </div>
      </div>
    )
  }

  return (
    <div
      data-slot="filter-node"
      data-op={node.op}
      data-path={pathKey(path)}
      className="flex flex-wrap items-center gap-2 rounded-lg border bg-card px-2 py-1.5"
    >
      <LeafControls {...props} node={node} />
      <RemoveButton label={spec.label} onClick={() => props.onRemove(path)} />
    </div>
  )
}

function RemoveButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-label={`Remove ${label}`}
      onClick={onClick}
      className="ml-auto rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
    >
      <X className="size-3.5" aria-hidden />
    </button>
  )
}

type LeafProps = NodeEditorProps & { node: Exclude<FilterNode, { op: 'and' | 'or' | 'not' }> }

function LeafControls({ node, path, tags, editingListId, onChange }: LeafProps) {
  const spec = predicateOrThrow(node.op)

  if (isComparison(node)) {
    const field = fieldSpec(node.field)
    const kind = field?.kind ?? 'string'
    const ops = OPS_BY_KIND[kind] as readonly ComparisonOp[]
    return (
      <>
        <Select
          aria-label="Field"
          value={node.field}
          onChange={(event) => {
            const next = event.target.value as FilterField
            const nextKind = fieldSpec(next)?.kind ?? 'string'
            const allowed = OPS_BY_KIND[nextKind] as readonly ComparisonOp[]
            const op = allowed.includes(node.op) ? node.op : (allowed[0] ?? 'eq')
            onChange(path, buildComparison(op, next, currentValue(node)))
          }}
        >
          {FIELDS.map((candidate) => (
            <option key={candidate.name} value={candidate.name}>
              {candidate.label}
            </option>
          ))}
        </Select>
        <Select
          aria-label="Comparison"
          value={node.op}
          onChange={(event) =>
            onChange(
              path,
              buildComparison(event.target.value as ComparisonOp, node.field, currentValue(node)),
            )
          }
        >
          {ops.map((candidate) => (
            <option key={candidate} value={candidate}>
              {predicateOrThrow(candidate).label}
            </option>
          ))}
        </Select>
        {node.op === 'between' ? (
          <>
            <ScalarEditor
              kind={kind}
              values={field?.values}
              label="From"
              value={node.low}
              onChange={(low) => onChange(path, { ...node, low })}
            />
            <span className="text-sm text-muted-foreground">and</span>
            <ScalarEditor
              kind={kind}
              values={field?.values}
              label="To"
              value={node.high}
              onChange={(high) => onChange(path, { ...node, high })}
            />
          </>
        ) : node.op === 'is_empty' ? null : (
          <ScalarEditor
            kind={node.op === 'contains' || node.op === 'starts_with' ? 'string' : kind}
            values={field?.values}
            label="Value"
            value={node.value}
            onChange={(value) =>
              onChange(
                path,
                node.op === 'contains' || node.op === 'starts_with'
                  ? { ...node, value: String(value) }
                  : { ...node, value },
              )
            }
          />
        )}
      </>
    )
  }

  const label = <span className="text-sm font-medium">{spec.label}</span>

  switch (node.op) {
    case 'has_email':
      return (
        <>
          {label}
          <Select
            aria-label="Email status"
            value={node.status ?? 'any'}
            onChange={(event) => {
              const chosen = event.target.value
              onChange(
                path,
                chosen === 'any'
                  ? { op: 'has_email' }
                  : { op: 'has_email', status: chosen as 'ok' | 'bounced' | 'invalid' },
              )
            }}
          >
            <option value="any">of any status</option>
            <option value="ok">that works</option>
            <option value="bounced">that bounced</option>
            <option value="invalid">that is invalid</option>
          </Select>
        </>
      )
    case 'has_phone':
    case 'has_li_url':
    case 'has_position':
      return label
    case 'email_contains':
      return (
        <>
          {label}
          <Input
            aria-label="Value"
            className="w-56"
            value={node.value}
            onChange={(event) => onChange(path, { ...node, value: event.target.value })}
          />
        </>
      )
    case 'last_contacted': {
      const mode = node.never
        ? 'never'
        : node.older_than_days !== null && node.older_than_days !== undefined
          ? 'older_than'
          : 'within'
      const days = node.within_days ?? node.older_than_days ?? 90
      return (
        <>
          {label}
          <Select
            aria-label="Window"
            value={mode}
            onChange={(event) => {
              const chosen = event.target.value
              if (chosen === 'never') {
                onChange(path, { op: 'last_contacted', never: true })
              } else if (chosen === 'older_than') {
                onChange(path, { op: 'last_contacted', never: false, older_than_days: days })
              } else {
                onChange(path, { op: 'last_contacted', never: false, within_days: days })
              }
            }}
          >
            <option value="within">in the last</option>
            <option value="older_than">more than</option>
            <option value="never">never</option>
          </Select>
          {mode !== 'never' && (
            <>
              <Input
                aria-label="Days"
                type="number"
                min={0}
                className="w-20"
                value={String(days)}
                onChange={(event) => {
                  const next = Math.max(0, Number(event.target.value) || 0)
                  onChange(
                    path,
                    mode === 'within'
                      ? { op: 'last_contacted', never: false, within_days: next }
                      : { op: 'last_contacted', never: false, older_than_days: next },
                  )
                }}
              />
              <span className="text-sm text-muted-foreground">
                {mode === 'within' ? 'days' : 'days ago'}
              </span>
            </>
          )}
        </>
      )
    }
    case 'connected_within_days':
    case 'changed_jobs_within_days':
      return (
        <>
          {label}
          <Input
            aria-label="Days"
            type="number"
            min={0}
            className="w-20"
            value={String(node.days)}
            onChange={(event) =>
              onChange(path, { ...node, days: Math.max(0, Number(event.target.value) || 0) })
            }
          />
          <span className="text-sm text-muted-foreground">days</span>
        </>
      )
    case 'tag_any':
    case 'tag_all':
    case 'tag_none':
      return (
        <>
          {label}
          <TagPicker
            names={node.names}
            tags={tags}
            onChange={(names) => onChange(path, { ...node, names })}
          />
        </>
      )
    case 'list_member':
      return (
        <ListPicker
          listId={node.list_id}
          editingListId={editingListId}
          onChange={(listId) => onChange(path, { ...node, list_id: listId })}
        />
      )
    case 'enrolled_in':
    case 'replied_in':
      return (
        <UnavailableLeaf
          label={spec.label}
          reason={spec.unavailable ?? ''}
          detail={`campaign #${node.campaign_id}`}
        />
      )
    default:
      return label
  }
}

function UnavailableLeaf({
  label,
  reason,
  detail,
}: {
  label: string
  reason: string
  detail: string
}) {
  return (
    <div className="min-w-0 flex-1 space-y-1">
      <span className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
        {label} {detail}
        <Badge variant="outline">Unavailable</Badge>
      </span>
      <Callout tone="warning">
        <p>{reason}</p>
      </Callout>
    </div>
  )
}
