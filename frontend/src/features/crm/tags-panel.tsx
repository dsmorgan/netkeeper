/**
 * Tag management and the auto-tag rule editor (spec 10.3).
 *
 * Manual tags are the user's; rules add and remove only the assignments they
 * made, and an automatic tag the user takes off stays off. None of that is
 * decided here — it is the service's — but the wording keeps it visible so the
 * screen does not promise something the backend does not do.
 */
import { ArrowDown, ArrowUp } from 'lucide-react'
import { useId, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'

import {
  createRule,
  createTag,
  deleteRule,
  deleteTag,
  reorderRules,
  rulesQuery,
  runAllRules,
  tagsQuery,
  updateRule,
  updateTag,
} from './api'
import type { RuleInput } from './api'
import { Callout, EmptyState, ErrorNote, LoadingNote } from './controls'
import { RulePreview } from './rule-preview'
import type { AutotagRuleOut, RuleField, TagOut } from './types'

const RULE_FIELDS: readonly RuleField[] = ['title', 'headline', 'company']

export function TagsPanel() {
  const tags = useQuery(tagsQuery)
  const rules = useQuery(rulesQuery)

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <TagList tags={tags} />
      <RuleList rules={rules} tags={tags.data ?? []} />
    </div>
  )
}

// --- tags --------------------------------------------------------------------

function TagList({ tags }: { tags: ReturnType<typeof useQuery<TagOut[]>> }) {
  const client = useQueryClient()
  const [name, setName] = useState('')
  const invalidate = () => {
    void client.invalidateQueries({ queryKey: ['tags'] })
  }
  const create = useMutation({
    mutationFn: (value: string) => createTag({ name: value }),
    onSuccess: () => {
      setName('')
      invalidate()
    },
  })
  const rename = useMutation({
    mutationFn: ({ id, value }: { id: number; value: string }) => updateTag(id, { name: value }),
    onSuccess: invalidate,
  })
  const remove = useMutation({
    mutationFn: (id: number) => deleteTag(id),
    onSuccess: invalidate,
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle level={2}>Tags</CardTitle>
        <CardDescription>
          A tag you add by hand is never removed by a rule. Deleting a tag deletes its assignments
          and its rules with it.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (name.trim() !== '') create.mutate(name.trim())
          }}
        >
          <div className="grid gap-1">
            <Label htmlFor="new-tag-name">New tag</Label>
            <Input
              id="new-tag-name"
              value={name}
              placeholder="founder"
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <Button type="submit" disabled={name.trim() === '' || create.isPending}>
            Add tag
          </Button>
        </form>
        {create.isError && <ErrorNote label="Could not add the tag" error={create.error} />}
        {rename.isError && <ErrorNote label="Could not rename the tag" error={rename.error} />}
        {remove.isError && <ErrorNote label="Could not delete the tag" error={remove.error} />}

        {tags.isPending && <LoadingNote label="Loading tags…" />}
        {tags.isError && <ErrorNote label="Could not load the tags" error={tags.error} />}
        {tags.data !== undefined &&
          (tags.data.length === 0 ? (
            <EmptyState title="No tags yet">
              <p>Add one above, or run the rules to seed the default set.</p>
            </EmptyState>
          ) : (
            <ul className="divide-y rounded-lg border">
              {tags.data.map((tag) => (
                <li key={tag.id} className="flex items-center gap-2 px-3 py-2">
                  <TagName tag={tag} onRename={(value) => rename.mutate({ id: tag.id, value })} />
                  <Badge variant="outline">{tag.kind}</Badge>
                  <span className="ml-auto text-sm text-muted-foreground">
                    {tag.contact_count.toLocaleString()} contacts
                  </span>
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`Delete ${tag.name}`}
                    onClick={() => remove.mutate(tag.id)}
                  >
                    Delete
                  </Button>
                </li>
              ))}
            </ul>
          ))}
      </CardContent>
    </Card>
  )
}

function TagName({ tag, onRename }: { tag: TagOut; onRename: (name: string) => void }) {
  const [draft, setDraft] = useState(tag.name)
  return (
    <Input
      aria-label={`Name of ${tag.name}`}
      className="w-44"
      value={draft}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={() => {
        if (draft.trim() !== '' && draft !== tag.name) onRename(draft.trim())
      }}
    />
  )
}

// --- rules -------------------------------------------------------------------

