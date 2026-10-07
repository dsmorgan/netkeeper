import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

import {
  type ConfigField,
  type ConfigSettings,
  configSettingsQuery,
  postureQuery,
  saveConfigSettings,
} from './api'
import {
  type Draft,
  GROUPS,
  changed,
  draftOf,
  draftProblem,
  valueOf,
  warnAboveHint,
} from './config-fields'

/**
 * Settings you change here instead of in config.toml (#343): LinkedIn budgets, active
 * hours, campaign defaults, the LLM switch and backups. config.toml is optional; a key
 * it sets wins and shows here locked, with the file's path.
 */
function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

const SOURCE_LABEL: Record<ConfigField['source'], string> = {
  default: 'Default',
  ui: 'Set here',
  file: 'config.toml',
}

export function ConfigSections() {
  const current = useQuery(configSettingsQuery)
  if (current.isPending) return <p role="status">Loading settings…</p>
  if (current.isError) return <p role="alert">{message(current.error)}</p>
  return (
    <>
      {GROUPS.map((group) => {
        const fields = current.data.fields.filter((f) => f.group === group.id)
        if (fields.length === 0) return null
        return (
          <Card size="sm" key={group.id}>
            <CardHeader>
              <h2 className="font-heading text-sm leading-snug font-medium">{group.title}</h2>
              <CardDescription>
                {group.description} Values in config.toml win over this page
                {current.data.config_path !== null ? ` (${current.data.config_path})` : ''}.
              </CardDescription>
            </CardHeader>
            <CardContent className="text-sm">
              <GroupForm
                title={group.title}
                fields={fields}
                key={JSON.stringify(fields.map((f) => f.value))}
              />
            </CardContent>
          </Card>
        )
      })}
    </>
  )
}

function GroupForm({ title, fields }: { title: string; fields: ConfigField[] }) {
  const queryClient = useQueryClient()
  const [drafts, setDrafts] = useState<Record<string, Draft>>(() =>
    Object.fromEntries(fields.map((f) => [f.key, draftOf(f)])),
  )
  const [saved, setSaved] = useState(false)
  const save = useMutation({
    mutationFn: saveConfigSettings,
    onSuccess: (data: ConfigSettings) => {
      queryClient.setQueryData(configSettingsQuery.queryKey, data)
      void queryClient.invalidateQueries({ queryKey: postureQuery.queryKey })
      setSaved(true)
    },
  })
  const editable = fields.filter((f) => f.editable)
  const problems = editable
    .map((f) => draftProblem(f, drafts[f.key] ?? draftOf(f)))
    .filter((p) => p !== null)
  const changes = Object.fromEntries(
    editable
      .filter((f) => changed(f, drafts[f.key] ?? draftOf(f)))
      .map((f) => [f.key, valueOf(f, drafts[f.key] ?? draftOf(f))]),
  )
  const set = (key: string, draft: Draft) => {
    setSaved(false)
    setDrafts((d) => ({ ...d, [key]: draft }))
  }

  return (
    <form
      aria-label={title}
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault()
        if (problems.length === 0 && Object.keys(changes).length > 0) save.mutate(changes)
      }}
    >
      {fields.map((field) => (
        <FieldRow
          key={field.key}
          field={field}
          draft={drafts[field.key] ?? draftOf(field)}
          onChange={(draft) => set(field.key, draft)}
          onReset={() => save.mutate({ [field.key]: null })}
          resetting={save.isPending}
        />
      ))}
      {editable.length > 0 && (
        <div className="flex items-center gap-2">
          <Button
            type="submit"
            disabled={problems.length > 0 || Object.keys(changes).length === 0 || save.isPending}
          >
            Save {title.toLowerCase()}
          </Button>
          {saved && (
            <span role="status" className="text-muted-foreground">
              Saved.
            </span>
          )}
        </div>
      )}
      {save.isError && <p role="alert">{message(save.error)}</p>}
    </form>
  )
}

