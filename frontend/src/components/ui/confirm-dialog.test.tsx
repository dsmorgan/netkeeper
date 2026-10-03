import { act, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { ConfirmDialog } from './confirm-dialog'

/** A minimal caller: opens the dialog, tracks confirm calls, never actually resolves. */
function Harness({ onConfirm }: { onConfirm: () => Promise<unknown> | void }) {
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

  it('resets after an instant failure, even if pending never renders true (instant 409)', () => {
    const onConfirm = vi.fn()
    function ControlledHarness() {
      const [open, setOpen] = useState(true)
      const [error, setError] = useState<string | null>(null)
      return (
        <ConfirmDialog
          open={open}
          onOpenChange={setOpen}
          title="Do the thing?"
          confirmLabel="Do it"
          error={error}
          onConfirm={() => {
            onConfirm()
            // Settles with an error before React ever commits a render with
            // `pending` true — a fast jsdom mock, or a fast real backend, can
            // both do this. `pending` (never passed here) stays `false` the
            // whole time.
            setError('run 3 is still running (409)')
          }}
        >
          <p>This does the thing.</p>
        </ConfirmDialog>
      )
    }
    render(<ControlledHarness />)
    const button = screen.getByRole('button', { name: 'Do it' })

    fireEvent.click(button)
    expect(onConfirm).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('alert')).toHaveTextContent('run 3 is still running (409)')

    // Retry: `pending` was never observed true, so only a stuck ref guard
    // could still be blocking this.
    fireEvent.click(button)
    expect(onConfirm).toHaveBeenCalledTimes(2)
  })

  it('resets after the dialog closes, even if pending never renders true (arm/disarm/arm cycle)', () => {
    const onConfirm = vi.fn()
    function ControlledHarness() {
      const [open, setOpen] = useState(true)
      return (
        <>
          <ConfirmDialog
            open={open}
            onOpenChange={setOpen}
            title="Do the thing?"
            confirmLabel="Do it"
            onConfirm={() => {
              onConfirm()
              // Success settles instantly too: the dialog closes without
              // `pending` ever being observed true.
              setOpen(false)
            }}
          >
            <p>This does the thing.</p>
          </ConfirmDialog>
          {!open && (
            <button type="button" onClick={() => setOpen(true)}>
              reopen
            </button>
          )}
        </>
      )
    }
    render(<ControlledHarness />)

    fireEvent.click(screen.getByRole('button', { name: 'Do it' }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()

    // Reopen for the next cycle (disarm, then arm again) — every real caller
    // reuses the same `ConfirmDialog` instance rather than remounting it, so
    // the ref has to have cleared on its own.
    fireEvent.click(screen.getByRole('button', { name: 'reopen' }))
    fireEvent.click(screen.getByRole('button', { name: 'Do it' }))
    expect(onConfirm).toHaveBeenCalledTimes(2)
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

  it('takes a third confirm after two fast failures with the same error text (#181)', async () => {
    // The jam: each attempt fails before React commits a render with `pending`
    // true, and both fail with the same text, so neither `pending`, `open`, nor
    // `error` changes between the second click and the third. Only the
    // promise settling can tell the dialog the action is over.
    const onConfirm = vi.fn()
    function FailingHarness() {
      const [error, setError] = useState<string | null>(null)
      return (
        <ConfirmDialog
          open
          onOpenChange={() => undefined}
          title="Do the thing?"
          confirmLabel="Do it"
          error={error}
          onConfirm={() => {
            onConfirm()
            setError('run 3 is still running (409)')
            return Promise.reject(new Error('run 3 is still running (409)'))
          }}
        >
          <p>This does the thing.</p>
        </ConfirmDialog>
      )
    }
    render(<FailingHarness />)
    const button = screen.getByRole('button', { name: 'Do it' })

    for (const attempt of [1, 2, 3]) {
      await act(async () => {
        fireEvent.click(button)
        await Promise.resolve()
      })
      expect(onConfirm).toHaveBeenCalledTimes(attempt)
    }
    expect(screen.getByRole('alert')).toHaveTextContent('run 3 is still running (409)')
  })

  it('holds further confirms until the promise settles, then takes the next one', async () => {
    let settle: () => void = () => undefined
    const onConfirm = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve
        }),
    )
    render(<Harness onConfirm={onConfirm} />)
    const button = screen.getByRole('button', { name: 'Do it' })

    fireEvent.click(button)
    await act(async () => {
      await Promise.resolve()
    })
    fireEvent.click(button)
    expect(onConfirm).toHaveBeenCalledTimes(1)

    await act(async () => {
      settle()
      await Promise.resolve()
    })
    fireEvent.click(button)
    expect(onConfirm).toHaveBeenCalledTimes(2)
  })
})
