import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useRef, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { FilterSummary, type FilterChip } from './filter-summary'

/** A list with a search box and two filters, wired the way a page wires the summary. */
function Harness({ onClear = () => undefined }: { onClear?: () => void }) {
  const [q, setQ] = useState('acme')
  const [status, setStatus] = useState('replied')
  const box = useRef<HTMLInputElement>(null)
  const chips: FilterChip[] = []
  if (q !== '') chips.push({ key: 'q', label: '“acme”', onRemove: () => setQ('') })
  if (status !== '') {
    chips.push({ key: 'status', label: `status: ${status}`, onRemove: () => setStatus('') })
  }
  return (
    <>
      <input ref={box} aria-label="Search" />
      <FilterSummary
        shown={chips.length === 2 ? 12 : 40}
        total={87}
        chips={chips}
        onClear={() => {
          onClear()
          setQ('')
          setStatus('')
        }}
        returnFocusTo={box}
      />
    </>
  )
}

describe('FilterSummary', () => {
  it('says how many of how many, and what narrows the list', () => {
    render(<Harness />)
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('Showing 12 of 87 · filtered by:')
    // A screen reader hears the filters with the count, not only the count.
    expect(status).toHaveTextContent('“acme”, status: replied')
    expect(status).toHaveAttribute('aria-live', 'polite')
    const chips = within(screen.getByRole('list', { name: 'Active filters' }))
    expect(chips.getAllByRole('button').map((chip) => chip.textContent)).toEqual([
      '“acme”',
      'status: replied',
    ])
  })

  it('removes one filter per chip, with a name that says so, and moves the focus on', async () => {
    render(<Harness />)
    const chip = screen.getByRole('button', { name: 'Remove “acme”' })
    // A native button: Tab reaches it, and Enter or Space removes it.
    expect(chip.tagName).toBe('BUTTON')
    chip.focus()
    fireEvent.click(chip)

    expect(screen.getByRole('status')).toHaveTextContent('Showing 40 of 87')
    expect(screen.queryByRole('button', { name: 'Remove “acme”' })).toBeNull()
    // The focus lands on the chip that took its place, not on the page body.
    const next = screen.getByRole('button', { name: 'Remove status: replied' })
    await waitFor(() => expect(next).toHaveFocus())

    // The last one gone, it goes back to the search box.
    fireEvent.click(next)
    await waitFor(() => expect(screen.getByLabelText('Search')).toHaveFocus())
  })

  it('clears every filter at once and hides itself, keeping the live region', async () => {
    const onClear = vi.fn()
    render(<Harness onClear={onClear} />)
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))

    expect(onClear).toHaveBeenCalledOnce()
    expect(screen.queryByRole('list', { name: 'Active filters' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Clear' })).toBeNull()
    // Still there, and empty, so the next filter's count is announced.
    expect(screen.getByRole('status')).toHaveTextContent('')
    await waitFor(() => expect(screen.getByLabelText('Search')).toHaveFocus())
  })

  it('says only how many match while the whole is unknown', () => {
    render(
      <FilterSummary
        shown={3}
        total={undefined}
        chips={[{ key: 'k', label: 'kind: bounce', onRemove: () => undefined }]}
        onClear={() => undefined}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent(/^Showing 3 · filtered by:/)
  })
})
