import { Link } from '@tanstack/react-router'

import { NAV_ITEMS } from './nav'

export function Sidebar() {
  return (
    <aside className="flex w-48 shrink-0 flex-col border-r bg-sidebar text-sidebar-foreground">
      <div className="flex h-11 items-center border-b px-3 font-semibold tracking-tight">
        netkeeper
      </div>
      <nav aria-label="Primary" className="flex flex-col gap-px p-2">
        {NAV_ITEMS.map((item) => (
          <Link
            key={item.to}
            to={item.to}
            activeOptions={{ exact: item.to === '/' }}
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
