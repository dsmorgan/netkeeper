import type { ReactNode } from 'react'

import { Sidebar } from './sidebar'
import { TopBar } from './top-bar'

/** Sidebar, top bar, content. Every page renders inside `children`. */
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        <main className="flex-1 p-4">{children}</main>
      </div>
    </div>
  )
}
