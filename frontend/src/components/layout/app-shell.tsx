import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react'

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
 *
 * While it is open it is modal (#364 S2): the top bar and the page are `inert`,
 * so neither a pointer, a screen reader, nor Tab reaches them, and the sidebar
 * keeps Tab inside itself. It only opens below `sm`; widening the window past
 * `sm` closes it, so `inert` never lingers on a layout with no drawer.
 */
const SM_UP = '(min-width: 40rem)'
export function AppShell({ children }: { children: ReactNode }) {
  const [navOpen, setNavOpen] = useState(false)
  const menuButton = useRef<HTMLButtonElement>(null)
  const navId = useId()

  // Focus can only go back to the menu button once it is no longer inert, which
  // is after the render that closes the drawer, so the request waits for it.
  const returnFocus = useRef(false)
  const closeNav = useCallback((andReturnFocus: boolean) => {
    returnFocus.current = andReturnFocus
    setNavOpen(false)
  }, [])

  useEffect(() => {
    if (navOpen || !returnFocus.current) return
    returnFocus.current = false
    menuButton.current?.focus()
  }, [navOpen])

  useEffect(() => {
    if (!navOpen || typeof window.matchMedia !== 'function') return
    const query = window.matchMedia(SM_UP)
    const onChange = (): void => {
      if (query.matches) closeNav(false)
    }
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [navOpen, closeNav])

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
      <div className="flex min-w-0 flex-1 flex-col" inert={navOpen} data-testid="shell-content">
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
