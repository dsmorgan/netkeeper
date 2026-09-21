/**
 * Every key in spec 10.2, and the one thing `→` must not do.
 *
 * The assertions go through the fake backend's state rather than through the
 * request bodies, because what matters is that pressing `m` leaves the contact
 * marked met — not that some request went out.
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { currentName, renderTriage } from './test-render'

function press(key: string) {
  return fireEvent.keyDown(window, { key })
}

describe('the triage keyboard map (spec 10.2)', () => {
  it('m marks the contact met and moves to the next one', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    expect(await currentName()).toContain('Ada')

    press('m')

    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(await currentName()).toContain('Bo')
  })

  it('n marks the contact not met', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('n')

    await waitFor(() => expect(backend.byId(1).met).toBe('not_met'))
  })

  it('s skips, and the skipped contact is revisitable under the filter', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    press('s')
    await waitFor(() => expect(backend.byId(1).met).toBe('skip'))
    expect(await currentName()).toContain('Bo')

    fireEvent.click(screen.getByRole('button', { name: 'Skipped' }))
    await waitFor(async () => expect(await currentName()).toContain('Ada'))
  })

  it('→ moves on without deciding and writes nothing', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    expect(await currentName()).toContain('Ada')

    press('ArrowRight')

    expect(await currentName()).toContain('Bo')
    await waitFor(() => expect(backend.countOf('/api/v1/triage/next')).toBe(2))
    expect(backend.countOf('/api/v1/triage/decisions')).toBe(0)
    expect(backend.decisions).toHaveLength(0)
    expect(backend.byId(1).met).toBe('unknown')
    expect(backend.byId(1).triaged_at).toBeNull()
  })

  it('→ asks for the contact past the frontier, never one already passed', async () => {
    const { backend } = renderTriage({ contacts: 6 })
    await currentName()

    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    press('ArrowRight')
    expect(await currentName()).toContain('Cleo')

    press('m')
    await waitFor(() => expect(backend.byId(3).met).toBe('met'))
    // Bo was passed over, not decided, and never came back around.
    expect(backend.byId(2).met).toBe('unknown')
    expect(await currentName()).toContain('Dev')
  })

  it('t opens the tag picker and applies a tag', async () => {
    const { backend } = renderTriage({ contacts: 2 })
    await currentName()

    press('t')

    const picker = await screen.findByRole('group', { name: 'Tag this contact' })
    fireEvent.click(await within(picker).findByRole('button', { name: /founder/ }))
    await waitFor(() => expect(backend.byId(1).tags).toHaveLength(1))
    expect(backend.byId(1).tags[0]?.name).toBe('founder')
  })

  it('t says that tagging is not on the triage undo stack', async () => {
    renderTriage({ contacts: 2 })
    await currentName()

    press('t')

    const picker = await screen.findByRole('group', { name: 'Tag this contact' })
    expect(picker).toHaveTextContent(/not on the triage undo stack/i)
  })

  it('p edits the preferred name, and typing in it does not decide', async () => {
    const { backend } = renderTriage({ contacts: 2 })
    await currentName()

    press('p')

    const field = await screen.findByLabelText('Preferred name')
    fireEvent.change(field, { target: { value: 'Addie' } })
    // "m" typed into the field is a letter, not a decision.
    fireEvent.keyDown(field, { key: 'm' })
    expect(backend.byId(1).met).toBe('unknown')

    fireEvent.submit(field.closest('form')!)
    await waitFor(() => expect(backend.byId(1).preferred_name).toBe('Addie'))
    expect(await currentName()).toContain('Addie')
  })

  it('? opens the help overlay, lists every key, and does not steal the keyboard', async () => {
    const { backend } = renderTriage({ contacts: 3 })
    await currentName()

    press('?')

    const help = await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })
    expect(help).toHaveAttribute('aria-modal', 'false')
    for (const label of ['m', 'n', 's', 'u', 't', 'p', '→', '?', 'Esc']) {
      expect(within(help).getByText(label, { selector: 'kbd' })).toBeInTheDocument()
    }

    // The map still works while the overlay is open.
    press('m')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(screen.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeInTheDocument()

    press('Escape')
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Keyboard shortcuts' })).not.toBeInTheDocument(),
    )
  })

  it('stands aside for browser shortcuts and for keys it does not own', async () => {
    const { backend } = renderTriage({ contacts: 3 })
    await currentName()

    fireEvent.keyDown(window, { key: 'm', metaKey: true })
    fireEvent.keyDown(window, { key: 'q' })
    fireEvent.keyDown(window, { key: 'ArrowLeft' })

    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(backend.byId(1).met).toBe('unknown')
    expect(backend.countOf('/api/v1/triage/decisions')).toBe(0)
  })

  it('prevents the default for a key it handles, so nothing scrolls', async () => {
    renderTriage({ contacts: 3 })
    await currentName()

    expect(press('ArrowRight')).toBe(false)
    expect(fireEvent.keyDown(window, { key: 'z' })).toBe(true)
  })
})
