/**
 * Static lists, smart lists, and their membership (spec 10.4).
 *
 * A static list is explicit membership. A smart list stores a filter, and its
 * members are whatever that filter selects right now: nothing is materialized,
 * so this screen never holds membership across a filter edit. Saving a new
 * filter changes the list's `updated_at`, which is part of the membership
 * query's key, so the old page cannot survive the save.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { cn } from '@/lib/utils'

import {
  addMembers,
  createList,
  deleteList,
  listsQuery,
  membersQuery,
  removeMember,
  tagsQuery,
  updateList,
} from './api'
import { BulkActionBar } from './bulk-actions'
import { Callout, EmptyState, ErrorNote, LoadingNote } from './controls'
import { ExportDialog } from './export-dialog'
import { FilterBuilder } from './filter-builder'
import { emptyTree, validateTree } from './tree'
import type { FilterTree, ListKind, ListOut } from './types'

export function ListsPanel() {
  const lists = useQuery(listsQuery)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const selected = lists.data?.find((row) => row.id === selectedId) ?? null

  return (
    <div className="grid gap-4 lg:grid-cols-[20rem_1fr]">
      <Card>
        <CardHeader>
          <CardTitle>Lists</CardTitle>
          <CardDescription>
            A static list is the people you put in it. A smart list is a filter, counted fresh every
            time you look.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <NewListForm />
          {lists.isPending && <LoadingNote label="Loading lists…" />}
          {lists.isError && <ErrorNote label="Could not load the lists" error={lists.error} />}
          {lists.data !== undefined &&
            (lists.data.length === 0 ? (
              <EmptyState title="No lists yet">
                <p>Make one above — “First 100” is the usual first batch.</p>
              </EmptyState>
            ) : (
              <ul className="divide-y rounded-lg border">
                {lists.data.map((row) => (
                  <li key={row.id}>
                    <button
                      type="button"
                      onClick={() => setSelectedId(row.id)}
                      aria-current={row.id === selectedId}
                      className={cn(
                        'flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-muted',
                        row.id === selectedId && 'bg-muted',
                      )}
                    >
                      <span className="truncate text-sm font-medium">{row.name}</span>
                      <Badge variant="outline">{row.kind}</Badge>
                      <span className="ml-auto text-sm text-muted-foreground">
                        {row.member_count.toLocaleString()}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            ))}
        </CardContent>
      </Card>

      {selected === null ? (
        <EmptyState title="Pick a list">
          <p>Its members, its filter, and its exports appear here.</p>
        </EmptyState>
      ) : (
        <ListDetail key={selected.id} list={selected} onDeleted={() => setSelectedId(null)} />
      )}
    </div>
  )
}

function NewListForm() {
  const client = useQueryClient()
  const [name, setName] = useState('')
  const [kind, setKind] = useState<ListKind>('static')
  const create = useMutation({
    mutationFn: () =>
      createList({ name: name.trim(), kind, filter: kind === 'smart' ? emptyTree() : null }),
    onSuccess: () => {
      setName('')
      void client.invalidateQueries({ queryKey: ['lists'] })
    },
  })

  return (
    <form
      className="space-y-2"
      onSubmit={(event) => {
        event.preventDefault()
        if (name.trim() !== '') create.mutate()
      }}
    >
      <div className="flex items-end gap-2">
        <div className="grid flex-1 gap-1">
          <Label htmlFor="new-list-name">New list</Label>
          <Input
            id="new-list-name"
            value={name}
            placeholder="First 100"
            onChange={(event) => setName(event.target.value)}
          />
        </div>
        <div className="grid gap-1">
          <Label htmlFor="new-list-kind">Kind</Label>
          <Select
            id="new-list-kind"
            value={kind}
            onChange={(event) => setKind(event.target.value as ListKind)}
          >
            <option value="static">static</option>
            <option value="smart">smart</option>
          </Select>
        </div>
        <Button type="submit" disabled={name.trim() === '' || create.isPending}>
          Create
        </Button>
      </div>
      {create.isError && <ErrorNote label="Could not create the list" error={create.error} />}
    </form>
  )
}

/**
 * The filter an export runs for one list.
 *
 * A smart list *is* its filter. A static list is its members, which the
 * language names with `list_member` — the predicate the compiler refused until
 * P1-27 (#73), which is why exporting a static list used to mean exporting
 * everybody and the button was withheld rather than offered wrong. The server
 * reads the membership rows, so this stays right as the list changes.
 */
