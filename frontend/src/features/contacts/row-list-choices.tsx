import { useQuery } from '@tanstack/react-query'

import { MenuItem } from '@/components/ui/menu'

import { listsQuery } from './api'

/**
 * The lists one row's "Add to list…" submenu offers (spec 10.1, 10.4).
 *
 * Only a static list takes members: a smart list's membership *is* its filter,
 * and the API answers `422` for one, so it is shown unavailable with that as
 * the reason rather than offered and refused. Like the tag submenu, this loads
 * only when the submenu opens.
 */
export function RowListChoices({ onAdd }: { onAdd: (listId: number) => void }) {
  const lists = useQuery(listsQuery)

  if (lists.isPending) return <MenuItem disabled>Loading lists…</MenuItem>
  if (lists.isError) return <MenuItem disabled>Lists unavailable</MenuItem>
  if (lists.data.length === 0) return <MenuItem disabled>No lists yet</MenuItem>

  return (
    <>
      {lists.data.map((list) =>
        list.kind === 'static' ? (
          <MenuItem key={list.id} onClick={() => onAdd(list.id)} className="justify-between gap-6">
            {list.name}
            <span className="text-xs text-muted-foreground">{list.member_count}</span>
          </MenuItem>
        ) : (
          <MenuItem
            key={list.id}
            disabled
            title="A smart list has no members to add: its filter decides who is on it."
            className="justify-between gap-6"
          >
            {list.name}
            <span className="text-xs text-muted-foreground">smart</span>
          </MenuItem>
        ),
      )}
    </>
  )
}
