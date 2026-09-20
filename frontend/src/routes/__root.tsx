import { Outlet, createRootRoute } from '@tanstack/react-router'

import { AppShell } from '@/components/layout/app-shell'
import { EventStreamProvider } from '@/features/events/event-stream-provider'

export const Route = createRootRoute({
  component: RootLayout,
  notFoundComponent: NotFound,
})

function RootLayout() {
  return (
    <EventStreamProvider>
      <AppShell>
        <Outlet />
      </AppShell>
    </EventStreamProvider>
  )
}

function NotFound() {
  return <p className="text-muted-foreground">No such page.</p>
}
