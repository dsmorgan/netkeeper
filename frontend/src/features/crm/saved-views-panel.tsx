/**
 * Saved views: a column set, a sort, and an optional filter the contacts table restores.
 *
 * A view is not a list — it selects nobody on its own. It is how the table
 * looked, saved so it can look that way again, and the filter it carries is the
 * same tree a smart list stores.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'

import { createView, deleteView, tagsQuery, updateView, viewsQuery } from './api'
import { EmptyState, ErrorNote, LoadingNote } from './controls'
import { CONTACT_COLUMNS, FIELDS } from './fields'
import { FilterBuilder } from './filter-builder'
import { emptyTree, validateTree } from './tree'
import type { ContactColumn, FilterTree, SavedViewOut, SortKey } from './types'

const DEFAULT_COLUMNS: readonly ContactColumn[] = [
  'preferred_name',
  'last_name',
  'current_title',
  'current_company',
  'met',
]

export function SavedViewsPanel() {
  const views = useQuery(viewsQuery)
  const tags = useQuery(tagsQuery)
  const client = useQueryClient()
  const [editing, setEditing] = useState<SavedViewOut | 'new' | null>(null)
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ['views'] })
  }
  const remove = useMutation({ mutationFn: (id: number) => deleteView(id), onSuccess: refresh })

  return (
    <div className="grid gap-4 lg:grid-cols-[20rem_1fr]">
      <Card>
        <CardHeader>
          <CardTitle level={2}>Saved views</CardTitle>
          <CardDescription>
            Columns, sort, and a filter, saved so the contacts table can restore them.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Button variant="outline" onClick={() => setEditing('new')}>
            New view
          </Button>
          {views.isPending && <LoadingNote label="Loading saved views…" />}
          {views.isError && (
            <ErrorNote label="Could not load the saved views" error={views.error} />
          )}
          {remove.isError && <ErrorNote label="Could not delete the view" error={remove.error} />}
          {views.data !== undefined &&
            (views.data.length === 0 ? (
              <EmptyState title="No saved views yet">
                <p>Save the table you keep rebuilding.</p>
              </EmptyState>
            ) : (
              <ul className="divide-y rounded-lg border">
                {views.data.map((view) => (
                  <li key={view.id} className="flex items-center gap-2 px-3 py-2">
                    <button
                      type="button"
                      className="flex-1 truncate text-left text-sm font-medium"
                      onClick={() => setEditing(view)}
                    >
                      {view.name}
                    </button>
                    <span className="text-xs text-muted-foreground">
                      {view.columns.length} columns
                    </span>
                    <Button
                      variant="ghost"
                      size="xs"
                      aria-label={`Delete ${view.name}`}
                      onClick={() => remove.mutate(view.id)}
                    >
                      Delete
                    </Button>
                  </li>
                ))}
              </ul>
            ))}
        </CardContent>
      </Card>

      {editing === null ? (
        <EmptyState title="Pick a view, or make one" />
      ) : (
        <ViewEditor
          key={editing === 'new' ? 'new' : editing.id}
          view={editing === 'new' ? null : editing}
          tags={tags.data ?? []}
          onSaved={() => {
            refresh()
            setEditing(null)
          }}
        />
      )}
    </div>
  )
}

function ViewEditor({
  view,
  tags,
  onSaved,
}: {
  view: SavedViewOut | null
  tags: Parameters<typeof FilterBuilder>[0]['tags']
  onSaved: () => void
}) {
  const [name, setName] = useState(view?.name ?? '')
  const [columns, setColumns] = useState<readonly ContactColumn[]>(
    (view?.columns as ContactColumn[] | undefined) ?? DEFAULT_COLUMNS,
  )
  const [sort, setSort] = useState<readonly SortKey[]>(view?.sort ?? [])
  const [filter, setFilter] = useState<FilterTree>(view?.filter ?? emptyTree())
  const issues = validateTree(filter)

  const save = useMutation({
    mutationFn: () =>
      view === null
        ? createView({ name: name.trim(), columns: [...columns], sort: [...sort], filter })
        : updateView(view.id, {
            name: name.trim(),
            columns: [...columns],
            sort: [...sort],
            filter,
          }),
    onSuccess: onSaved,
  })

  const toggle = (column: ContactColumn) =>
    setColumns((current) =>
      current.includes(column)
        ? current.filter((candidate) => candidate !== column)
        : [...current, column],
    )

  return (
    <Card>
      <CardHeader>
        <CardTitle level={2}>{view === null ? 'New view' : view.name}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid max-w-sm gap-1">
          <Label htmlFor="view-name">Name</Label>
          <Input id="view-name" value={name} onChange={(event) => setName(event.target.value)} />
        </div>

        <fieldset className="space-y-2">
          <legend className="text-sm font-medium">Columns</legend>
          <div className="grid gap-1 sm:grid-cols-3">
            {CONTACT_COLUMNS.map((column) => (
              <Label key={column} className="gap-2 text-sm font-normal">
                <Checkbox
                  checked={columns.includes(column)}
                  onCheckedChange={() => toggle(column)}
                />
                {column.replace(/_/g, ' ')}
              </Label>
            ))}
          </div>
        </fieldset>

        <fieldset className="space-y-2">
          <legend className="text-sm font-medium">Sort</legend>
          {sort.map((key, index) => (
            <div key={`${key.field}-${index}`} className="flex items-center gap-2">
              <Select
                aria-label={`Sort field ${index + 1}`}
                value={key.field}
                onChange={(event) =>
                  setSort((current) =>
                    current.map((candidate, position) =>
                      position === index
                        ? { ...candidate, field: event.target.value as SortKey['field'] }
                        : candidate,
                    ),
                  )
                }
              >
                {FIELDS.map((field) => (
                  <option key={field.name} value={field.name}>
                    {field.label}
                  </option>
                ))}
              </Select>
              <Select
                aria-label={`Sort direction ${index + 1}`}
                value={key.direction ?? 'asc'}
                onChange={(event) =>
                  setSort((current) =>
                    current.map((candidate, position) =>
                      position === index
                        ? {
                            ...candidate,
                            direction: event.target.value === 'desc' ? 'desc' : 'asc',
                          }
                        : candidate,
                    ),
                  )
                }
              >
                <option value="asc">ascending</option>
                <option value="desc">descending</option>
              </Select>
              <Button
                variant="ghost"
                size="xs"
                aria-label={`Remove sort ${index + 1}`}
                onClick={() => setSort((current) => current.filter((_, p) => p !== index))}
              >
                Remove
              </Button>
            </div>
          ))}
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              setSort((current) => [...current, { field: 'last_name', direction: 'asc' }])
            }
          >
            Add a sort
          </Button>
        </fieldset>

        <div className="space-y-2">
          <h4 className="text-sm font-medium">Filter</h4>
          <FilterBuilder value={filter} onChange={setFilter} tags={tags} />
        </div>

        <div className="flex items-center gap-2">
          <Button
            onClick={() => save.mutate()}
            disabled={
              name.trim() === '' || columns.length === 0 || issues.length > 0 || save.isPending
            }
          >
            Save view
          </Button>
          {columns.length === 0 && (
            <span className="text-sm text-muted-foreground">Pick at least one column.</span>
          )}
        </div>
        {save.isError && <ErrorNote label="Could not save the view" error={save.error} />}
      </CardContent>
    </Card>
  )
}
