import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { ConfirmDialog } from './confirm-dialog'

/** A minimal caller: opens the dialog, tracks confirm calls, never actually resolves. */
function Harness({ onConfirm }: { onConfirm: () => void }) {
  const [open, setOpen] = useState(true)
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={setOpen}
      title="Do the thing?"
      confirmLabel="Do it"
      onConfirm={onConfirm}
    >
      <p>This does the thing.</p>
    </ConfirmDialog>
  )
}

describe('ConfirmDialog', () => {
  it('shows the confirm label at rest', () => {
    render(<Harness onConfirm={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Do it' })).toBeInTheDocument()
  })

  it('never shows a caller-built pending label, only "Working…"', () => {
    const [open, onOpenChange] = [true, vi.fn()]
    render(
      <ConfirmDialog
        open={open}
        onOpenChange={onOpenChange}
        title="Do the thing?"
        confirmLabel="Do it"
        onConfirm={vi.fn()}
        pending
      >
        <p>This does the thing.</p>
      </ConfirmDialog>,
    )
    expect(screen.getByRole('button', { name: 'Working…' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Do it' })).not.toBeInTheDocument()
  })

  it('drops a second same-tick click: only one onConfirm (L2)', () => {
    const onConfirm = vi.fn()
    render(<Harness onConfirm={onConfirm} />)
    const button = screen.getByRole('button', { name: 'Do it' })

    // Two clicks before any re-render could ever flip `pending` — the ref
    // guard, not the `disabled` prop, is what has to catch this.
    fireEvent.click(button)
    fireEvent.click(button)

    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('allows a fresh confirm after pending clears', () => {
    const onConfirm = vi.fn()
    function ControlledHarness() {
      const [open, setOpen] = useState(true)
      const [pending, setPending] = useState(false)
      return (
        <ConfirmDialog
          open={open}
          onOpenChange={setOpen}
          title="Do the thing?"
          confirmLabel="Do it"
          pending={pending}
          onConfirm={() => {
            onConfirm()
            setPending(true)
          }}
        >
          <p>This does the thing.</p>
          <button type="button" onClick={() => setPending(false)}>
            simulate settle
          </button>
        </ConfirmDialog>
      )
    }
    render(<ControlledHarness />)

    fireEvent.click(screen.getByRole('button', { name: 'Do it' }))
    expect(onConfirm).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'simulate settle' }))
    fireEvent.click(screen.getByRole('button', { name: 'Do it' }))
    expect(onConfirm).toHaveBeenCalledTimes(2)
  })
})
