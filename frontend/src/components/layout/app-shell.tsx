import { useCallback, useId, useRef, useState, type ReactNode } from 'react'

import { ReauthBanner } from '@/features/mailboxes/reauth-banner'

import { Sidebar } from './sidebar'
import { TopBar } from './top-bar'

/**
 * Sidebar, top bar, content. Every page renders inside `children`.
 *
 * From `sm` up the sidebar is always in place. Below `sm` it would leave a
 * 390px phone a content column only about 166px wide (#181), so there it is
 * off-canvas instead: the top bar's menu button opens it over the page, and
 * Escape, the close button, the backdrop, or following a link closes it.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const [navOpen, setNavOpen] = useState(false)
  const menuButton = useRef<HTMLButtonElement>(null)
  const navId = useId()

  const closeNav = useCallback((returnFocus: boolean) => {
    setNavOpen(false)
    if (returnFocus) menuButton.current?.focus()
  }, [])

  return (
    <div className="flex min-h-screen">
      {navOpen && (
        <div
          aria-hidden="true"
          data-testid="nav-backdrop"
          className="fixed inset-0 z-30 bg-black/40 sm:hidden"
          onClick={() => closeNav(true)}
        />
      )}
      <Sidebar id={navId} open={navOpen} onClose={closeNav} />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar
          menuButtonRef={menuButton}
          navId={navId}
          navOpen={navOpen}
          onToggleNav={() => setNavOpen((open) => !open)}
        />
        <main className="min-w-0 flex-1 p-4">
          <ReauthBanner />
          {children}
        </main>
      </div>
    </div>
  )
}