function RuleList({
  rules,
  tags,
}: {
  rules: ReturnType<typeof useQuery<AutotagRuleOut[]>>
  tags: readonly TagOut[]
}) {
  const client = useQueryClient()
  const invalidate = () => {
    void client.invalidateQueries({ queryKey: ['autotag-rules'] })
    void client.invalidateQueries({ queryKey: ['tags'] })
  }
  const save = useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: Partial<RuleInput> }) => updateRule(id, patch),
    onSuccess: invalidate,
  })
  const remove = useMutation({ mutationFn: (id: number) => deleteRule(id), onSuccess: invalidate })
  const reorder = useMutation({ mutationFn: reorderRules, onSuccess: invalidate })
  const runAll = useMutation({ mutationFn: runAllRules, onSuccess: invalidate })

  const byId = new Map(tags.map((tag) => [tag.id, tag]))

  /** Swaps the rule at `index` with its neighbour `by` places away, and saves the whole order. */
  function move(ordered: readonly AutotagRuleOut[], index: number, by: -1 | 1) {
    const ids = ordered.map((rule) => rule.id)
    const other = index + by
    const here = ids[index]
    const there = ids[other]
    if (here === undefined || there === undefined) return
    ids[index] = there
    ids[other] = here
    reorder.mutate(ids)
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle level={2}>Auto-tag rules</CardTitle>
        <CardDescription>
          Each rule searches one field for a regular expression, without regard to case. Rules run
          on every contact create and enrichment, and on demand. When two rules for the same tag
          both match a contact, the one higher in this list gets the credit.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <NewRuleForm tags={tags} onSaved={invalidate} />

        <div className="flex items-center gap-2">
          <Button variant="outline" onClick={() => runAll.mutate()} disabled={runAll.isPending}>
            Run all rules now
          </Button>
          {runAll.data !== undefined && (
            <span role="status" className="text-sm text-muted-foreground">
              {runAll.data.contacts.toLocaleString()} contacts: {runAll.data.added} added,{' '}
              {runAll.data.removed} removed
              {runAll.data.timeouts > 0 && `, ${runAll.data.timeouts} searches timed out`}
            </span>
          )}
        </div>
        {runAll.isError && <ErrorNote label="Could not run the rules" error={runAll.error} />}
        {runAll.data !== undefined && runAll.data.timeouts > 0 && (
          <Callout tone="warning" title="Some searches timed out">
            <p>
              {runAll.data.timeouts.toLocaleString()} searches hit the 50 ms budget. A timed-out
              search counts as “not known”: it adds no tag and removes none, so nothing was lost —
              but those contacts were not decided.
            </p>
          </Callout>
        )}

        {rules.isPending && <LoadingNote label="Loading rules…" />}
        {rules.isError && <ErrorNote label="Could not load the rules" error={rules.error} />}
        {save.isError && <ErrorNote label="Could not save the rule" error={save.error} />}
        {remove.isError && <ErrorNote label="Could not delete the rule" error={remove.error} />}
        {reorder.isError && <ErrorNote label="Could not reorder the rules" error={reorder.error} />}
        {rules.data !== undefined &&
          (rules.data.length === 0 ? (
            <EmptyState title="No rules yet">
              <p>Add one above, or run the rules once to seed the default set.</p>
            </EmptyState>
          ) : (
            <ul className="space-y-2">
              {rules.data.map((rule, index, ordered) => (
                <RuleRow
                  key={rule.id}
                  rule={rule}
                  tags={tags}
                  tagName={byId.get(rule.tag_id)?.name ?? `tag #${rule.tag_id}`}
                  first={index === 0}
                  last={index === ordered.length - 1}
                  busy={reorder.isPending}
                  onToggle={(enabled) => save.mutate({ id: rule.id, patch: { enabled } })}
                  onMove={(by) => move(ordered, index, by)}
                  onDelete={() => remove.mutate(rule.id)}
                  onSaved={invalidate}
                />
              ))}
            </ul>
          ))}
      </CardContent>
    </Card>
  )
}

/**
 * One rule: read as a sentence, or edited in place.
 *
 * Editing changes the rule rather than replacing it, so a typo in a pattern no
 * longer costs the rule its place in the order or its credit for the tags it
 * already added. The edit form has the same live preview as a new rule.
 */
