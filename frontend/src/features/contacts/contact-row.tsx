import { ExternalLink, Mail, MoreHorizontal } from 'lucide-react'
import { memo } from 'react'

import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Menu,
  MenuContent,
  MenuGroupLabel,
  MenuItem,
  MenuLinkItem,
  MenuSeparator,
  MenuSubmenu,
  MenuSubmenuTrigger,
  MenuTrigger,
} from '@/components/ui/menu'
import { TableCell, TableRow } from '@/components/ui/table'

import type { ColumnSpec } from './columns'
import { displayName, gmailSearchUrl } from './format'
import { rowRenders } from './instrumentation'
import { RowListChoices } from './row-list-choices'
import { RowTagChoices } from './row-tag-choices'
import type { ContactMet, ContactRow as Row } from './types'
import { MET_LABELS, MET_VALUES } from './types'

/**
 * What a row action does, lifted to the table so every row shares one stable
 * object and `memo` below actually holds.
 */
export interface RowActions {
  setMet: (row: Row, met: ContactMet) => void
  setDoNotContact: (row: Row, value: boolean) => void
  setArchived: (row: Row, archived: boolean) => void
  toggleTag: (row: Row, tagId: number, wanted: boolean) => void
  addToList: (row: Row, listId: number) => void
}

export interface ContactRowProps {
  row: Row
  columns: readonly ColumnSpec[]
  selected: boolean
  onSelect: (id: number, selected: boolean) => void
  actions: RowActions
  /** Fixed, so the windowed body can do arithmetic instead of measuring. */
  height: number
}

/**
 * One row.
 *
 * Memoized on its props, all of which are primitives or objects the table keeps
 * stable, so a filter keystroke, a selection change on another row, or a scroll
 * re-renders nothing here.
 */
export const ContactTableRow = memo(function ContactTableRow({
  row,
  columns,
  selected,
  onSelect,
  actions,
  height,
}: ContactRowProps) {
  rowRenders.count += 1
  const name = displayName(row)
  const archived = Boolean(row.archived_at)

  return (
    <TableRow data-selected={selected || undefined} style={{ height }}>
      <TableCell className="w-8 pl-3">
        <Checkbox
          checked={selected}
          onCheckedChange={(checked) => onSelect(row.id, checked === true)}
          aria-label={`Select ${name}`}
        />
      </TableCell>
      {columns.map((column) => (
        <TableCell key={column.id} className={column.wide ? 'max-w-64 truncate' : undefined}>
          {column.render(row)}
        </TableCell>
      ))}
      <TableCell className="w-10 pr-3 text-right">
        <Menu>
          <MenuTrigger
            render={
              <Button variant="ghost" size="icon-xs" aria-label={`Actions for ${name}`}>
                <MoreHorizontal />
              </Button>
            }
          />
          <MenuContent>
            <MenuGroupLabel>{name}</MenuGroupLabel>
            <MenuSubmenu>
              <MenuSubmenuTrigger>Tag…</MenuSubmenuTrigger>
              <MenuContent align="start" side="inline-end">
                <RowTagChoices
                  contactId={row.id}
                  onToggle={(tagId, wanted) => actions.toggleTag(row, tagId, wanted)}
                />
              </MenuContent>
            </MenuSubmenu>
            <MenuSubmenu>
              <MenuSubmenuTrigger>Add to list…</MenuSubmenuTrigger>
              <MenuContent align="start" side="inline-end">
                <RowListChoices onAdd={(listId) => actions.addToList(row, listId)} />
              </MenuContent>
            </MenuSubmenu>
            <MenuSubmenu>
              <MenuSubmenuTrigger>Set met</MenuSubmenuTrigger>
              <MenuContent align="start" side="inline-end">
                {MET_VALUES.map((value) => (
                  <MenuItem key={value} onClick={() => actions.setMet(row, value)}>
                    {MET_LABELS[value]}
                    {row.met === value ? (
                      <span className="ml-auto text-xs text-muted-foreground">current</span>
                    ) : null}
                  </MenuItem>
                ))}
              </MenuContent>
            </MenuSubmenu>
            <MenuItem onClick={() => actions.setDoNotContact(row, !row.do_not_contact)}>
              {row.do_not_contact ? 'Allow contact again' : 'Set do not contact'}
            </MenuItem>
            <MenuItem
              disabled
              title="Enrichment pins arrive with the LinkedIn API; there is no /linkedin/pins yet."
              className="justify-between gap-6"
            >
              Pin for enrichment
              <span className="text-xs text-muted-foreground">not built yet</span>
            </MenuItem>
            <MenuSeparator />
            <MenuLinkItem href={row.li_url}>
              <ExternalLink />
              Open on LinkedIn
            </MenuLinkItem>
            <MenuLinkItem href={gmailSearchUrl(row.primary_email)}>
              <Mail />
              Open in Gmail
            </MenuLinkItem>
            <MenuSeparator />
            <MenuItem onClick={() => actions.setArchived(row, !archived)}>
              {archived ? 'Unarchive' : 'Archive'}
            </MenuItem>
          </MenuContent>
        </Menu>
      </TableCell>
    </TableRow>
  )
})