function exportFilter(list: ListOut): FilterTree {
  if (list.kind === 'smart') return list.filter ?? emptyTree()
  return { where: { op: 'list_member', list_id: list.id }, include_archived: false }
}

function ListDetail({ list, onDeleted }: { list: ListOut; onDeleted: () => void }) {
  const client = useQueryClient()
  const tags = useQuery(tagsQuery)
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ['lists'] })
  }
  const remove = useMutation({
    mutationFn: () => deleteList(list.id),
    onSuccess: () => {
      onDeleted()
      refresh()
    },
  })

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {list.name}
            <Badge variant="outline">{list.kind}</Badge>
          </CardTitle>
          <CardDescription>
            {list.member_count.toLocaleString()} contacts right now.
            {list.kind === 'smart' &&
              ' Membership is computed from the filter on every request — nothing is stored.'}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <ExportDialog
              filter={exportFilter(list)}
              listName={list.name}
              listCount={list.member_count}
            />
            <Button
              variant="destructive"
              onClick={() => remove.mutate()}
              disabled={remove.isPending}
            >
              Delete list
            </Button>
          </div>
        </CardContent>
      </Card>

      {remove.isError && <ErrorNote label="Could not delete the list" error={remove.error} />}

      {list.kind === 'smart' ? (
        <SmartListFilter list={list} tags={tags.data ?? []} onSaved={refresh} />
      ) : (
        <StaticListMembers list={list} onChanged={refresh} />
      )}

      <Card>
        <CardHeader>
          <CardTitle>Bulk actions</CardTitle>
          <CardDescription>
            Every action confirms a count first. If the selection moves between the count and the
            click, nothing is applied and the new count is shown.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {list.kind === 'smart' ? (
            <BulkActionBar selection={{ filter: list.filter ?? emptyTree() }} onApplied={refresh} />
          ) : (
            <StaticBulkActions list={list} onApplied={refresh} />
          )}
        </CardContent>
      </Card>

      <MemberTable list={list} />
    </div>
  )
}

function SmartListFilter({
  list,
  tags,
  onSaved,
}: {
  list: ListOut
  tags: Parameters<typeof FilterBuilder>[0]['tags']
  onSaved: () => void
}) {
  const [draft, setDraft] = useState<FilterTree>(list.filter ?? emptyTree())
  const issues = validateTree(draft)
  const save = useMutation({
    mutationFn: () => updateList(list.id, { filter: draft }),
    onSuccess: onSaved,
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Filter</CardTitle>
        <CardDescription>
          The same filter the contacts table, a campaign audience, and an export run.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <FilterBuilder value={draft} onChange={setDraft} tags={tags} />
        <div className="flex items-center gap-2">
          <Button onClick={() => save.mutate()} disabled={issues.length > 0 || save.isPending}>
            Save filter
          </Button>
          {save.isSuccess && (
            <span role="status" className="text-sm text-muted-foreground">
              Saved. Membership below is recounted from the new filter.
            </span>
          )}
        </div>
        {save.isError && <ErrorNote label="The server refused the filter" error={save.error} />}
      </CardContent>
    </Card>
  )
}

function StaticListMembers({ list, onChanged }: { list: ListOut; onChanged: () => void }) {
  const [ids, setIds] = useState('')
  const add = useMutation({
    mutationFn: () =>
      addMembers(
        list.id,
        ids
          .split(/[\s,]+/)
          .map((part) => Number(part))
          .filter((value) => Number.isInteger(value) && value > 0),
      ),
    onSuccess: () => {
      setIds('')
      onChanged()
    },
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Membership</CardTitle>
        <CardDescription>
          Contacts you put in this list by hand, each with the moment it was added.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <form
          className="flex items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (ids.trim() !== '') add.mutate()
          }}
        >
          <div className="grid flex-1 gap-1">
            <Label htmlFor="add-members">Add contacts by id</Label>
            <Input
              id="add-members"
              value={ids}
              placeholder="12, 40, 91"
              onChange={(event) => setIds(event.target.value)}
            />
          </div>
          <Button type="submit" disabled={ids.trim() === '' || add.isPending}>
            Add
          </Button>
        </form>
        {add.isError && <ErrorNote label="Could not add the contacts" error={add.error} />}
        {add.isSuccess && (
          <p role="status" className="text-sm text-muted-foreground">
            Added {add.data} contacts.
          </p>
        )}
      </CardContent>
    </Card>
  )
}

