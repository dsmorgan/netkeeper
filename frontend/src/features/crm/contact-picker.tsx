/**
 * Find contacts by name and pick several (#336).
 *
 * Replaces the "Add contacts by id" box, which asked for ids the UI never
 * shows. The search covers first, last and preferred name plus company, the
 * same word-by-word match as Triage's "Jump to a contact". Archived and
 * merged-away contacts never come back (the query excludes both). A contact
 * already in the list shows, disabled and labeled "In list", so a search for a
 * name you know tells you it is there instead of looking empty.
 */
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { api } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { nameSearchFilter, NAME_FIELDS } from '@/features/contacts/name-search'
import type { FilterNode } from '@/features/contacts/types'

import { ErrorNote } from './controls'

/** How many matches the picker lists. */
const PICKER_LIMIT = 20

const SEARCH_FIELDS = [...NAME_FIELDS, 'current_company'] as const

export interface PickerContact {
  id: number
  name: string
  company: string | null
  title: string | null
}

async function searchContacts(
  text: string,
  listId: number | null,
  signal: AbortSignal,
): Promise<PickerMatches> {
  const names = nameSearchFilter(text, SEARCH_FIELDS)
  if (names === null) return { items: [], total: 0 }
  const where: FilterNode =
    listId === null
      ? names
      : { op: 'and', children: [{ op: 'list_member', list_id: listId }, names] }
  const { data, error, response } = await api.POST('/api/v1/contacts/query', {
    body: {
      filter: { include_archived: false, where },
      sort: [],
      limit: PICKER_LIMIT,
      offset: 0,
      columns: ['first_name', 'last_name', 'preferred_name', 'current_company', 'current_title'],
    },
    signal,
  })
  if (data === undefined) {
    throw new Error(
      `the search could not be run (${response.status}${error === undefined ? '' : `: ${JSON.stringify(error)}`})`,
    )
  }
  return {
    total: data.total,
    items: data.items.map((row) => ({
      id: row.id,
      name: displayName(row),
      company: row.current_company ?? null,
      title: row.current_title ?? null,
    })),
  }
}

interface PickerMatches {
  items: PickerContact[]
  total: number
}

/** "Cy (Cyrus) Dunn" when the preferred name differs from the first; an empty one is missing. */
function displayName(row: {
  first_name?: string | null
  last_name?: string | null
  preferred_name?: string | null
}): string {
  const first = row.first_name ?? ''
  const preferred = row.preferred_name === '' ? null : (row.preferred_name ?? null)
  const given =
    preferred !== null && first !== '' && preferred !== first
      ? `${preferred} (${first})`
      : (preferred ?? first)
  return `${given} ${row.last_name ?? ''}`.trim()
}

export function ContactPicker({
  listId,
  adding,
  onAdd,
}: {
  listId: number
  adding: boolean
  /** Resolves once the add succeeded; a rejection keeps the picks. */
  onAdd: (contactIds: number[]) => Promise<unknown>
}) {
  const [text, setText] = useState('')
  const [picked, setPicked] = useState<Map<number, string>>(new Map())
  const trimmed = text.trim()
  const matches = useQuery({
    queryKey: ['lists', listId, 'picker', 'matches', trimmed],
    queryFn: ({ signal }) => searchContacts(trimmed, null, signal),
    enabled: trimmed !== '',
    gcTime: 0,
  })
  // Which of those matches the list already holds, asked of the server so it
  // stays right however large the list is.
  const inList = useQuery({
    queryKey: ['lists', listId, 'picker', 'members', trimmed],
    queryFn: ({ signal }) => searchContacts(trimmed, listId, signal),
    enabled: trimmed !== '',
    gcTime: 0,
  })
  const memberIds = new Set((inList.data?.items ?? []).map((row) => row.id))

  function toggle(contact: PickerContact, on: boolean) {
    setPicked((current) => {
      const next = new Map(current)
      if (on) next.set(contact.id, contact.name)
      else next.delete(contact.id)
      return next
    })
  }

  return (
    <div className="space-y-2">
      <div className="grid gap-1">
        <Label htmlFor="add-members-search">Add contacts</Label>
        <Input
          id="add-members-search"
          type="search"
          value={text}
          placeholder="Search by name or company"
          onChange={(event) => setText(event.target.value)}
        />
      </div>
      {trimmed !== '' && matches.isError && (
        <ErrorNote label="The search failed" error={matches.error} />
      )}
      {trimmed !== '' && matches.isSuccess && matches.data.items.length === 0 && (
        <p className="text-sm text-muted-foreground">Nobody matches “{trimmed}”.</p>
      )}
      {matches.isSuccess && matches.data.items.length > 0 && (
        <ul aria-label="Matches" className="divide-y rounded-lg border text-sm">
          {matches.data.items.map((row) => {
            const member = memberIds.has(row.id)
            const detail = [row.title, row.company].filter((part) => part !== null).join(' · ')
            return (
              <li key={row.id}>
                <label
                  className={`flex items-center gap-2 px-3 py-1.5 ${member ? 'opacity-60' : 'cursor-pointer hover:bg-muted'}`}
                >
                  <input
                    type="checkbox"
                    disabled={member}
                    checked={member || picked.has(row.id)}
                    onChange={(event) => toggle(row, event.target.checked)}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{row.name}</span>
                    {detail !== '' && (
                      <span className="block truncate text-xs text-muted-foreground">{detail}</span>
                    )}
                  </span>
                  {member && <span className="text-xs text-muted-foreground">In list</span>}
                </label>
              </li>
            )
          })}
        </ul>
      )}
      {matches.isSuccess && matches.data.total > PICKER_LIMIT && (
        <p className="text-xs text-muted-foreground">
          Showing {PICKER_LIMIT} of {matches.data.total}. Type more of the name to narrow it.
        </p>
      )}
      {inList.isError && (
        <p role="alert" className="text-xs text-destructive">
          Could not check which of these are already in the list.
        </p>
      )}
      <div className="flex items-center gap-2">
        <Button
          type="button"
          disabled={picked.size === 0 || adding}
          onClick={() => {
            // The picks stay until the add succeeds; the caller shows a failure.
            onAdd([...picked.keys()]).then(
              () => setPicked(new Map()),
              () => {},
            )
          }}
        >
          Add
        </Button>
        {picked.size > 0 && (
          <span className="text-sm text-muted-foreground">{picked.size} picked</span>
        )}
      </div>
    </div>
  )
}
