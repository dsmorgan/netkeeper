import { Columns3 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import {
  Menu,
  MenuCheckboxItem,
  MenuContent,
  MenuGroupLabel,
  MenuItem,
  MenuSeparator,
  MenuTrigger,
} from '@/components/ui/menu'

import type { ColumnId } from './columns'
import { COLUMNS, DEFAULT_COLUMNS } from './columns'

/**
 * Which columns the table shows (spec 10.1).
 *
 * The choice is kept in local storage by the caller, so it survives a reload
 * and every other view; a saved view carries its own copy.
 */
export function ColumnPicker({
  columns,
  onChange,
}: {
  columns: readonly ColumnId[]
  onChange: (ids: readonly ColumnId[]) => void
}) {
  const chosen = new Set(columns)

  function toggle(id: ColumnId, wanted: boolean) {
    // Ordered by the registry, so columns never shuffle as they are turned on.
    const next = COLUMNS.filter((column) =>
      column.id === id ? wanted : chosen.has(column.id),
    ).map((column) => column.id)
    onChange(next)
  }

  return (
    <Menu>
      <MenuTrigger
        render={
          <Button variant="outline" size="sm">
            <Columns3 data-icon="inline-start" />
            Columns
            <span className="text-muted-foreground">{columns.length}</span>
          </Button>
        }
      />
      <MenuContent className="max-h-96">
        <MenuGroupLabel>Columns</MenuGroupLabel>
        {COLUMNS.map((column) => (
          <MenuCheckboxItem
            key={column.id}
            checked={chosen.has(column.id)}
            // The last column cannot be turned off: a table of nothing is not a view.
            disabled={chosen.has(column.id) && columns.length === 1}
            onCheckedChange={(checked) => toggle(column.id, checked === true)}
          >
            {column.label}
          </MenuCheckboxItem>
        ))}
        <MenuSeparator />
        <MenuItem onClick={() => onChange(DEFAULT_COLUMNS)}>Reset to default</MenuItem>
      </MenuContent>
    </Menu>
  )
}