function RuleRow({
  rule,
  tags,
  tagName,
  first,
  last,
  busy,
  onToggle,
  onMove,
  onDelete,
  onSaved,
}: {
  rule: AutotagRuleOut
  tags: readonly TagOut[]
  tagName: string
  first: boolean
  last: boolean
  busy: boolean
  onToggle: (enabled: boolean) => void
  onMove: (by: -1 | 1) => void
  onDelete: () => void
  onSaved: () => void
}) {
  const [editing, setEditing] = useState(false)

  if (editing) {
    return (
      <li data-slot="rule-row">
        <RuleForm
          label={`Edit rule ${rule.id}`}
          tags={tags}
          initial={rule}
          submitLabel="Save rule"
          errorLabel="Could not save the rule"
          save={(input) => updateRule(rule.id, input)}
          onSaved={() => {
            setEditing(false)
            onSaved()
          }}
          onCancel={() => setEditing(false)}
        />
      </li>
    )
  }

  return (
    <li
      data-slot="rule-row"
      className="flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2"
    >
      <span className="text-sm font-medium">{tagName}</span>
      <span className="text-sm text-muted-foreground">{rule.field} matches</span>
      <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{rule.pattern}</code>
      <Label className="ml-auto gap-2 text-sm">
        <Checkbox
          checked={rule.enabled}
          aria-label={`Rule ${rule.id} enabled`}
          onCheckedChange={(checked) => onToggle(checked === true)}
        />
        Enabled
      </Label>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label={`Move rule ${rule.id} up`}
        disabled={first || busy}
        onClick={() => onMove(-1)}
      >
        <ArrowUp />
      </Button>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label={`Move rule ${rule.id} down`}
        disabled={last || busy}
        onClick={() => onMove(1)}
      >
        <ArrowDown />
      </Button>
      <Button
        variant="ghost"
        size="sm"
        aria-label={`Edit rule ${rule.id}`}
        onClick={() => setEditing(true)}
      >
        Edit
      </Button>
      <Button variant="ghost" size="sm" aria-label={`Delete rule ${rule.id}`} onClick={onDelete}>
        Delete
      </Button>
    </li>
  )
}

function NewRuleForm({ tags, onSaved }: { tags: readonly TagOut[]; onSaved: () => void }) {
  return (
    <RuleForm
      label="New rule"
      tags={tags}
      submitLabel="Add rule"
      errorLabel="Could not add the rule"
      save={(input) => createRule(input)}
      onSaved={onSaved}
      resetOnSave
    />
  )
}

/** The tag, field and pattern of a rule, with the live preview under them. */
function RuleForm({
  label,
  tags,
  initial,
  submitLabel,
  errorLabel,
  save,
  onSaved,
  onCancel,
  resetOnSave = false,
}: {
  label: string
  tags: readonly TagOut[]
  initial?: AutotagRuleOut
  submitLabel: string
  errorLabel: string
  save: (input: RuleInput) => Promise<AutotagRuleOut>
  onSaved: () => void
  onCancel?: () => void
  resetOnSave?: boolean
}) {
  const id = useId()
  const [tagId, setTagId] = useState<number | null>(initial?.tag_id ?? null)
  const [field, setField] = useState<RuleField>(initial?.field ?? 'title')
  const [pattern, setPattern] = useState(initial?.pattern ?? '')
  const chosen = tagId ?? tags[0]?.id ?? null

  const submit = useMutation({
    mutationFn: save,
    onSuccess: () => {
      if (resetOnSave) setPattern('')
      onSaved()
    },
  })

  return (
    <form
      aria-label={label}
      className="space-y-2 rounded-lg border bg-muted/20 p-3"
      onSubmit={(event) => {
        event.preventDefault()
        if (chosen !== null && pattern.trim() !== '') {
          submit.mutate({
            tag_id: chosen,
            field,
            pattern: pattern.trim(),
            enabled: initial?.enabled ?? true,
          })
        }
      }}
    >
      <div className="flex flex-wrap items-end gap-2">
        <div className="grid gap-1">
          <Label htmlFor={`${id}-tag`}>Tag</Label>
          <Select
            id={`${id}-tag`}
            value={chosen === null ? '' : String(chosen)}
            onChange={(event) => setTagId(Number(event.target.value))}
          >
            {tags.length === 0 && <option value="">make a tag first</option>}
            {tags.map((tag) => (
              <option key={tag.id} value={tag.id}>
                {tag.name}
              </option>
            ))}
          </Select>
        </div>
        <div className="grid gap-1">
          <Label htmlFor={`${id}-field`}>Field</Label>
          <Select
            id={`${id}-field`}
            value={field}
            onChange={(event) => setField(event.target.value as RuleField)}
          >
            {RULE_FIELDS.map((candidate) => (
              <option key={candidate} value={candidate}>
                {candidate}
              </option>
            ))}
          </Select>
        </div>
        <div className="grid flex-1 gap-1">
          <Label htmlFor={`${id}-pattern`}>Pattern</Label>
          <Input
            id={`${id}-pattern`}
            className="font-mono"
            placeholder="\\b(founder|co-?founder)\\b"
            value={pattern}
            onChange={(event) => setPattern(event.target.value)}
          />
        </div>
        <Button
          type="submit"
          disabled={chosen === null || pattern.trim() === '' || submit.isPending}
        >
          {submitLabel}
        </Button>
        {onCancel !== undefined && (
          <Button type="button" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        )}
      </div>
      <RulePreview field={field} pattern={pattern} />
      {submit.isError && <ErrorNote label={errorLabel} error={submit.error} />}
    </form>
  )
}
