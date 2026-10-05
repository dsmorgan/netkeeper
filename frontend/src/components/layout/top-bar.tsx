import { useRouterState } from '@tanstack/react-router'
import { Menu } from 'lucide-react'
import type { RefObject } from 'react'

import { Button } from '@/components/ui/button'
import { PollStatusHeader } from '@/features/poll-status/poll-status-header'

import { BackendHealth } from './backend-health'
import { sectionLabel } from './nav'

interface TopBarProps {
  menuButtonRef: RefObject<HTMLButtonElement | null>
  navId: string
  navOpen: boolean
  onToggleNav: () => void
}

export function TopBar({ menuButtonRef, navId, navOpen, onToggleNav }: TopBarProps) {
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  return (
    <header className="flex h-11 items-center justify-between gap-2 border-b px-4">
      <div className="flex min-w-0 items-center gap-2">
        {/* Below `sm` only: from `sm` up the sidebar is always in place. */}
        <Button
          ref={menuButtonRef}
          variant="ghost"
          size="icon-sm"
          className="-ml-2 sm:hidden"
          aria-label="Open navigation"
          aria-expanded={navOpen}
          aria-controls={navId}
          onClick={onToggleNav}
        >
          <Menu aria-hidden="true" />
        </Button>
        <h1 className="truncate font-medium">{sectionLabel(pathname)}</h1>
      </div>
      <div className="flex min-w-0 items-center gap-3">
        <PollStatusHeader />
        <BackendHealth />
      </div>
    </header>
  )
}
