import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { renderApp } from './render'

const EXPECTED_HREFS = [
  '/',
  '/contacts',
  '/triage',
  '/lists',
  '/imports',
  '/exports',
  '/linkedin',
  '/templates',
  '/campaigns',
  '/inbox',
  '/settings',
]

describe('app shell', () => {
  it('renders the eleven navigation links from spec 14.3, in order', async () => {
    await renderApp('/')

    const nav = screen.getByRole('navigation', { name: 'Primary' })
    const links = within(nav).getAllByRole('link')

    expect(links).toHaveLength(11)
    expect(links.map((link) => link.getAttribute('href'))).toEqual(EXPECTED_HREFS)
  })

  it('marks the current section active', async () => {
    // Every page has landed: /inbox, in P3-11b, was the last placeholder.
    await renderApp('/inbox')

    const nav = screen.getByRole('navigation', { name: 'Primary' })
    expect(within(nav).getByRole('link', { name: 'Inbox' })).toHaveAttribute(
      'data-status',
      'active',
    )
    expect(within(nav).getByRole('link', { name: 'Dashboard' })).not.toHaveAttribute(
      'data-status',
      'active',
    )
  })

  describe('below sm, the sidebar is off-canvas (#181)', () => {
    it('opens from the menu button and moves focus to the first link', async () => {
      await renderApp('/')
      const menu = screen.getByRole('button', { name: 'Open navigation' })
      const sidebar = document.getElementById(menu.getAttribute('aria-controls') ?? '')

      expect(menu).toHaveAttribute('aria-expanded', 'false')
      expect(sidebar).toHaveAttribute('data-open', 'false')
      expect(sidebar).toHaveClass('max-sm:invisible')

      fireEvent.click(menu)

      expect(menu).toHaveAttribute('aria-expanded', 'true')
      expect(sidebar).toHaveAttribute('data-open', 'true')
      expect(sidebar).not.toHaveClass('max-sm:invisible')
      const nav = screen.getByRole('navigation', { name: 'Primary' })
      expect(within(nav).getByRole('link', { name: 'Dashboard' })).toHaveFocus()
    })

    it('closes on Escape and returns focus to the menu button', async () => {
      await renderApp('/')
      const menu = screen.getByRole('button', { name: 'Open navigation' })
      fireEvent.click(menu)

      fireEvent.keyDown(document, { key: 'Escape' })

      expect(menu).toHaveAttribute('aria-expanded', 'false')
      expect(menu).toHaveFocus()
    })

    it('closes from its close button and from the backdrop', async () => {
      await renderApp('/')
      const menu = screen.getByRole('button', { name: 'Open navigation' })

      fireEvent.click(menu)
      fireEvent.click(screen.getByRole('button', { name: 'Close navigation' }))
      expect(menu).toHaveAttribute('aria-expanded', 'false')
      expect(menu).toHaveFocus()

      fireEvent.click(menu)
      fireEvent.click(screen.getByTestId('nav-backdrop'))
      expect(menu).toHaveAttribute('aria-expanded', 'false')
      expect(screen.queryByTestId('nav-backdrop')).not.toBeInTheDocument()
    })

    it('closes once a link is followed', async () => {
      const { router } = await renderApp('/')
      const menu = screen.getByRole('button', { name: 'Open navigation' })
      fireEvent.click(menu)

      const nav = screen.getByRole('navigation', { name: 'Primary' })
      fireEvent.click(within(nav).getByRole('link', { name: 'Settings' }))

      expect(menu).toHaveAttribute('aria-expanded', 'false')
      await waitFor(() => expect(router.state.location.pathname).toBe('/settings'))
    })

    it('ignores Escape while closed', async () => {
      await renderApp('/')
      const menu = screen.getByRole('button', { name: 'Open navigation' })

      fireEvent.keyDown(document, { key: 'Escape' })

      expect(menu).toHaveAttribute('aria-expanded', 'false')
      expect(menu).not.toHaveFocus()
    })
  })
})
