import {
  Download,
  FileText,
  Inbox,
  LayoutDashboard,
  List,
  ListChecks,
  Network,
  Send,
  Settings,
  Upload,
  Users,
  type LucideIcon,
} from 'lucide-react'

/** Primary navigation, in the order of the pages table in spec section 14.3. */
export const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/contacts', label: 'Contacts', icon: Users },
  { to: '/triage', label: 'Triage', icon: ListChecks },
  { to: '/lists', label: 'Lists', icon: List },
  { to: '/imports', label: 'Imports', icon: Download },
  { to: '/exports', label: 'Exports', icon: Upload },
  { to: '/linkedin', label: 'LinkedIn', icon: Network },
  { to: '/templates', label: 'Templates', icon: FileText },
  { to: '/campaigns', label: 'Campaigns', icon: Send },
  { to: '/inbox', label: 'Inbox', icon: Inbox },
  { to: '/settings', label: 'Settings', icon: Settings },
] as const satisfies ReadonlyArray<{ to: string; label: string; icon: LucideIcon }>

export type NavItem = (typeof NAV_ITEMS)[number]

/** Label of the nav item that owns `pathname`, for the top bar. */
export function sectionLabel(pathname: string): string {
  const item = NAV_ITEMS.find((candidate) =>
    candidate.to === '/' ? pathname === '/' : pathname.startsWith(candidate.to),
  )
  return item?.label ?? 'netkeeper'
}
