/**
 * The screen is keyboard-*first*, not keyboard-only (issue #114).
 *
 * The CP2 walkthrough read as "nice that there are keypress shortcuts, but
 * there needs to be buttons too", and stopped there. So the row of buttons is
 * generated from the keymap and checked against it here: an action that gains a
 * key without gaining a button fails this file, which is the only way the two
 * stay in step.
 *
 * The other half is that the mouse path costs what the keyboard path costs. A
 * click goes through the same handler, synchronously, against the same two
 * cards in hand.
 *
 * Since #142 the buttons live in two rows — the action row above the card, and
 * the decision row on it — and the invariant is unchanged by that: every
 * action with a key has a button somewhere a person can press it. Which row is
 * `KeyBinding.where`'s business, and the decisions being in exactly one of
 * them is asserted here rather than left to the layout.
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { BAR_BINDINGS, BUTTON_BINDINGS, DECISION_BINDINGS, KEY_BINDINGS } from './keymap'
import { currentName, renderTriage } from './test-render'

function click(name: string) {
  fireEvent.click(screen.getByRole('button', { name }))
}

describe('the on-screen controls', () => {
  it('gives every action in the keymap a button that shows its key', async () => {
    renderTriage({ contacts: 4 })
    await currentName()

    // Two rows, one invariant: an action cannot gain a key without gaining a
    // button somebody can press. `where` says which row draws it.
    const rows: Record<string, HTMLElement> = {
      bar: screen.getByTestId('triage-actions'),
      card: screen.getByTestId('triage-decision'),
    }
    for (const binding of BUTTON_BINDINGS) {
      const row = rows[binding.where]
      expect(row, `${binding.action} is drawn in an unknown row`).toBeDefined()
      const button = within(row!).getByRole('button', { name: binding.button })
      expect(button).toHaveAttribute('aria-keyshortcuts', binding.aria)
      expect(within(button).getByText(binding.label, { selector: 'kbd' })).toBeInTheDocument()
    }
    expect(BAR_BINDINGS.length + DECISION_BINDINGS.length).toBe(BUTTON_BINDINGS.length)
  })

  it('offers the three decisions once, on the card and not in the action row', async () => {
    // The row sits above the card, so a second copy of Met/Not met/Skip there
    // would put the met call above the name and the tags the person is meant
    // to fix first — which is the ordering #142 exists to undo — and would be
    // two answers to "where do I decide?". This is what stops it coming back.
    renderTriage({ contacts: 4 })
    await currentName()

    const bar = screen.getByTestId('triage-actions')
    for (const binding of DECISION_BINDINGS) {
      expect(within(bar).queryByRole('button', { name: binding.button })).toBeNull()
      // Once on the whole screen, so no test has to say which one it means.
      expect(screen.getAllByRole('button', { name: binding.button })).toHaveLength(1)
    }
    expect(DECISION_BINDINGS.map((binding) => binding.button)).toEqual(['Met', 'Not met', 'Skip'])

    // And the decision row comes after the card, which is the point of it.
    const steps = screen.getByTestId('triage-steps')
    expect(steps.contains(screen.getByTestId('triage-decision'))).toBe(true)
    expect(
      screen.getByTestId('triage-card').compareDocumentPosition(steps) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('leaves out only Escape, whose button is the Close on whatever is open', async () => {
    renderTriage({ contacts: 4 })
    await currentName()

    const withoutButton = KEY_BINDINGS.filter((binding) => binding.button === null)
    expect(withoutButton.map((binding) => binding.action)).toEqual(['dismiss'])

    // And the thing Escape closes carries its own.
    fireEvent.keyDown(window, { key: '?' })
    const help = await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })
    fireEvent.click(within(help).getByRole('button', { name: 'Close' }))
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Keyboard shortcuts' })).not.toBeInTheDocument(),
    )
  })

  it('records a decision from the button, the way the key does', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    expect(await currentName()).toContain('Ada')

    click('Met')

    await waitFor(() => expect(backend.byId(1).met).toBe('met'))
    expect(await currentName()).toContain('Bo')
  })

  it.each([
    ['Not met', 'not_met'],
    ['Skip', 'skip'],
  ])('records %s from its button', async (label, expected) => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    click(label)

    await waitFor(() => expect(backend.byId(1).met).toBe(expected))
  })

  it('moves on from the Next button without writing anything', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    expect(await currentName()).toContain('Ada')

    click('Next')

    expect(await currentName()).toContain('Bo')
    expect(backend.decisions).toHaveLength(0)
    expect(backend.byId(1).met).toBe('unknown')
  })

  it('opens the tag picker, the name editor, and the help from their buttons', async () => {
    renderTriage({ contacts: 4 })
    await currentName()

    click('Tag')
    expect(await screen.findByRole('group', { name: 'Tag this contact' })).toBeInTheDocument()

    click('Name')
    expect(await screen.findByLabelText('Preferred name')).toBeInTheDocument()

    click('Keyboard')
    expect(await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })).toBeInTheDocument()
  })

  it('takes back the last write from the Undo button', async () => {
    const { backend } = renderTriage({ contacts: 4 })
    await currentName()

    click('Met')
    await waitFor(() => expect(backend.byId(1).met).toBe('met'))

    click('Undo')

    await waitFor(() => expect(backend.byId(1).met).toBe('unknown'))
  })

  it('does not disable a control instead of explaining it', async () => {
    // Nothing to undo yet, nothing behind you yet: both still press, and both
    // answer. A disabled button says neither what it would do nor why it cannot.
    renderTriage({ contacts: 2 })
    await currentName()

    const bar = screen.getByTestId('triage-actions')
    for (const binding of BAR_BINDINGS) {
      expect(within(bar).getByRole('button', { name: binding.button })).not.toBeDisabled()
    }
    const decisions = screen.getByTestId('triage-decision')
    for (const binding of DECISION_BINDINGS) {
      expect(within(decisions).getByRole('button', { name: binding.button })).not.toBeDisabled()
    }

    click('Back')
    expect(screen.getByTestId('triage-notice')).toHaveTextContent(/nothing behind you/i)
  })

  it('costs no round trip, the same as the keyboard', async () => {
    renderTriage({ contacts: 6, latencyMs: 20 })
    await currentName()
    // Well past the round trip, so the prefetch is in hand.
    await new Promise((resolve) => setTimeout(resolve, 80))

    const showing = await currentName()
    click('Met')
    // No await: the next contact was already rendered when the click landed.
    expect(screen.getByTestId('triage-card').querySelector('h2')?.textContent).not.toBe(showing)
  })
})
