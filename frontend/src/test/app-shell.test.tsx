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
    // Triage is still a placeholder; /contacts is a real page from P1-12 on.
    await renderApp('/triage')

    const nav = screen.getByRole('navigation', { name: 'Primary' })
    expect(within(nav).getByRole('link', { name: 'Triage' })).toHaveAttribute(
      'data-status',
      'active',
    )
    expect(within(nav).getByRole('link', { name: 'Dashboard' })).not.toHaveAttribute(
      'data-status',
      'active',
    )
    expect(await screen.findByText('This page arrives in phase 1.')).toBeInTheDocument()
  })

  it('renders every placeholder route with its phase', async () => {
    const phases: Array<[string, number]> = [
      ['/triage', 1],
      ['/lists', 1],
      ['/imports', 1],
      ['/exports', 1],
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