function StaticBulkActions({ list, onApplied }: { list: ListOut; onApplied: () => void }) {
  const members = useQuery(membersQuery(list.id, 0, 200))
  if (members.isPending) return <LoadingNote label="Loading members…" />
  if (members.isError) return <ErrorNote label="Could not load the members" error={members.error} />
  if (members.data.items.length === 0) {
    return <EmptyState title="No members to act on" />
  }
  return (
    <div className="space-y-2">
      {members.data.total > members.data.items.length && (
        <Callout tone="info">
          <p>
            A bulk action on a static list takes ids, and this page holds the first{' '}
            {members.data.items.length} of {members.data.total}. Act on the rest from the contacts
            table, which selects by filter.
          </p>
        </Callout>
      )}
      <BulkActionBar
        selection={{ ids: members.data.items.map((item) => item.id) }}
        onApplied={onApplied}
      />
    </div>
  )
}

function MemberTable({ list }: { list: ListOut }) {
  const client = useQueryClient()
  const members = useQuery(membersQuery(list.id))
  const drop = useMutation({
    mutationFn: (contactId: number) => removeMember(list.id, contactId),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['lists'] })
    },
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Members</CardTitle>
      </CardHeader>
      <CardContent>
        {members.isPending && <LoadingNote label="Loading members…" />}
        {members.isError && <ErrorNote label="Could not load the members" error={members.error} />}
        {drop.isError && <ErrorNote label="Could not remove the contact" error={drop.error} />}
        {members.data !== undefined &&
          (members.data.items.length === 0 ? (
            <EmptyState title="Nobody in this list yet" />
          ) : (
            <table className="w-full text-sm">
              <caption className="sr-only">
                {members.data.total} members of {list.name}
              </caption>
              <thead>
                <tr className="border-b text-left text-muted-foreground">
                  <th className="py-1 font-medium">Name</th>
                  <th className="py-1 font-medium">Title</th>
                  <th className="py-1 font-medium">Company</th>
                  <th className="py-1" />
                </tr>
              </thead>
              <tbody>
                {members.data.items.map((member) => (
                  <tr key={member.id} className="border-b last:border-0">
                    <td className="py-1">
                      {member.preferred_name} {member.last_name}
                    </td>
                    <td className="py-1 text-muted-foreground">{member.current_title ?? '—'}</td>
                    <td className="py-1 text-muted-foreground">{member.current_company ?? '—'}</td>
                    <td className="py-1 text-right">
                      {list.kind === 'static' && (
                        <Button
                          variant="ghost"
                          size="xs"
                          aria-label={`Remove ${member.preferred_name} ${member.last_name}`}
                          onClick={() => drop.mutate(member.id)}
                        >
                          Remove
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ))}
      </CardContent>
    </Card>
  )
}
