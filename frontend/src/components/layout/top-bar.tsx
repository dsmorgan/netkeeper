import { useRouterState } from '@tanstack/react-router'

import { BackendHealth } from './backend-health'
import { sectionLabel } from './nav'

export function TopBar() {
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  return (
    <header className="flex h-11 items-center justify-between border-b px-4">
      <h1 className="font-medium">{sectionLabel(pathname)}</h1>
      <BackendHealth />
    </header>
  )
}
