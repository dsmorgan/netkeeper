import { Link } from '@tanstack/react-router'
import { X } from 'lucide-react'
import { useEffect, useRef } from 'react'

import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

import { NAV_ITEMS } from './nav'

interface SidebarProps {
  id: string
  /** Below `sm` only: whether the off-canvas sidebar is showing. From `sm` up it always is. */
  open: boolean
  /** Close the off-canvas sidebar, returning focus to the menu button when `returnFocus`. */
  onClose: (returnFocus: boolean) => void
}

/**
 * The primary navigation. From `sm` up it is a fixed column beside the page;
 * below `sm` it slides in over the page while `open`, and is `invisible` (out
 * of the tab order and the accessibility tree) while closed, so a keyboard
 * never lands on a link nobody can see.
 *
 * Opening it moves focus to its first link; Escape and the close button
 * close it and put focus back on the menu button that opened it. Following a
 * link closes it without moving focus, since the router's navigation does.
 */
export function Sidebar({ id, open, onClose }: SidebarProps) {
  const nav = useRef<HTMLElement>(null)

  useEffect(() => {
    if (!open) return
    nav.current?.querySelector<HTMLElement>('a')?.focus()
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onClose(true)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open, onClose])

  return (
    <aside
      id={id}
      data-open={open}
      className={cn(
        'flex w-48 shrink-0 flex-col border-r bg-sidebar text-sidebar-foreground',
        'max-sm:fixed max-sm:inset-y-0 max-sm:left-0 max-sm:z-40 max-sm:w-64 max-sm:shadow-lg max-sm:transition-transform',
        open ? 'max-sm:translate-x-0' : 'max-sm:invisible max-sm:-translate-x-full',
      )}
    >
      <div className="flex h-11 items-center justify-between border-b px-3 font-semibold tracking-tight">
        netkeeper
        <Button
          variant="ghost"
          size="icon-sm"
          className="sm:hidden"
          aria-label="Close navigation"
          onClick={() => onClose(true)}
        >
          <X aria-hidden="true" />
        </Button>
      </div>
      <nav ref={nav} aria-label="Primary" className="flex flex-col gap-px p-2">
        {NAV_ITEMS.map((item) => (
          <Link
            key={item.to}
            to={item.to}
            activeOptions={{ exact: item.to === '/' }}
            onClick={() => onClose(false)}
            className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground data-[status=active]:bg-sidebar-accent data-[status=active]:font-medium data-[status=active]:text-sidebar-accent-foreground"
          >
            <item.icon className="size-4" aria-hidden="true" />
            {item.label}
          </Link>
        ))}
      </nav>
    </aside>
  )
}
