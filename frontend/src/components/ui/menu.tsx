'use client'

import { Menu as MenuPrimitive } from '@base-ui/react/menu'
import { Check } from 'lucide-react'
import * as React from 'react'
import { cn } from 'cn'

const POPUP_CLASS =
  'z-50 max-h-[min(24rem,var(--available-height))] min-w-40 origin-(--transform-origin) overflow-y-auto rounded-lg bg-popover p-1 text-sm text-popover-foreground ring-1 ring-foreground/10 shadow-lg outline-none data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0'

const ITEM_CLASS =
  'relative flex cursor-default items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-none select-none data-highlighted:bg-muted data-highlighted:text-foreground data-disabled:pointer-events-none data-disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*=size-])]:size-4'

/**
 * Groups a trigger and its popup. `modal` is off by default: the Contacts table
 * keeps its bulk bar and row underneath reachable while a row menu is open, and
 * a non-modal menu leaves the rest of the page queryable in tests.
 */
function Menu({ modal = false, ...props }: MenuPrimitive.Root.Props) {
  return <MenuPrimitive.Root modal={modal} {...props} />
}

function MenuTrigger(props: MenuPrimitive.Trigger.Props) {
  return <MenuPrimitive.Trigger data-slot="menu-trigger" {...props} />
}

function MenuContent({
  className,
  align = 'end',
  side = 'bottom',
  sideOffset = 4,
  ...props
}: MenuPrimitive.Popup.Props &
  Pick<MenuPrimitive.Positioner.Props, 'align' | 'side' | 'sideOffset'>) {
  return (
    <MenuPrimitive.Portal>
      <MenuPrimitive.Positioner
        align={align}
        side={side}
        sideOffset={sideOffset}
        className="isolate z-50"
      >
        <MenuPrimitive.Popup
          data-slot="menu-content"
          className={cn(POPUP_CLASS, className)}
          {...props}
        />
      </MenuPrimitive.Positioner>
    </MenuPrimitive.Portal>
  )
}

function MenuItem({ className, ...props }: MenuPrimitive.Item.Props) {
  return (
    <MenuPrimitive.Item data-slot="menu-item" className={cn(ITEM_CLASS, className)} {...props} />
  )
}

/**
 * An item that opens a URL. Given no `href` it renders a disabled item: an
 * action the data does not support is visibly unavailable, never a dead click.
 */
function MenuLinkItem({
  className,
  href,
  children,
  ...props
}: Omit<MenuPrimitive.LinkItem.Props, 'href'> & { href: string | null | undefined }) {
  if (!href) {
    return (
      <MenuItem disabled className={className} {...(props as MenuPrimitive.Item.Props)}>
        {children}
      </MenuItem>
    )
  }
  return (
    <MenuPrimitive.LinkItem
      data-slot="menu-link-item"
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      closeOnClick
      className={cn(ITEM_CLASS, className)}
      {...props}
    >
      {children}
    </MenuPrimitive.LinkItem>
  )
}

function MenuCheckboxItem({ className, children, ...props }: MenuPrimitive.CheckboxItem.Props) {
  return (
    <MenuPrimitive.CheckboxItem
      data-slot="menu-checkbox-item"
      closeOnClick={false}
      className={cn(ITEM_CLASS, 'pl-7', className)}
      {...props}
    >
      <MenuPrimitive.CheckboxItemIndicator className="absolute left-2 flex items-center justify-center">
        <Check className="size-3.5" />
      </MenuPrimitive.CheckboxItemIndicator>
      {children}
    </MenuPrimitive.CheckboxItem>
  )
}

function MenuGroup(props: MenuPrimitive.Group.Props) {
  return <MenuPrimitive.Group data-slot="menu-group" {...props} />
}

/**
 * A heading above a run of items. A plain element on purpose: Base UI's
 * `Menu.GroupLabel` names a `Menu.Group`, and these menus label a whole popup
 * rather than a group inside one.
 */
function MenuGroupLabel({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      data-slot="menu-group-label"
      className={cn('px-2 py-1.5 text-xs font-medium text-muted-foreground', className)}
      {...props}
    />
  )
}

function MenuSeparator({ className, ...props }: MenuPrimitive.Separator.Props) {
  return (
    <MenuPrimitive.Separator
      data-slot="menu-separator"
      className={cn('-mx-1 my-1 h-px bg-border', className)}
      {...props}
    />
  )
}

function MenuSubmenu(props: MenuPrimitive.SubmenuRoot.Props) {
  return <MenuPrimitive.SubmenuRoot {...props} />
}

function MenuSubmenuTrigger({ className, ...props }: MenuPrimitive.SubmenuTrigger.Props) {
  return (
    <MenuPrimitive.SubmenuTrigger
      data-slot="menu-submenu-trigger"
      className={cn(ITEM_CLASS, 'justify-between', className)}
      {...props}
    />
  )
}

export {
  Menu,
  MenuCheckboxItem,
  MenuContent,
  MenuGroup,
  MenuGroupLabel,
  MenuItem,
  MenuLinkItem,
  MenuSeparator,
  MenuSubmenu,
  MenuSubmenuTrigger,
  MenuTrigger,
}
