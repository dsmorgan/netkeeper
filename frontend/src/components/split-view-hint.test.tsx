import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { SPLIT_VIEW_HELP_URL, SplitViewHint } from './split-view-hint'

describe('SplitViewHint', () => {
  it('gives the Chrome steps and a fallback for other browsers', () => {
    render(<SplitViewHint />)
    expect(screen.getByText(/Open link in split view/)).toBeInTheDocument()
    expect(screen.getByText(/New split view with current tab/)).toBeInTheDocument()
    expect(screen.getByText(/Separate split view/)).toBeInTheDocument()
    expect(
      screen.getByText(/drag the linked page’s tab out into its own window/),
    ).toBeInTheDocument()
  })

  it('opens Google’s help page in a new tab', () => {
    render(<SplitViewHint />)
    const help = screen.getByRole('link', { name: /split view help/ })
    expect(help).toHaveAttribute('href', SPLIT_VIEW_HELP_URL)
    expect(help).toHaveAttribute('target', '_blank')
    expect(help).toHaveAttribute('rel', 'noopener noreferrer')
  })
})
