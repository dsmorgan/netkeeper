import { screen, within } from '@testing-library/react'
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

  it('marks the current section active and shows its placeholder', async () => {
    // A route that is still a placeholder. Every phase-1 screen has landed, so
    // this now walks into phase 2; move it on again when /linkedin is real.
    await renderApp('/linkedin')

    const nav = screen.getByRole('navigation', { name: 'Primary' })
    expect(within(nav).getByRole('link', { name: 'LinkedIn' })).toHaveAttribute(
      'data-status',
      'active',
    )
    expect(within(nav).getByRole('link', { name: 'Dashboard' })).not.toHaveAttribute(
      'data-status',
      'active',
    )
    expect(await screen.findByText('This page arrives in phase 2.')).toBeInTheDocument()
  })

  it('renders every placeholder route with its phase', async () => {
    // A route drops off this list when its own page lands: /contacts in P1-12,
    // /triage in P1-14, /lists and /exports in P1-15, /imports in P1-13. Each
    // lane removes its own entry, so keep every deletion when this conflicts.
    const phases: Array<[string, number]> = [
      ['/linkedin', 2],
      ['/templates', 3],
      ['/campaigns', 3],
      ['/inbox', 3],
    ]
    for (const [path, phase] of phases) {
      const { unmount } = await renderApp(path)
      expect(await screen.findByText(`This page arrives in phase ${phase}.`)).toBeInTheDocument()
      unmount()
    }
  })
})
