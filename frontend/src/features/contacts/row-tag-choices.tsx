import { useQuery } from '@tanstack/react-query'

import { MenuCheckboxItem, MenuItem } from '@/components/ui/menu'

import { contactTagsQuery, tagsQuery } from './api'

/**
 * The tag list for one row's "Tag…" submenu.
 *
 * A table row does not carry its tags (`ContactRow` says so), so this asks for
 * them only once the submenu opens — a menu popup mounts its children lazily,
 * so a page of rows costs nothing until you open one.
 */
export function RowTagChoices({
  contactId,
  onToggle,
}: {
  contactId: number
  onToggle: (tagId: number, wanted: boolean) => void
}) {
  const tags = useQuery(tagsQuery)
  const carried = useQuery(contactTagsQuery(contactId))

  if (tags.isPending || carried.isPending) {
    return <MenuItem disabled>Loading tags…</MenuItem>
  }
  if (tags.isError || carried.isError) {
    return <MenuItem disabled>Tags unavailable</MenuItem>
  }
  if (tags.data.length === 0) {
    return <MenuItem disabled>No tags yet</MenuItem>
  }

  const on = new Set(carried.data.map((row) => row.tag_id))
  return (
    <>
      {tags.data.map((tag) => (
        <MenuCheckboxItem
          key={tag.id}
          checked={on.has(tag.id)}
          onCheckedChange={(checked) => onToggle(tag.id, checked === true)}
        >
          {tag.name}
        </MenuCheckboxItem>
      ))}
    </>
  )
}