function FieldRow({
  field,
  draft,
  onChange,
  onReset,
  resetting,
}: {
  field: ConfigField
  draft: Draft
  onChange: (draft: Draft) => void
  onReset: () => void
  resetting: boolean
}) {
  const id = useId()
  const problem = field.editable ? draftProblem(field, draft) : null
  const hint = field.editable && problem === null ? warnAboveHint(field, draft) : null
  const disabled = !field.editable
  return (
    <div className="space-y-1.5" role="group" aria-label={field.label}>
      <div className="flex flex-wrap items-center gap-2">
        {field.kind === 'bool' ? (
          <div className="flex items-center gap-2">
            <Checkbox
              id={id}
              checked={draft === true}
              disabled={disabled}
              onCheckedChange={(checked) => onChange(checked === true)}
            />
            <Label htmlFor={id}>{field.label}</Label>
          </div>
        ) : (
          <Label htmlFor={id}>{field.label}</Label>
        )}
        <Badge variant={field.source === 'file' ? 'secondary' : 'outline'}>
          {SOURCE_LABEL[field.source]}
        </Badge>
        {field.source === 'ui' && field.editable && (
          <Button type="button" variant="link" size="sm" disabled={resetting} onClick={onReset}>
            Use the default ({describe(field, field.default)})
          </Button>
        )}
      </div>
      <FieldInput id={id} field={field} draft={draft} disabled={disabled} onChange={onChange} />
      <p className="text-muted-foreground">
        {field.help}
        {field.maximum !== null && field.kind !== 'float'
          ? ` Hard maximum ${field.maximum}.`
          : ''}{' '}
        {field.applies_note}
      </p>
      {field.locked_reason !== null && (
        <p className="text-muted-foreground">{field.locked_reason}</p>
      )}
      {field.restart_pending && (
        <p role="status" className="text-amber-800 dark:text-amber-300">
          Restart netkeeper serve to apply this value: the running server still uses the one it
          started with.
        </p>
      )}
      {field.stored_unreadable && (
        <p role="alert" className="text-destructive">
          The value saved here cannot be used, so the default applies. Save a new one.
        </p>
      )}
      {problem !== null && <p role="alert">{problem}</p>}
      {hint !== null && (
        <p role="note" className="text-amber-800 dark:text-amber-300">
          {hint}
        </p>
      )}
      {field.warnings.map((w) => (
        <p key={w} role="alert" className="text-destructive">
          Warning: {w}
        </p>
      ))}
      {field.notes.map((n) => (
        <p key={n} role="note" className="text-amber-800 dark:text-amber-300">
          {n}
        </p>
      ))}
    </div>
  )
}

function describe(field: ConfigField, value: unknown): string {
  if (field.kind === 'optional_int' && value === null) return 'automatic'
  if (field.kind === 'bool') return value === true ? 'on' : 'off'
  if (Array.isArray(value))
    return value.length === 0 ? 'none' : value.join(field.kind === 'window' ? '–' : ', ')
  return String(value)
}

function FieldInput({
  id,
  field,
  draft,
  disabled,
  onChange,
}: {
  id: string
  field: ConfigField
  draft: Draft
  disabled: boolean
  onChange: (draft: Draft) => void
}) {
  if (field.kind === 'bool') return null
  if (field.kind === 'window') {
    const [start, end] = draft as [string, string]
    return (
      <div className="flex flex-wrap items-center gap-2">
        <Input
          id={id}
          aria-label={`${field.label} from`}
          type="time"
          value={start}
          disabled={disabled}
          onChange={(event) => onChange([event.target.value, end])}
          className="w-fit"
        />
        <span>to</span>
        <Input
          aria-label={`${field.label} to`}
          type="time"
          value={end}
          disabled={disabled}
          onChange={(event) => onChange([start, event.target.value])}
          className="w-fit"
        />
      </div>
    )
  }
  if (field.kind === 'dates') {
    return (
      <textarea
        id={id}
        value={draft as string}
        disabled={disabled}
        rows={3}
        placeholder="2026-12-25"
        onChange={(event) => onChange(event.target.value)}
        className="w-full max-w-xs rounded-md border border-input bg-transparent px-2.5 py-1.5 font-mono text-sm"
      />
    )
  }
  return (
    <Input
      id={id}
      type="number"
      inputMode={field.kind === 'float' ? 'decimal' : 'numeric'}
      step={field.kind === 'float' ? 0.1 : 1}
      min={field.minimum ?? undefined}
      max={field.maximum ?? undefined}
      placeholder={field.kind === 'optional_int' ? 'Automatic' : undefined}
      value={draft as string}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      className="w-32"
    />
  )
}
